"""pew — run commands, push files, and diff files across systems.

Host selection is delegated entirely to Ansible: a --hosts pattern is
resolved via `ansible <pattern> --list-hosts`, so any pattern Ansible
understands (group names, `web:&staging`, `web:!excluded`, `host1,host2`,
...) works here too. No host/group data is stored or parsed by this tool.

Spiritual successor to the old Perl "mew" tool. Not flag-compatible with it.
"""

import argparse
import concurrent.futures
import datetime
import grp
import json
import os
import pwd
import queue
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import threading

PROG = "pew"


def die(msg, code=1):
    print(f"{PROG}: error: {msg}", file=sys.stderr)
    sys.exit(code)


def warn(msg):
    print(f"{PROG}: warning: {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Verbose/debug logging. Plain module globals (set once in main()) rather
# than threading opts through every function, matching the original mew's
# own $DEBUG global.
# ---------------------------------------------------------------------------

VERBOSE = False
DEBUG = False


def info(msg):
    if VERBOSE or DEBUG:
        print(f"{PROG}: {msg}", file=sys.stderr)


def debug(msg):
    if DEBUG:
        print(f"{PROG}: debug: {msg}", file=sys.stderr)


def debug_cmd(cmd):
    if DEBUG:
        print(f"{PROG}: debug: + {shlex.join(cmd)}", file=sys.stderr)


def debug_json(label, data):
    if DEBUG:
        print(f"{PROG}: debug: {label}:", file=sys.stderr)
        print(json.dumps(data, indent=2, sort_keys=True), file=sys.stderr)


def debug_block(label, text):
    if DEBUG and text and text.strip():
        print(f"{PROG}: debug: {label}:", file=sys.stderr)
        print(text.rstrip(), file=sys.stderr)


# ---------------------------------------------------------------------------
# Host resolution (via ansible)
# ---------------------------------------------------------------------------

PEWRC_PATH = os.path.expanduser("~/.pewrc")


def load_pewrc():
    """Parse ~/.pewrc as simple key=value lines. Missing file -> {}."""
    if not os.path.isfile(PEWRC_PATH):
        return {}
    config = {}
    with open(PEWRC_PATH) as f:
        for lineno, raw in enumerate(f, 1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                warn(f"{PEWRC_PATH}:{lineno}: ignoring line without '=': {line!r}")
                continue
            key, _, value = line.partition("=")
            config[key.strip()] = value.strip().strip('"').strip("'")
    return config


def determine_inventory(cli_inventory):
    """--inventory flag > $PEW_INVENTORY > ~/.pewrc [inventory=] > let ansible decide."""
    if cli_inventory:
        debug(f"inventory from --inventory: {cli_inventory}")
        return cli_inventory
    if os.environ.get("PEW_INVENTORY"):
        debug(f"inventory from $PEW_INVENTORY: {os.environ['PEW_INVENTORY']}")
        return os.environ["PEW_INVENTORY"]
    from_rc = load_pewrc().get("inventory")
    if from_rc:
        debug(f"inventory from {PEWRC_PATH}: {from_rc}")
    else:
        debug("no inventory override found; letting ansible resolve its own default")
    return from_rc


def apply_ansible_config():
    """Export $ANSIBLE_CONFIG from ~/.pewrc's ansible_config=, if set and not
    already present in the environment.

    Unlike inventory, this isn't a per-call argument — ansible.cfg carries
    settings pew has no other way to pass through (vault_password_file,
    roles_path, callback config, etc.), and Ansible only auto-discovers
    ./ansible.cfg relative to the *current directory*, not pew's own
    location or the target inventory's. An explicit environment variable
    already set by the caller always wins over ~/.pewrc.
    """
    if os.environ.get("ANSIBLE_CONFIG"):
        debug(f"ANSIBLE_CONFIG already set in environment: {os.environ['ANSIBLE_CONFIG']}")
        return
    config_path = load_pewrc().get("ansible_config")
    if not config_path:
        return
    if not os.path.isfile(config_path):
        warn(f"ansible_config in {PEWRC_PATH} does not exist: {config_path}")
    debug(f"ANSIBLE_CONFIG from {PEWRC_PATH}: {config_path}")
    os.environ["ANSIBLE_CONFIG"] = config_path


def resolve_hosts(pattern, inventory=None, sort=False):
    inventory = determine_inventory(inventory)
    info(f"resolving hosts for pattern {pattern!r}"
         + (f" (inventory: {inventory})" if inventory else ""))
    cmd = ["ansible", pattern, "--list-hosts"]
    if inventory:
        cmd += ["-i", inventory]
    debug_cmd(cmd)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        die("'ansible' not found on PATH. Is ansible-core installed?")

    if proc.returncode != 0:
        die(f"could not resolve host pattern {pattern!r}:\n"
            f"{proc.stderr.strip() or proc.stdout.strip()}")

    hosts = [
        line.strip() for line in proc.stdout.splitlines()
        if line.strip() and not line.lstrip().startswith("hosts (")
    ]
    if not hosts:
        die(f"host pattern {pattern!r} matched no hosts")
    hosts = sorted(hosts) if sort else hosts
    info(f"resolved {len(hosts)} host(s)")
    debug(f"host list: {', '.join(hosts)}")
    return hosts


# ---------------------------------------------------------------------------
# Fact-based filtering (--where)
# ---------------------------------------------------------------------------

WHERE_OPERATOR_RE = re.compile(r"^(.*?)(!=|<=|>=|~=|<|>|=)(.*)$")


def parse_regex(value):
    try:
        re.compile(value)
    except re.error as e:
        raise argparse.ArgumentTypeError(f"invalid regex {value!r}: {e}")
    return value


def parse_strftime(value):
    try:
        datetime.datetime.now().strftime(value)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"invalid --strftime format {value!r}: {e}")
    return value


def parse_where(value):
    m = WHERE_OPERATOR_RE.match(value)
    if not m:
        raise argparse.ArgumentTypeError(
            f"--where must be KEY<op>VALUE (one of = != < > <= >= ~=), got {value!r}"
        )
    key, op, val = m.group(1).strip(), m.group(2), m.group(3).strip()
    if op == "~=":
        parse_regex(val)
    return key, op, val


def compare_one(op, actual, expected):
    a = str(actual)
    if op == "=":
        return a == expected
    if op == "~=":
        return re.search(expected, a) is not None
    try:
        lhs, rhs = float(a), float(expected)
    except ValueError:
        lhs, rhs = a, expected
    if op == "<":
        return lhs < rhs
    if op == ">":
        return lhs > rhs
    if op == "<=":
        return lhs <= rhs
    if op == ">=":
        return lhs >= rhs
    raise AssertionError(f"unhandled --where operator {op!r}")


def where_matches(merged, segments, op, expected):
    resolved = list(resolve_path(merged, segments))
    if op == "!=":
        # Negation of "=", not "flip the operator": a [] list path only
        # satisfies != if NONE of its elements equal the value (including
        # when the path resolves to nothing at all — a missing key can't
        # equal anything, so it trivially satisfies !=).
        return not any(compare_one("=", v, expected) for v in resolved)
    return any(compare_one(op, v, expected) for v in resolved)


# A --where path is dotted keys with optional [] ("any element of this
# list") or [N] (a specific index): ansible_mounts[].mount,
# ansible_default_ipv4.address, ansible_mounts[0].fstype. Splits on '.' and
# on bracket groups; bare dots between segments just fall out of the match.
PATH_SEGMENT_RE = re.compile(r"[^.\[\]]+|\[\d*\]")


def parse_path(path):
    return PATH_SEGMENT_RE.findall(path)


def resolve_path(data, segments):
    """Yields every value `segments` reaches by walking `data`. A plain
    dotted path with no [] yields at most one value (or none, if any
    segment is missing) — same behavior as a flat dict.get() lookup."""
    if not segments:
        yield data
        return
    seg, rest = segments[0], segments[1:]
    if seg == "[]":
        if isinstance(data, list):
            for item in data:
                yield from resolve_path(item, rest)
        return
    if seg.startswith("[") and seg.endswith("]"):
        try:
            idx = int(seg[1:-1])
        except ValueError:
            return
        if isinstance(data, list) and -len(data) <= idx < len(data):
            yield from resolve_path(data[idx], rest)
        return
    if isinstance(data, dict) and seg in data:
        yield from resolve_path(data[seg], rest)


def flatten(data, prefix=""):
    """Inverse of resolve_path: walks a nested dict/list and yields every
    leaf as (path, value), using the exact same dotted/bracket syntax
    parse_path()/resolve_path() expect — so a path shown by `pew facts` is
    directly usable as a --where KEY."""
    if isinstance(data, dict):
        for k, v in data.items():
            yield from flatten(v, f"{prefix}.{k}" if prefix else k)
    elif isinstance(data, list):
        for i, v in enumerate(data):
            yield from flatten(v, f"{prefix}[{i}]")
    else:
        yield prefix, data


# Matches ansible ad-hoc's *default* result marker (no special flag or
# stdout callback needed — both `--tree` and `-o`/the oneline callback are
# deprecated for removal in ansible-core 2.23, so this deliberately doesn't
# use either):
#   hostname | SUCCESS => {
#       "ansible_facts": { ... },
#       "changed": false
#   }
# Both success and unreachable/failed results use this same "=> {...}"
# shape in default output, so one parser handles both — a host is simply
# absent from the returned dict if its JSON has no ansible_facts key (or
# didn't match at all).
ANSIBLE_RESULT_MARKER_RE = re.compile(
    r"^(?P<host>\S+) \| \S+(?: \| rc=\d+)? => ", re.MULTILINE,
)


def gather_facts(hosts, keys, opts):
    """Returns {host: {key: value, ...}} for hosts ansible could reach.

    keys=None (or empty) fetches every fact, unfiltered — used by `facts`.
    Unreachable/failed hosts are simply absent from the result — the caller
    decides how to treat that. Reuses Ansible's own fact cache automatically
    if fact_caching is configured in ansible.cfg; otherwise gathers live.
    """
    inventory = determine_inventory(opts.inventory)
    info(f"gathering facts ({', '.join(sorted(set(keys))) if keys else 'all'}) "
         f"for {len(hosts)} host(s)")
    cmd = ["ansible", ",".join(hosts), "-m", "setup"]
    if keys:
        cmd += ["-a", f"filter={','.join(sorted(set(keys)))}"]
    if inventory:
        cmd += ["-i", inventory]
    user = effective_remote_user(opts)
    if user:
        cmd += ["-u", user]
    debug_cmd(cmd)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        die("'ansible' not found on PATH. Is ansible-core installed?")
    debug_block("ansible -m setup stdout", proc.stdout)
    debug_block("ansible -m setup stderr", proc.stderr)

    decoder = json.JSONDecoder()
    facts = {}
    for m in ANSIBLE_RESULT_MARKER_RE.finditer(proc.stdout):
        try:
            data, _ = decoder.raw_decode(proc.stdout, m.end())
        except json.JSONDecodeError:
            continue
        host_facts = data.get("ansible_facts")
        if host_facts:
            facts[m.group("host")] = host_facts
    debug_json("gathered facts", facts)
    return facts


def gather_inventory_vars(hosts, opts):
    """Returns {host: {var: value, ...}} from Ansible inventory itself
    (host_vars/group_vars/inline vars) — NOT gathered facts. Cheap: just a
    config read, no SSH connection needed, so always fetched whenever
    --where is used regardless of whether any clause actually needs it.
    """
    inventory = determine_inventory(opts.inventory)
    cmd = ["ansible-inventory", "--list"]
    if inventory:
        cmd += ["-i", inventory]
    debug_cmd(cmd)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        die("'ansible-inventory' not found on PATH. Is ansible-core installed?")
    debug_block("ansible-inventory stdout", proc.stdout)
    debug_block("ansible-inventory stderr", proc.stderr)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}
    all_hostvars = data.get("_meta", {}).get("hostvars", {})
    return {h: all_hostvars[h] for h in hosts if h in all_hostvars}


