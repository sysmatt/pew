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
import difflib
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

# Installed under one of these names (pipx entry point or a symlink to
# ./pew), pew behaves as if that subcommand had been typed first:
# `pes alpha` is `pew shell alpha`. Keyed on basename(argv[0]).
PROG_ALIASES = {"pes": "shell", "per": "run"}

# Subcommands whose first positional is HOST unless --hosts/-l is given
# (`pew run web uptime`). `-` as HOST means ~/.pewrc's hosts_default=.
# shell has its own HOST positional and isn't listed.
HOST_POSITIONAL = ("run", "copy", "diff", "list", "facts")
HOST_EPILOG = ("HOST is an Ansible host pattern, the same as --hosts. It's "
               "the first positional unless --hosts/-l is given; use - for "
               "~/.pewrc's hosts_default=.")


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


def determine_pewrc_path(cli_pewrc):
    """--pewrc flag > $PEW_PEWRC > ~/.pewrc (the default already in PEWRC_PATH)."""
    if cli_pewrc:
        return cli_pewrc
    if os.environ.get("PEW_PEWRC"):
        return os.environ["PEW_PEWRC"]
    return None


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


def determine_hosts(cli_hosts):
    """--hosts flag > ~/.pewrc [hosts_default=] > error."""
    if cli_hosts:
        debug(f"hosts from --hosts: {cli_hosts}")
        return cli_hosts
    from_rc = load_pewrc().get("hosts_default")
    if from_rc:
        debug(f"hosts from {PEWRC_PATH}: {from_rc}")
        return from_rc
    die("no HOST or --hosts/-l given, and no hosts_default= set in " + PEWRC_PATH)


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


def resolve_hosts(pattern, inventory=None, sort=False, allow_empty=False):
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
        if allow_empty:
            return []
        die(no_match_message(pattern, inventory))
    hosts = sorted(hosts) if sort else hosts
    info(f"resolved {len(hosts)} host(s)")
    debug(f"host list: {', '.join(hosts)}")
    return hosts


SUGGEST_LIMIT = 10


def pattern_terms(pattern):
    """The plain names in an Ansible pattern, for suggestions: split on
    `,`/`:`, minus `&`/`!` prefixes, glob characters, `[n:m]` ranges, and
    `~regex` terms (which aren't names at all). One-letter terms are
    dropped too: as substrings they match nearly everything."""
    terms = []
    for term in re.split(r"[,:]", re.sub(r"\[[^\]]*\]", "", pattern)):
        term = term.lstrip("&!")
        if term.startswith("~"):
            continue
        term = re.sub(r"[*?]", "", term)
        if len(term) >= 2 and term != "all":
            terms.append(term)
    return terms


def similar_names(needles, names):
    """Names resembling any needle, case-insensitively: substring matches
    first, then close spellings (difflib) of the whole name or any of its
    `.`/`-`/`_`-separated parts (`aplha` -> alpha-rtr.example.com)."""
    keys = {}
    for name in names:
        lower = name.lower()
        for key in [lower] + [p for p in re.split(r"[._-]", lower) if len(p) >= 3]:
            keys.setdefault(key, set()).add(name)
    found = {}
    for needle in needles:
        n = needle.lower()
        for name in names:
            if n in name.lower():
                found.setdefault(name)
        scored = {}
        for key, owners in keys.items():
            ratio = difflib.SequenceMatcher(None, n, key).ratio()
            if ratio >= 0.6:
                for name in owners:
                    scored[name] = max(ratio, scored.get(name, 0))
        for name in sorted(scored, key=lambda x: (-scored[x], x)):
            found.setdefault(name)
    return list(found)


def no_match_message(pattern, inventory):
    """The `matched no hosts` error, plus hosts and groups resembling the
    pattern's unknown names, if there are any."""
    msg = f"host pattern {pattern!r} matched no hosts"
    data = load_inventory_json(inventory)
    groups = [g for g in data if g not in ("_meta", "all", "ungrouped")]
    hosts = set(data.get("_meta", {}).get("hostvars", {}))
    for g in groups + ["ungrouped"]:
        hosts.update(data[g].get("hosts", []) if isinstance(data.get(g), dict) else [])
    hosts = sorted(hosts)
    known = set(hosts) | set(groups)
    needles = [t for t in pattern_terms(pattern) if t not in known]
    for label, names in (("hosts", hosts), ("groups", sorted(groups))):
        similar = similar_names(needles, names)
        if not similar:
            continue
        msg += f"\nsimilar {label}:"
        msg += "".join(f"\n  {n}" for n in similar[:SUGGEST_LIMIT])
        if len(similar) > SUGGEST_LIMIT:
            msg += f"\n  ... and {len(similar) - SUGGEST_LIMIT} more"
    return msg