def filter_by_where(hosts, wheres, opts):
    if not wheres:
        return hosts

    parsed = [(parse_path(k), op, v) for k, op, v in wheres]
    top_level_keys = {segs[0] for segs, _, _ in parsed if segs}

    inv_vars = gather_inventory_vars(hosts, opts)
    facts = gather_facts(hosts, top_level_keys, opts)

    matched, unreachable = [], []
    for host in hosts:
        # Gathered facts win over inventory vars on key collision, same
        # precedence order Ansible itself uses.
        merged = {**inv_vars.get(host, {}), **facts.get(host, {})}
        if all(where_matches(merged, segs, op, v) for segs, op, v in parsed):
            matched.append(host)
        elif host not in facts:
            # No fact data at all for this host, and it didn't match on
            # inventory vars alone — genuinely unknown, not just a
            # confident non-match, so treat it as unreachable.
            unreachable.append(host)

    if unreachable:
        hint = "" if DEBUG else " (rerun with --debug to see ansible's output)"
        warn(f"could not gather facts from {len(unreachable)} host(s), "
             f"excluded from consideration: {', '.join(sorted(unreachable))}{hint}")
    if not matched:
        die("no hosts matched --where filter(s)")
    info(f"{len(matched)}/{len(hosts)} host(s) matched --where filter(s)")
    return matched


def row_matches(path, value, match_any, match_keys, match_values):
    val_str = str(value)
    for pat in match_any:
        if not (re.search(pat, path) or re.search(pat, val_str)):
            return False
    for pat in match_keys:
        if not re.search(pat, path):
            return False
    for pat in match_values:
        if not re.search(pat, val_str):
            return False
    return True


def do_facts(hosts, opts):
    inv_vars = gather_inventory_vars(hosts, opts)
    facts = gather_facts(hosts, None, opts)

    unreachable = [h for h in hosts if h not in facts]
    if unreachable:
        hint = "" if DEBUG else " (rerun with --debug to see ansible's output)"
        warn(f"could not gather facts from {len(unreachable)} host(s): "
             f"{', '.join(sorted(unreachable))}{hint}")

    match_any = opts.match or []
    match_keys = opts.match_keys or []
    match_values = opts.match_values or []

    for host in hosts:
        host_facts = facts.get(host, {})
        host_vars = inv_vars.get(host, {})
        collisions = set(host_facts) & set(host_vars)

        print(f"{host}:")
        for label, data, other_label in (
            ("facts", host_facts, "variable"), ("vars", host_vars, "fact"),
        ):
            rows = list(flatten(data))
            shown = [
                (p, v) for p, v in rows
                if row_matches(p, v, match_any, match_keys, match_values)
            ]
            print(f"  {label}:")
            if not shown:
                if rows:
                    print(f"    (no matches out of {len(rows)} {label} discovered)")
                else:
                    print("    (none discovered)")
            else:
                last_top = None
                for p, v in shown:
                    top = parse_path(p)[0] if p else p
                    if top in collisions and top != last_top:
                        print(f"    *** WARNING: {label[:-1]} {top!r} collides "
                              f"with an inventory {other_label} of the same "
                              f"name — facts always win ***")
                    last_top = top
                    print(f"    {p} = {v}")
            info(f"{host}: {len(shown)}/{len(rows)} {label} matched")

    return unreachable


# ---------------------------------------------------------------------------
# Confirm loop (y/n/q/a/?)
# ---------------------------------------------------------------------------

def label_width(hosts):
    return max((len(h) for h in hosts), default=0)


CONFIRM_SYNONYMS = {
    "": "y", "y": "y", "yes": "y",
    "n": "n", "no": "n", "skip": "n",
    "a": "a", "all": "a",
    "q": "q", "quit": "q",
    "?": "?",
}

CONFIRM_HELP = (
    "y = yes, run on this host (default)\n"
    "n = no, skip this host\n"
    "a = yes to this and all remaining hosts, stop asking\n"
    "q = quit immediately, don't touch remaining hosts\n"
    "? = show this help"
)