def group_members(data, group, seen=None):
    """Every host in group, its child groups' included (recursively), the
    same set `ansible GROUP --list-hosts` resolves to."""
    seen = set() if seen is None else seen
    if group in seen or not isinstance(data.get(group), dict):
        return set()
    seen.add(group)
    hosts = set(data[group].get("hosts", []))
    for child in data[group].get("children", []):
        hosts |= group_members(data, child, seen)
    return hosts


def do_groups(opts):
    """One line per inventory group: `group : host host ...`. `all` is
    left out (it's everything), and `ungrouped` unless it has hosts."""
    data = load_inventory_json(opts.inventory)
    if not data:
        die("could not read inventory (rerun with --debug to see ansible-inventory's output)")
    groups = {g: sorted(group_members(data, g))
              for g in data if g not in ("_meta", "all")}
    if not groups.get("ungrouped"):
        groups.pop("ungrouped", None)
    width = label_width(groups)
    for g in sorted(groups):
        print(f"{g:<{width}} : {' '.join(groups[g])}".rstrip())
    return 0


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
    r"^(?P<host>\S+) \| (?P<status>\S+)(?: \| rc=\d+)? => ", re.MULTILINE,
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
    cmd += extra_vars_args(opts)
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


def load_inventory_json(inventory):
    """`ansible-inventory --list` as parsed JSON, or {} if it isn't."""
    inventory = determine_inventory(inventory)
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
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        return {}


def gather_inventory_vars(hosts, opts):
    """Returns {host: {var: value, ...}} from Ansible inventory itself
    (host_vars/group_vars/inline vars) — NOT gathered facts. Cheap: just a
    config read, no SSH connection needed, so always fetched whenever
    --where is used regardless of whether any clause actually needs it.
    """
    data = load_inventory_json(opts.inventory)
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


def determine_extra_vars(cli_extra_vars):
    """~/.pewrc [extra_vars=] first, then each --extra-vars/-e in order, so
    a command-line value wins (Ansible applies repeated -e left to right)."""
    extra_vars = []
    from_rc = load_pewrc().get("extra_vars")
    if from_rc:
        debug(f"extra vars from {PEWRC_PATH}: {from_rc}")
        extra_vars.append(from_rc)
    extra_vars += cli_extra_vars or []
    return extra_vars


def extra_vars_args(opts):
    return [a for ev in opts.extra_vars for a in ("-e", ev)]


# Ansible connection variables pew honors, as the Jinja expression rendered
# for each. Rendered by Ansible itself (not read raw from ansible-inventory,
# which returns templated values like ansible_host unrendered), so group_vars
# logic such as `ansible_host: "{{ ... if connect_via_tailnet ... }}"` and
# -e overrides resolve exactly as they would for a playbook.
CONNECTION_VARS = {
    "address": "ansible_host | default(inventory_hostname)",
    "port": "ansible_port | default('')",
    "user": "ansible_user | default('')",
    "key": "ansible_ssh_private_key_file | default('')",
    "common_args": "ansible_ssh_common_args | default('')",
    "extra_args": "ansible_ssh_extra_args | default('')",
}


def resolve_connections(hosts, opts):
    """Returns {host: {address, port, user, key, common_args, extra_args}}
    rendered per host via a local `ansible -m debug` (no SSH connection).

    A host whose variables fail to render (e.g. an undefined variable in
    its ansible_host template) is warned about and falls back to dialing
    its inventory name directly, with no extra options.
    """
    inventory = determine_inventory(opts.inventory)
    info(f"resolving connection variables for {len(hosts)} host(s)")
    template = ("{{ {"
                + ", ".join(f"'{k}': {expr}" for k, expr in CONNECTION_VARS.items())
                + "} | to_json }}")
    cmd = ["ansible", ",".join(hosts), "-m", "debug", "-a", f'msg="{template}"']
    if inventory:
        cmd += ["-i", inventory]
    cmd += extra_vars_args(opts)
    debug_cmd(cmd)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        die("'ansible' not found on PATH. Is ansible-core installed?")
    debug_block("ansible -m debug stdout", proc.stdout)
    debug_block("ansible -m debug stderr", proc.stderr)

    decoder = json.JSONDecoder()
    connections = {}
    errors = {}
    for m in ANSIBLE_RESULT_MARKER_RE.finditer(proc.stdout):
        host = m.group("host")
        try:
            data, _ = decoder.raw_decode(proc.stdout, m.end())
        except json.JSONDecodeError:
            continue
        msg = data.get("msg")
        if m.group("status") != "SUCCESS" or msg is None:
            errors[host] = msg
            continue
        try:
            conn = json.loads(msg) if isinstance(msg, str) else msg
        except json.JSONDecodeError:
            errors[host] = f"unparseable result: {msg!r}"
            continue
        connections[host] = {k: "" if v is None else str(v) for k, v in conn.items()}

    for host in hosts:
        if host not in connections:
            warn(f"{host}: could not render connection variables, dialing "
                 f"inventory name directly: {errors.get(host) or 'no result from ansible'}")
            connections[host] = {"address": host}
    debug_json("connections", connections)
    return connections


def host_connection(host, opts):
    connections = getattr(opts, "connections", None) or {}
    return connections.get(host) or {"address": host}


def remote_spec(host, opts, munge_user=False):
    """Build the ssh/scp/rsync connection target for a host: its rendered
    ansible_host as [user@]address. --remote-user/--root override
    ansible_user uniformly across the whole invocation.

    munge_user drops a `+...` suffix from the user: MikroTik reads console
    options from it (admin+ct), and those are wrong for an interactive
    session. `shell` asks for this unless --no-munge-username.
    """
    conn = host_connection(host, opts)
    user = effective_remote_user(opts) or conn.get("user")
    if munge_user and user and "+" in user:
        munged = user.split("+", 1)[0]
        debug(f"username {user!r} munged to {munged!r} (--no-munge-username to keep it)")
        user = munged
    address = conn.get("address") or host
    return f"{user}@{address}" if user else address


def ssh_options(host, opts, tool="ssh"):
    """ssh/scp options from a host's rendered Ansible connection variables.
    Port goes in as -o Port= since ssh and scp spell the flag differently
    (-p vs -P). Like Ansible, ansible_ssh_extra_args applies to ssh only
    (rsync's transport counts as ssh), not scp."""
    conn = host_connection(host, opts)
    options = []
    if conn.get("port"):
        options += ["-o", f"Port={conn['port']}"]
    if conn.get("key"):
        options += ["-i", os.path.expanduser(conn["key"])]
    options += shlex.split(conn.get("common_args") or "")
    if tool == "ssh":
        options += shlex.split(conn.get("extra_args") or "")
    return options


def ssh_argv(host, opts, munge_user=False):
    return ["ssh"] + ssh_options(host, opts) + [remote_spec(host, opts, munge_user)]


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def substitute(args, host):
    return [a.replace("{}", host) for a in args]


def run_host(host, args, opts):
    argv = ssh_argv(host, opts) + substitute(args, host)
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
    argv = ssh_argv(host, opts) + substitute(args, host)
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
    return shlex.join(ssh_argv(host, opts) + substitute(args, host))


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
    fixup_argv = ssh_argv(host, opts) + [remote_script]
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


def rsync_argv(host, lf, target, opts):
    argv = ["rsync"]
    if opts.verbose or opts.debug:
        argv.append("-v")
    options = ssh_options(host, opts)
    if options:
        argv += ["-e", shlex.join(["ssh"] + options)]
    argv += [lf, f"{remote_spec(host, opts)}:{target}"]
    return argv


def plan_copy(host, targets, opts):
    lines = []
    for lf, target in targets:
        lines.append(shlex.join(rsync_argv(host, lf, target, opts)))
        owner, group, mode = effective_owner_group_mode(lf, opts)
        fixup = f"chown {owner}:{group} {target}; chmod {mode:o} {target}"
        lines.append(shlex.join(ssh_argv(host, opts) + [fixup]))
    return "\n".join(lines)


def copy_host(host, targets, opts):
    output_parts = []
    for lf, target in targets:
        argv = rsync_argv(host, lf, target, opts)
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

def scp_argv(host, src, dst, opts):
    return ["scp"] + ssh_options(host, opts, tool="scp") + [src, dst]