def confirm(host, description, state, width=0):
    """Returns True if the action should run on this host."""
    if state["yes_all"]:
        return True
    while True:
        try:
            ans = input(f"{host:<{width}}: {description} [Y/n/a/q/?] ").strip().lower()
        except EOFError:
            ans = "q"
        ans = CONFIRM_SYNONYMS.get(ans)
        if ans == "y":
            return True
        if ans == "n":
            return False
        if ans == "q":
            print(f"{PROG}: quit on demand")
            sys.exit(1)
        if ans == "a":
            state["yes_all"] = True
            return True
        if ans == "?":
            print(CONFIRM_HELP)
            continue
        print("Don't understand that. Use '?' for help.")


# ---------------------------------------------------------------------------
# Remote connection spec (ssh/scp/rsync target, honoring --remote-user/--root)
# ---------------------------------------------------------------------------

def effective_remote_user(opts):
    return "root" if opts.root else opts.remote_user


def remote_spec(host, opts):
    """Build the ssh/scp/rsync connection target for a host.

    Ansible resolves ansible_user per host from inventory; pew only ever
    asks ansible for bare hostnames (`ansible ... --list-hosts`), so it has
    no per-host user data of its own. --remote-user/--root apply one user
    uniformly across the whole invocation instead.
    """
    user = effective_remote_user(opts)
    return f"{user}@{host}" if user else host


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def substitute(args, host):
    return [a.replace("{}", host) for a in args]


def run_host(host, args, opts):
    argv = ["ssh", remote_spec(host, opts)] + substitute(args, host)
    debug_cmd(argv)
    try:
        proc = subprocess.run(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, timeout=opts.timeout,
        )
        return proc.returncode, proc.stdout, None
    except subprocess.TimeoutExpired:
        return 124, "", f"timed out after {opts.timeout}s"
    except FileNotFoundError:
        return 127, "", "ssh not found on PATH"


def run_host_streaming(host, args, opts, on_line):
    """Like run_host, but calls on_line(line) for each line of output as it
    arrives instead of buffering and returning it all at once. Returns
    (rc, note), same shape as run_host's last two return values.

    Timeout is enforced with a threading.Timer that kills the process if it
    fires — not subprocess.run(timeout=...), which doesn't apply here since
    we're reading incrementally. Killing the process closes its end of the
    pipe, which ends the `for line in proc.stdout` loop naturally even if
    the command was still silent (never having produced a line at all).
    """
    argv = ["ssh", remote_spec(host, opts)] + substitute(args, host)
    debug_cmd(argv)
    try:
        proc = subprocess.Popen(
            argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        )
    except FileNotFoundError:
        return 127, "ssh not found on PATH"

    timed_out = False
    timer = None
    if opts.timeout:
        def _kill():
            nonlocal timed_out
            timed_out = True
            proc.kill()
        timer = threading.Timer(opts.timeout, _kill)
        timer.start()

    for line in proc.stdout:
        on_line(line.rstrip("\n"))
    rc = proc.wait()

    if timer:
        timer.cancel()
    if timed_out:
        return 124, f"timed out after {opts.timeout}s"
    return rc, None


DEFAULT_TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S"


def effective_timestamp_format(opts):
    """--strftime > --ts > --tss > --timestamp (using ~/.pewrc's
    timestamp_format= if set, else DEFAULT_TIMESTAMP_FORMAT) > no
    timestamp at all. The four flags are mutually exclusive at the
    argparse level, so at most one of them is ever actually set."""
    if getattr(opts, "strftime", None):
        return opts.strftime
    if getattr(opts, "ts", False):
        return "%Y%m%d%H%M%S"
    if getattr(opts, "tss", False):
        return "%Y%m%d%H%M%S.%f"
    if getattr(opts, "timestamp", False):
        return load_pewrc().get("timestamp_format") or DEFAULT_TIMESTAMP_FORMAT
    return None


def timestamp_prefix(opts):
    """Terminal-only — never written into --log-prefix files, which stay
    clean on purpose."""
    fmt = effective_timestamp_format(opts)
    return datetime.datetime.now().strftime(fmt) + " " if fmt else ""


def emit_line(host, line, quiet, width, opts=None):
    ts = timestamp_prefix(opts)
    print(f"{ts}{line}" if quiet else f"{ts}{host:<{width}}: {line}")


def open_log(log_prefix, host):
    # buffering=1 (line-buffered) so --log-prefix files are flushed on every
    # line, not just on close — the whole point of pairing this with
    # --follow is that `tail -f PREFIX.hostname` works while it's running.
    return open(f"{log_prefix}.{host}", "a", buffering=1) if log_prefix else None


def do_run_follow_serial(hosts, args, opts, width):
    failures = []
    state = {"yes_all": opts.yes}
    for host in hosts:
        desc = run_cmdline(host, args, opts)
        if not confirm(host, desc, state, width):
            print(f"*** {host}: SKIPPED ***")
            continue
        log_f = open_log(opts.log_prefix, host)

        def on_line(line):
            emit_line(host, line, opts.quiet, width, opts)
            if log_f:
                log_f.write(line + "\n")

        rc, note = run_host_streaming(host, args, opts, on_line)
        if log_f:
            log_f.close()
        if rc != 0:
            failures.append(host)
            extra = f" ({note})" if note else ""
            warn(f"{host} returned {rc}{extra}")
            if not opts.continue_on_fail:
                warn("stopping (pass --continue-on-fail/-X to proceed past failures)")
                break
    return failures