def plan_diff(host, local_file, opts):
    spec = remote_spec(host, opts)
    lines = [
        shlex.join(scp_argv(host, f"{spec}:{local_file}", "<tmpfile>", opts)),
        shlex.join(["diff", "-u", local_file, "<tmpfile>"]),
    ]
    return "\n".join(lines)


def diff_host(host, local_file, opts):
    spec = remote_spec(host, opts)
    fd, tmp_path = tempfile.mkstemp(prefix="pew-diff-")
    os.close(fd)
    try:
        argv = scp_argv(host, f"{spec}:{local_file}", tmp_path, opts)
        debug_cmd(argv)
        scp_proc = subprocess.run(argv, capture_output=True, text=True)
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
# shell
# ---------------------------------------------------------------------------

PLAIN_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def match_plain_name(name, inventory):
    """Hosts a plain name (no pattern syntax) refers to, most specific
    first: short name (`alpha` -> alpha.example.com, the same convention
    as `inventory_hostname.split('.')[0]`), else case-insensitive
    substring (`qth0` -> qth0-router.example.com). The first stage with
    any match wins, so a short-name hit is never made ambiguous by
    substring hits.

    Matched in Python against every inventory host rather than with an
    Ansible glob like `*NAME*`, which would match group names too and
    could silently select a group's hosts that don't contain NAME.
    """
    all_hosts = resolve_hosts("all", inventory, sort=True, allow_empty=True)
    needle = name.lower()
    short = [h for h in all_hosts if h.lower().startswith(needle + ".")]
    if short:
        info(f"{name!r} matched {len(short)} host(s) as a short name")
        return short
    substring = [h for h in all_hosts if needle in h.lower()]
    info(f"{name!r} matched {len(substring)} host(s) as a substring")
    return substring


def resolve_shell_host(pattern, opts):
    """Resolve pattern (plus --where) to exactly one host, or die.

    A plain name that matches nothing as an Ansible pattern falls back to
    match_plain_name(); anything with pattern syntax is taken as written.
    """
    hosts = resolve_hosts(pattern, opts.inventory, sort=True, allow_empty=True)
    fuzzy = False
    if not hosts and PLAIN_NAME_RE.match(pattern):
        hosts = match_plain_name(pattern, opts.inventory)
        fuzzy = True
    if not hosts:
        die(no_match_message(pattern, opts.inventory))
    if opts.where:
        hosts = filter_by_where(hosts, opts.where, opts)
    if len(hosts) != 1:
        listing = "\n".join(f"  {h}" for h in hosts)
        die(f"{pattern!r} matched {len(hosts)} hosts; shell needs exactly one:\n"
            f"{listing}\nnarrow the pattern (or use --where) to a single host")
    if fuzzy:
        print(f"{PROG}: {pattern} → {hosts[0]}", file=sys.stderr)
    return hosts[0]