def do_run_follow_parallel(hosts, args, opts, width):
    q = queue.Queue()
    DONE = object()
    log_files = {h: open_log(opts.log_prefix, h) for h in hosts}

    def worker(host):
        def on_line(line):
            q.put((host, line))
        rc, note = run_host_streaming(host, args, opts, on_line)
        q.put((host, DONE))
        return rc, note

    failures = []
    max_workers = opts.jobs if opts.jobs else len(hosts)
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        future_to_host = {ex.submit(worker, h): h for h in hosts}
        remaining = len(hosts)
        while remaining > 0:
            host, item = q.get()
            if item is DONE:
                remaining -= 1
                continue
            emit_line(host, item, opts.quiet, width, opts)
            if log_files[host]:
                log_files[host].write(item + "\n")

        for fut in concurrent.futures.as_completed(future_to_host):
            host = future_to_host[fut]
            rc, note = fut.result()
            if rc != 0:
                failures.append(host)
                extra = f" ({note})" if note else ""
                warn(f"{host} returned {rc}{extra}")

    for f in log_files.values():
        if f:
            f.close()
    return failures


def emit(host, output, quiet, multiline, log_prefix, width=0, opts=None):
    # Log files stay clean on purpose — timestamps (if any) are terminal-only.
    if log_prefix:
        with open(f"{log_prefix}.{host}", "a") as f:
            f.write(output)
            if output and not output.endswith("\n"):
                f.write("\n")

    ts = timestamp_prefix(opts)

    if quiet:
        for line in output.splitlines():
            print(f"{ts}{line}")
        return

    if multiline:
        print(f"{ts}### {host.upper()} ###")
        for line in output.splitlines():
            print(f"{ts}{line}")
        return

    prefix = f"{host:<{width}}: "
    lines = output.splitlines()
    if not lines:
        print(f"{ts}{prefix}")
    for line in lines:
        print(f"{ts}{prefix}{line}")


def run_cmdline(host, args, opts):
    return "ssh " + remote_spec(host, opts) + " " + shlex.join(substitute(args, host))


def do_run(hosts, args, opts):
    width = label_width(hosts)

    if opts.dry_run:
        for host in hosts:
            emit(host, run_cmdline(host, args, opts), opts.quiet, opts.multiline, None, width, opts)
        return []

    if opts.follow:
        if opts.parallel:
            return do_run_follow_parallel(hosts, args, opts, width)
        return do_run_follow_serial(hosts, args, opts, width)

    failures = []

    if opts.parallel:
        max_workers = opts.jobs if opts.jobs else len(hosts)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_to_host = {
                ex.submit(run_host, h, args, opts): h for h in hosts
            }
            for fut in concurrent.futures.as_completed(future_to_host):
                host = future_to_host[fut]
                rc, output, note = fut.result()
                emit(host, output, opts.quiet, opts.multiline, opts.log_prefix, width, opts)
                if rc != 0:
                    failures.append(host)
                    extra = f" ({note})" if note else ""
                    warn(f"{host} returned {rc}{extra}")
        return failures

    state = {"yes_all": opts.yes}
    for host in hosts:
        desc = run_cmdline(host, args, opts)
        if not confirm(host, desc, state, width):
            print(f"*** {host}: SKIPPED ***")
            continue
        rc, output, note = run_host(host, args, opts)
        emit(host, output, opts.quiet, opts.multiline, opts.log_prefix, width, opts)
        if rc != 0:
            failures.append(host)
            extra = f" ({note})" if note else ""
            warn(f"{host} returned {rc}{extra}")
            if not opts.continue_on_fail:
                warn("stopping (pass --continue-on-fail/-X to proceed past failures)")
                break
    return failures


# ---------------------------------------------------------------------------
# copy
# ---------------------------------------------------------------------------

def owner_group_mode(local_file):
    st = os.stat(local_file)
    try:
        owner = pwd.getpwuid(st.st_uid).pw_name
    except KeyError:
        owner = str(st.st_uid)
    try:
        group = grp.getgrgid(st.st_gid).gr_name
    except KeyError:
        group = str(st.st_gid)
    mode = stat.S_IMODE(st.st_mode)
    return owner, group, mode


def effective_owner_group_mode(local_file, opts):
    owner, group, mode = owner_group_mode(local_file)
    if opts.owner:
        owner = opts.owner
    if opts.group:
        group = opts.group
    if opts.mode is not None:
        mode = opts.mode
    return owner, group, mode


def fixup_ownership(host, target, local_file, opts):
    owner, group, mode = effective_owner_group_mode(local_file, opts)
    q = shlex.quote(target)
    remote_script = (
        f"chown {owner}:{group} {q} >/dev/null 2>&1; CHOWN_RC=$?; "
        f"chmod {mode:o} {q}; CHMOD_RC=$?; "
        f"echo PEW_FIXUP CHOWN_RC=$CHOWN_RC CHMOD_RC=$CHMOD_RC"
    )
    fixup_argv = ["ssh", remote_spec(host, opts), remote_script]
    debug_cmd(fixup_argv)
    proc = subprocess.run(fixup_argv, capture_output=True, text=True)
    chown_rc = None
    for line in proc.stdout.splitlines():
        if line.startswith("PEW_FIXUP"):
            for field in line.split():
                if field.startswith("CHOWN_RC="):
                    chown_rc = int(field.split("=", 1)[1])
    if chown_rc:
        warn(f"{host}: could not set owner:group on {target} "
             f"(owner={owner} group={group}); mode {mode:o} still applied")