def do_shell(host, command, opts):
    """Replace this process with ssh to host, so ssh owns the terminal,
    signals, and exit code outright. With a command, -t forces a TTY for
    it (the thing `run`, which captures output, can't give you)."""
    argv = ssh_argv(host, opts, munge_user=not opts.no_munge_username)
    if command:
        argv = argv[:1] + ["-t"] + argv[1:] + substitute(command, host)
    if opts.dry_run:
        print(shlex.join(argv))
        return 0
    debug_cmd(argv)
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvp(argv[0], argv)
    except FileNotFoundError:
        die("ssh not found on PATH")


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
    # Inventory-only flags, shared by everything including `groups`, which
    # reads the inventory but never resolves a host pattern.
    inventory_parent = argparse.ArgumentParser(add_help=False)
    inventory_parent.add_argument(
        "--inventory", "-i", metavar="PATH",
        help="Ansible inventory path (default: whatever ansible resolves itself)",
    )
    inventory_parent.add_argument(
        "--pewrc", metavar="PATH",
        help="Use PATH instead of ~/.pewrc for inventory=/ansible_config=/"
             "hosts_default=/timestamp_format=/extra_vars= overrides. Same as "
             "$PEW_PEWRC, which this takes precedence over.",
    )
    inventory_parent.add_argument(
        "--verbose", "-v", action="store_true",
        help="Print informational messages about what pew is doing, to stderr",
    )
    inventory_parent.add_argument(
        "--debug", "-d", action="store_true",
        help="Like --verbose, plus raw data: exact commands run and "
             "pretty-printed --where fact-gathering output, to stderr",
    )

    filter_parent = argparse.ArgumentParser(add_help=False, parents=[inventory_parent])
    filter_parent.add_argument(
        "--hosts", "-l", metavar="PATTERN",
        help="Ansible host pattern, e.g. webservers, 'web:&staging', host1,host2 "
             "(default: ~/.pewrc's hosts_default=, if set)",
    )
    filter_parent.add_argument(
        "--extra-vars", "-e", action="append", metavar="VARS",
        help="Pass through to every ansible call as -e VARS (repeatable), "
             "e.g. -e connect_via_tailnet=true. Affects how inventory "
             "connection variables like ansible_host render, and --where "
             "fact-gathering. Appended after ~/.pewrc's extra_vars=, so "
             "these win.",
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
    # --remote-user/--root live here (not host_parent) because --where's fact
    # gathering opens SSH connections too, so even `list` needs this.
    user_group = filter_parent.add_mutually_exclusive_group()
    user_group.add_argument(
        "--remote-user", "-U", metavar="USER", default=None,
        help="SSH/SCP/rsync as USER instead of each host's ansible_user "
             "(or your local username if it has none). Applies to every "
             "matched host and to --where's fact-gathering.",
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
                            usage="%(prog)s [options] HOST [--] COMMAND...",
                            epilog=HOST_EPILOG,
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
                             usage="%(prog)s [options] HOST SOURCE... DEST",
                             epilog=HOST_EPILOG,
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
                             usage="%(prog)s [options] HOST LOCAL_FILE",
                             epilog=HOST_EPILOG,
                             help="Diff a local file against the same path on each host")
    diff_p.add_argument(
        "--continue-on-fail", "-X", action="store_true",
        help="Keep going to remaining hosts after an error (e.g. scp failure)",
    )
    diff_p.add_argument("local_file", help="Local file to diff against each host")

    sub.add_parser("list", parents=[filter_parent],
                    usage="%(prog)s [options] [HOST]",
                    epilog=HOST_EPILOG,
                    help="Resolve and print hosts matching HOST/--hosts")

    sub.add_parser("groups", parents=[inventory_parent],
                    usage="%(prog)s [options]",
                    help="List every inventory group and the hosts it contains")

    facts_p = sub.add_parser("facts", parents=[filter_parent],
                              usage="%(prog)s [options] [HOST]",
                              epilog=HOST_EPILOG,
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

    shell_p = sub.add_parser("shell", parents=[filter_parent],
                              usage="%(prog)s [options] HOST [-- COMMAND...]",
                              help="Open an interactive ssh session to exactly one host",
                              epilog="Anything after -- is run with a TTY (ssh -t) "
                                     "instead of a login shell; {} is the hostname "
                                     "placeholder.")
    shell_p.add_argument(
        "--dry-run", "-n", action="store_true",
        help="Print the ssh command instead of running it",
    )
    shell_p.add_argument(
        "--no-munge-username", action="store_true",
        help="Keep a +suffix on the username. By default shell drops it "
             "(admin+ct -> admin): MikroTik reads console options from it, "
             "which suit scripted runs but not an interactive session.",
    )
    shell_p.add_argument(
        "target", nargs="?", metavar="HOST",
        help="Ansible host pattern that must resolve to exactly one host "
             "(same as --hosts). A plain name matching nothing falls back to "
             "a short-name match (alpha -> alpha.example.com), then a "
             "case-insensitive substring match (qth0 -> qth0-router.example.com).",
    )
    p.subcommands = list(sub.choices)
    p.subparsers = sub.choices
    return p


def scan_option(token, subparser):
    """For an option token, return (is_hosts, consumes_next): whether it is
    --hosts/-l, and whether its value is the next token. Mirrors argparse:
    --opt=value, unique long-option prefixes, -xVALUE, and bundled short
    flags (-qP). Unknown options take no value; argparse reports them."""
    actions = subparser._option_string_actions
    if token.startswith("--"):
        name, eq, _ = token.partition("=")
        action = actions.get(name)
        if action is None:
            matches = {a for o, a in actions.items() if o.startswith(name)}
            action = matches.pop() if len(matches) == 1 else None
        if action is None:
            return False, False
        return action.dest == "hosts", action.nargs != 0 and not eq
    for i, ch in enumerate(token[1:], start=1):
        action = actions.get("-" + ch)
        if action is None:
            return False, False
        if action.nargs != 0:
            return action.dest == "hosts", i == len(token) - 1
    return False, False


def extract_host_positional(argv, parser):
    """Turn a leading HOST positional into --hosts HOST, so argparse never
    sees it (`run web -P uptime` -> `run --hosts web -P uptime`). Options may
    sit before or after HOST. With --hosts/-l anywhere before it there is no
    HOST positional; giving both is an error. HOST `-` is dropped, leaving
    ~/.pewrc's hosts_default= to apply. Nothing after `--` is ever HOST.
    For run, the next positional after HOST starts the command, and
    everything from there on is the command's, options included."""
    subparser = parser.subparsers[argv[0]]
    hosts_flag = False
    host_idx = None
    i = 1
    while i < len(argv):
        token = argv[i]
        if token == "--":
            break
        if token.startswith("-") and token != "-":
            is_hosts, consumes_next = scan_option(token, subparser)
            if is_hosts:
                if host_idx is not None:
                    die("give hosts either as HOST or --hosts/-l, not both")
                hosts_flag = True
            i += 2 if consumes_next else 1
            continue
        if hosts_flag or (host_idx is not None and argv[0] == "run"):
            break
        if host_idx is None:
            host_idx = i
        i += 1
    if host_idx is None:
        return argv
    host = argv[host_idx]
    rest = argv[1:host_idx] + argv[host_idx + 1:]
    if host == "-":
        debug("HOST '-': using hosts_default=")
        return argv[:1] + rest
    return argv[:1] + ["--hosts", host] + rest


def expand_subcommand(word, names):
    """Expand a unique prefix of a subcommand name (`r` -> run, `sh` ->
    shell). An exact name always wins; an ambiguous prefix is an error,
    never a guess. Anything else (options like -h, unknown words) is
    returned as-is for argparse to handle."""
    if not word or word in names or word.startswith("-"):
        return word
    candidates = [n for n in names if n.startswith(word)]
    if len(candidates) > 1:
        die(f"subcommand {word!r} is ambiguous: {', '.join(sorted(candidates))}")
    if candidates:
        debug(f"subcommand {word!r} expanded to {candidates[0]!r}")
        return candidates[0]
    return word


def main():
    global VERBOSE, DEBUG, PEWRC_PATH
    try:
        parser = build_parser()
        # shell's command is split off at the first -- before argparse sees
        # it: a REMAINDER positional after HOST would swallow any options
        # given after HOST (`pew shell web1 --root -- sudo -i`). The
        # subcommand is always argv[0], the top-level parser has no options.
        argv = sys.argv[1:]
        alias = PROG_ALIASES.get(os.path.basename(sys.argv[0]))
        if alias:
            argv.insert(0, alias)
        if argv:
            argv[0] = expand_subcommand(argv[0], parser.subcommands)
        if argv[:1] and argv[0] in HOST_POSITIONAL:
            argv = extract_host_positional(argv, parser)
        shell_command = []
        if argv[:1] == ["shell"] and "--" in argv:
            split = argv.index("--")
            argv, shell_command = argv[:split], argv[split + 1:]
        opts = parser.parse_args(argv)
        VERBOSE = opts.verbose
        DEBUG = opts.debug
        pewrc_override = determine_pewrc_path(opts.pewrc)
        if pewrc_override:
            if not os.path.isfile(pewrc_override):
                warn(f"--pewrc/$PEW_PEWRC path does not exist: {pewrc_override}")
            PEWRC_PATH = pewrc_override
        apply_ansible_config()

        if opts.action == "groups":
            return do_groups(opts)

        opts.extra_vars = determine_extra_vars(opts.extra_vars)

        if opts.action == "shell":
            # No hosts_default= fallback: a shell names its target explicitly.
            if opts.target and opts.hosts:
                die("give the host either as HOST or --hosts/-l, not both")
            pattern = opts.target or opts.hosts
            if not pattern:
                die("shell needs a HOST (or --hosts/-l)")
            host = resolve_shell_host(pattern, opts)
            opts.connections = resolve_connections([host], opts)
            return do_shell(host, shell_command, opts)

        opts.hosts = determine_hosts(opts.hosts)

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

        opts.connections = resolve_connections(hosts, opts)

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