def build_copy_targets(local_files, remote_path, same):
    """Returns a list of (local_file, remote_target) pairs.

    Normal mode: remote_path is a directory if there's more than one source
    or it ends in '/' (each file keeps its basename); otherwise it's an
    exact target path/rename, matching cp/scp semantics.

    --same mode: each source (already validated absolute) targets the
    identical path on the remote host, reviving the original mew -c
    behavior.
    """
    for f in local_files:
        if not os.path.isfile(f):
            die(f"not a regular file: {f}")

    if same:
        return [(f, f) for f in local_files]

    multi = len(local_files) > 1 or remote_path.endswith("/")
    return [
        (f, remote_path + os.path.basename(f) if multi else remote_path)
        for f in local_files
    ]


def rsync_argv(lf, spec, target, opts):
    argv = ["rsync"]
    if opts.verbose or opts.debug:
        argv.append("-v")
    argv += [lf, f"{spec}:{target}"]
    return argv


def plan_copy(host, targets, opts):
    spec = remote_spec(host, opts)
    lines = []
    for lf, target in targets:
        lines.append(shlex.join(rsync_argv(lf, spec, target, opts)))
        owner, group, mode = effective_owner_group_mode(lf, opts)
        fixup = f"chown {owner}:{group} {target}; chmod {mode:o} {target}"
        lines.append(shlex.join(["ssh", spec, fixup]))
    return "\n".join(lines)


def copy_host(host, targets, opts):
    spec = remote_spec(host, opts)
    output_parts = []
    for lf, target in targets:
        argv = rsync_argv(lf, spec, target, opts)
        debug_cmd(argv)
        proc = subprocess.run(argv, capture_output=True, text=True)
        if proc.returncode != 0:
            return proc.returncode, (proc.stdout + proc.stderr).strip()
        output_parts.append(proc.stdout)
        fixup_ownership(host, target, lf, opts)
    return 0, "".join(output_parts)


def copy_description(host, targets, opts):
    spec = remote_spec(host, opts)
    return "; ".join(f"{lf} -> {spec}:{target}" for lf, target in targets)


def do_copy(hosts, targets, opts):
    width = label_width(hosts)

    if opts.dry_run:
        for host in hosts:
            emit(host, plan_copy(host, targets, opts), opts.quiet, False, None, width, opts)
        return []

    failures = []

    def report_success(host, output):
        # rsync is silent on success unless --verbose/--debug added -v, in
        # which case output holds its real transfer log. Otherwise fall
        # back to a plain confirmation line so success isn't totally mute.
        text = output if output.strip() else f"copied {copy_description(host, targets, opts)}"
        emit(host, text, opts.quiet, False, opts.log_prefix, width, opts)

    if opts.parallel:
        max_workers = opts.jobs if opts.jobs else len(hosts)
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
            future_to_host = {
                ex.submit(copy_host, h, targets, opts): h for h in hosts
            }
            for fut in concurrent.futures.as_completed(future_to_host):
                host = future_to_host[fut]
                rc, result = fut.result()
                if rc != 0:
                    failures.append(host)
                    warn(f"{host}: rsync failed: {result}")
                else:
                    report_success(host, result)
        return failures

    state = {"yes_all": opts.yes}
    for host in hosts:
        desc = f"rsync {copy_description(host, targets, opts)}"
        if not confirm(host, desc, state, width):
            print(f"*** {host}: SKIPPED ***")
            continue
        rc, result = copy_host(host, targets, opts)
        if rc != 0:
            failures.append(host)
            warn(f"{host}: rsync failed: {result}")
            if not opts.continue_on_fail:
                warn("stopping (pass --continue-on-fail/-X to proceed past failures)")
                break
        else:
            report_success(host, result)
    return failures


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------

def plan_diff(host, local_file, opts):
    spec = remote_spec(host, opts)
    lines = [
        shlex.join(["scp", f"{spec}:{local_file}", "<tmpfile>"]),
        shlex.join(["diff", "-u", local_file, "<tmpfile>"]),
    ]
    return "\n".join(lines)


def diff_host(host, local_file, opts):
    spec = remote_spec(host, opts)
    fd, tmp_path = tempfile.mkstemp(prefix="pew-diff-")
    os.close(fd)
    try:
        scp_argv = ["scp", f"{spec}:{local_file}", tmp_path]
        debug_cmd(scp_argv)
        scp_proc = subprocess.run(scp_argv, capture_output=True, text=True)
        if scp_proc.returncode != 0:
            return 2, (scp_proc.stdout + scp_proc.stderr).strip()

        diff_argv = ["diff", "-u", local_file, tmp_path]
        debug_cmd(diff_argv)
        diff_proc = subprocess.run(diff_argv, capture_output=True, text=True)
        output = diff_proc.stdout.replace(tmp_path, f"{spec}:{local_file}")
        return diff_proc.returncode, output
    finally:
        os.unlink(tmp_path)


def do_diff(hosts, local_file, opts):
    if not os.path.isfile(local_file):
        die(f"not a regular file: {local_file}")

    width = label_width(hosts)

    if opts.dry_run:
        for host in hosts:
            emit(host, plan_diff(host, local_file, opts), opts.quiet, False, None, width)
        return []

    failures = []
    state = {"yes_all": opts.yes}
    for host in hosts:
        desc = f"diff {local_file} vs {remote_spec(host, opts)}:{local_file}"
        if not confirm(host, desc, state, width):
            print(f"*** {host}: SKIPPED ***")
            continue
        rc, output = diff_host(host, local_file, opts)
        if rc >= 2:
            failures.append(host)
            warn(f"{host}: {output}")
            if not opts.continue_on_fail:
                warn("stopping (pass --continue-on-fail/-X to proceed past failures)")
                break
            continue
        if not opts.quiet:
            print(f"==> {host} <==")
        if output:
            sys.stdout.write(output)
        elif not opts.quiet:
            print("(no differences)")
    return failures


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_octal_mode(value):
    try:
        mode = int(value, 8)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid octal mode: {value!r}")
    if not (0 <= mode <= 0o7777):
        raise argparse.ArgumentTypeError(f"mode out of range: {value!r}")
    return mode


def build_parser():
    # Host-selection flags: identical across every subcommand, including
    # `list`, so a --hosts value can be copy-pasted from one to another and
    # you can trust it resolves to exactly the same set of systems.
    filter_parent = argparse.ArgumentParser(add_help=False)
    filter_parent.add_argument(
        "--hosts", "-l", required=True, metavar="PATTERN",
        help="Ansible host pattern, e.g. webservers, 'web:&staging', host1,host2",
    )
    filter_parent.add_argument(
        "--inventory", "-i", metavar="PATH",
        help="Ansible inventory path (default: whatever ansible resolves itself)",
    )
    filter_parent.add_argument(
        "--pewrc", metavar="PATH",
        help="Use PATH instead of ~/.pewrc for inventory=/ansible_config=/"
             "timestamp_format= overrides",
    )
    filter_parent.add_argument(
        "--sort", "-r", action="store_true", help="Sort hosts before processing",
    )
    filter_parent.add_argument(
        "--where", action="append", type=parse_where, metavar="KEY<op>VALUE",
        help="Narrow --hosts to those matching an inventory variable or "
             "gathered Ansible fact, e.g. ansible_distribution=Ubuntu or "
             "ansible_mounts[].mount=/data. KEY is a dotted path; [] means "
             "'any element of this list', [N] a specific index. <op> is "
             "one of = != < > <= >= (numeric if both sides parse as "
             "numbers, string otherwise) or ~= (regex search) — quote the "
             "whole KEY<op>VALUE, most of these are shell-special. "
             "Repeatable (AND-ed together). Gathers facts fresh for the "
             "matched candidates (or reuses Ansible's own fact cache if "
             "fact_caching is configured).",
    )
    filter_parent.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print informational messages about what pew is doing, to stderr",
    )
    filter_parent.add_argument(
        "--debug", "-d", action="store_true",
        help="Like --verbose, plus raw data: exact commands run and "
             "pretty-printed --where fact-gathering output, to stderr",
    )
    # --remote-user/--root live here (not host_parent) because --where's fact
    # gathering opens SSH connections too, so even `list` needs this.
    user_group = filter_parent.add_mutually_exclusive_group()
    user_group.add_argument(
        "--remote-user", "-U", metavar="USER", default=None,
        help="SSH/SCP/rsync as USER instead of your local username. Applies to "
             "every matched host and to --where's fact-gathering — pew does "
             "not read ansible_user from inventory.",
    )
    user_group.add_argument(
        "--root", action="store_true",
        help="Shortcut for --remote-user root",
    )

    # Flags only meaningful for subcommands that actually act on hosts
    # (not `list`, which just resolves and prints).
    host_parent = argparse.ArgumentParser(add_help=False, parents=[filter_parent])
    host_parent.add_argument(
        "--yes", "-Z", action="store_true",
        help="Don't prompt per host; run on all matched hosts",
    )
    host_parent.add_argument(
        "--quiet", "-q", action="store_true", help="Suppress hostname labeling",
    )
    host_parent.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Show the command(s) that would run on each host, without running them",
    )

    exec_parent = argparse.ArgumentParser(add_help=False, parents=[host_parent])
    exec_parent.add_argument(
        "--parallel", "-P", action="store_true",
        help="Run across all matched hosts concurrently (implies --yes)",
    )
    exec_parent.add_argument(
        "--jobs", "-j", type=int, default=None, metavar="N",
        help="Max concurrent hosts with --parallel (default: all at once)",
    )
    exec_parent.add_argument(
        "--continue-on-fail", "-X", action="store_true",
        help="Keep going to remaining hosts after a failure (serial mode)",
    )
    exec_parent.add_argument(
        "--timeout", "-O", type=float, default=None, metavar="SECONDS",
        help="Per-host timeout",
    )
    exec_parent.add_argument(
        "--log-prefix", "-y", metavar="PREFIX",
        help="Also write each host's output to PREFIX.hostname",
    )
    timestamp_mode = exec_parent.add_mutually_exclusive_group()
    timestamp_mode.add_argument(
        "--timestamp", "-t", action="store_true",
        help="Prefix each terminal line with a timestamp "
             f"({repr(DEFAULT_TIMESTAMP_FORMAT).replace('%', '%%')}, or "
             "~/.pewrc's timestamp_format= if set). Never written to "
             "--log-prefix files, which stay clean.",
    )
    timestamp_mode.add_argument(
        "--strftime", type=parse_strftime, metavar="FORMAT",
        help="Like --timestamp, with a custom strftime format",
    )
    timestamp_mode.add_argument(
        "--ts", action="store_true",
        help="Matt's correct timestamp. Like --timestamp, shorthand for "
             "--strftime '%%Y%%m%%d%%H%%M%%S'",
    )
    timestamp_mode.add_argument(
        "--tss", action="store_true",
        help="Matt's correct timestamp, with subseconds. Like --timestamp, "
             "shorthand for --strftime '%%Y%%m%%d%%H%%M%%S.%%f' "
             "(microsecond precision)",
    )

    p = argparse.ArgumentParser(prog=PROG, description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="action", required=True)

    run_p = sub.add_parser("run", parents=[exec_parent],
                            help="Run a command on each host")
    output_mode = run_p.add_mutually_exclusive_group()
    output_mode.add_argument(
        "--multiline", "-m", action="store_true",
        help="Print a '### HOST ###' banner then output, instead of prefixing every line",
    )
    output_mode.add_argument(
        "--follow", "-f", action="store_true",
        help="Stream output line by line as it's produced, instead of waiting "
             "for each host to finish. Independent of --parallel — works "
             "serially too. Can't combine with --multiline, which needs the "
             "full buffered output to print its banner-then-block. If "
             "--log-prefix is also set, PREFIX.hostname is appended to live "
             "as well, so `tail -f` on it works while the host is still running.",
    )
    run_p.add_argument(
        "command", nargs=argparse.REMAINDER,
        help="Command to run. Use {} as a hostname placeholder. "
             "Put -- before it if it has its own flags.",
    )

    copy_p = sub.add_parser("copy", parents=[exec_parent],
                             help="Copy file(s) to each host, preserving mode "
                                  "and best-effort owner/group")
    copy_p.add_argument(
        "--owner", "-o", metavar="NAME",
        help="Set the remote file's owner (default: mirror the local file's owner)",
    )
    copy_p.add_argument(
        "--group", "-g", metavar="NAME",
        help="Set the remote file's group (default: mirror the local file's group)",
    )
    copy_p.add_argument(
        "--mode", "-m", type=parse_octal_mode, default=None, metavar="OCTAL",
        help="Set the remote file's mode, e.g. 640 (default: mirror the local file's mode)",
    )
    copy_p.add_argument(
        "--same", "-s", action="store_true",
        help="Push each SOURCE (must be an absolute path) to the identical "
             "path on the remote host; no DEST argument",
    )
    copy_p.add_argument(
        "paths", nargs="+", metavar="PATH",
        help="SOURCE... DEST — one or more local files followed by the remote "
             "destination path (a directory if there's more than one source, "
             "or if it ends in '/'). With --same: one or more absolute-path "
             "SOURCEs and no DEST.",
    )

    diff_p = sub.add_parser("diff", parents=[host_parent],
                             help="Diff a local file against the same path on each host")
    diff_p.add_argument(
        "--continue-on-fail", "-X", action="store_true",
        help="Keep going to remaining hosts after an error (e.g. scp failure)",
    )
    diff_p.add_argument("local_file", help="Local file to diff against each host")

    sub.add_parser("list", parents=[filter_parent],
                    help="Resolve and print hosts matching --hosts")

    facts_p = sub.add_parser("facts", parents=[filter_parent],
                              help="Dump gathered facts and inventory variables "
                                   "for each host, flattened to --where-compatible paths")
    facts_p.add_argument(
        "--match", action="append", type=parse_regex, metavar="REGEX",
        help="Only show rows where REGEX matches the key or the value. "
             "Repeatable (AND-ed together).",
    )
    facts_p.add_argument(
        "--match-keys", action="append", type=parse_regex, metavar="REGEX",
        help="Only show rows where REGEX matches the key path. "
             "Repeatable (AND-ed together, and with --match/--match-values).",
    )
    facts_p.add_argument(
        "--match-values", action="append", type=parse_regex, metavar="REGEX",
        help="Only show rows where REGEX matches the value. "
             "Repeatable (AND-ed together, and with --match/--match-keys).",
    )

    return p


def main():
    global VERBOSE, DEBUG, PEWRC_PATH
    try:
        parser = build_parser()
        opts = parser.parse_args()
        VERBOSE = opts.verbose
        DEBUG = opts.debug
        if opts.pewrc:
            if not os.path.isfile(opts.pewrc):
                warn(f"--pewrc path does not exist: {opts.pewrc}")
            PEWRC_PATH = opts.pewrc
        apply_ansible_config()

        hosts = resolve_hosts(opts.hosts, opts.inventory, opts.sort)
        if opts.where:
            hosts = filter_by_where(hosts, opts.where, opts)

        if opts.action == "list":
            for h in hosts:
                print(h)
            return 0

        if opts.action == "facts":
            unreachable = do_facts(hosts, opts)
            return 1 if unreachable else 0

        if opts.action == "run":
            command = opts.command
            if command and command[0] == "--":
                command = command[1:]
            if not command:
                die("no command specified")
            failures = do_run(hosts, command, opts)

        elif opts.action == "copy":
            if opts.same:
                if not opts.paths:
                    die("--same needs at least one source file")
                local_files, remote_path = opts.paths, None
                for f in local_files:
                    if not os.path.isabs(f):
                        die(f"--same requires absolute paths: {f}")
            else:
                if len(opts.paths) < 2:
                    die("copy needs at least one local file and a remote destination "
                        "path (SOURCE... DEST) — or pass --same to push each file to "
                        "its identical path on the remote host")
                local_files, remote_path = opts.paths[:-1], opts.paths[-1]
            targets = build_copy_targets(local_files, remote_path, opts.same)
            failures = do_copy(hosts, targets, opts)

        elif opts.action == "diff":
            failures = do_diff(hosts, opts.local_file, opts)

        if failures:
            warn(f"{len(failures)}/{len(hosts)} host(s) failed: {', '.join(sorted(failures))}")
            return 1
        return 0
    except KeyboardInterrupt:
        print()
        return 130

