"""pew — run commands, push files, and diff files across systems.

Host selection is delegated entirely to Ansible: a --hosts pattern is
resolved via `ansible <pattern> --list-hosts`, so any pattern Ansible
understands (group names, `web:&staging`, `web:!excluded`, `host1,host2`,
...) works here too. No host/group data is stored or parsed by this tool.

Spiritual successor to the old Perl "mew" tool. Not flag-compatible with it.
"""

import argparse
import collections
import concurrent.futures
import contextlib
import datetime
import difflib
import errno
import grp
import json
import math
import os
import pwd
import queue
import re
import selectors
import shlex
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata

PROG = "pew"

# Installed under one of these names (pipx entry point or a symlink to
# ./pew), pew behaves as if these words had been typed first:
# `pes alpha` is `pew shell alpha`. Keyed on basename(argv[0]).
PROG_ALIASES = {"pes": ["shell"], "per": ["run"], "pez": ["run", "-P", "-Z"]}

# Aliases that also turn on --color/--pretty/--tty, softly: like ~/.pewrc's
# color=yes/pretty=yes/tty=yes rather than a literal flag, so --follow/
# --quiet/--no-color/--no-pretty/--no-tty still work.
ALIAS_DEFAULTS = {"pez": {"color", "pretty", "tty"}}

# Subcommands whose first positional is HOST unless --hosts/-l is given
# (`pew run web uptime`). `-` as HOST means ~/.pewrc's hosts_default=.
# shell has its own HOST positional and isn't listed.
HOST_POSITIONAL = ("run", "copy", "diff", "list", "facts", "ping")
HOST_EPILOG = ("HOST is an Ansible host pattern, the same as --hosts. It's "
               "the first positional unless --hosts/-l is given; use - for "
               "~/.pewrc's hosts_default=.")


def out(text=""):
    """print() for stdout, landing above the --pretty footer if it's up."""
    if LIVE:
        LIVE.write(text + "\n", sys.stdout)
    else:
        print(text)


def err(text):
    if LIVE:
        LIVE.write(text + "\n", sys.stderr)
    else:
        print(text, file=sys.stderr)


def die(msg, code=1):
    err(f"{PROG}: error: {msg}")
    sys.exit(code)


def warn(msg):
    err(f"{PROG}: warning: {msg}")


# ---------------------------------------------------------------------------
# --color: ANSI hostname coloring for `run` (green rc 0, red non-zero).
# ---------------------------------------------------------------------------

GREEN = "\033[32m"
RED = "\033[31m"
RESET = "\033[0m"
TRUTHY = {"1", "yes", "true", "on"}


def paint(text, rc, enabled):
    if not enabled or rc is None:
        return text
    return f"{GREEN if rc == 0 else RED}{text}{RESET}"


def determine_color(opts, default=False):
    """--color/--no-color > $NO_COLOR > ~/.pewrc [color=] or default (a
    ALIAS_DEFAULTS name) > off. An explicit --color is rejected with
    --follow/--quiet; a default from ~/.pewrc or the alias is just quietly
    dropped there, so it doesn't break every --follow run.
    Either way it then only applies if stdout is actually a terminal."""
    if opts.color:
        if opts.follow:
            die("--color/-C can't be combined with --follow/-f")
        if opts.quiet:
            die("--color/-C can't be combined with --quiet/-q")
        wanted = True
    elif opts.color is False or "NO_COLOR" in os.environ:
        wanted = False
    else:
        wanted = ((default or load_pewrc().get("color", "").lower() in TRUTHY)
                  and not opts.follow and not opts.quiet)
    return wanted and sys.stdout.isatty()


def failure_detail(rc, note):
    return f"{rc}, {note}" if note else str(rc)


# ---------------------------------------------------------------------------
# --pretty: a live status footer on stderr while run/copy work, erased when
# they finish and replaced by one final status line. Everything printed
# while it's up goes through out()/err(), which put it above the footer.
# ---------------------------------------------------------------------------

CYAN = "\033[36m"
DIM = "\033[2m"

# "unicode" sticks to single-column characters so the footer's cursor math
# holds in any terminal; "emoji" is prettier but double-width, which some
# terminals/tmux/fonts disagree on.
PRETTY_STYLES = {
    "unicode": {"spin": "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏", "ok": "✔", "fail": "✘",
                "wait": "◌", "skip": "⊘", "kill": "■"},
    "emoji": {"spin": "🕐🕑🕒🕓🕔🕕🕖🕗🕘🕙🕚🕛", "ok": "✅", "fail": "❌",
              "wait": "⏳", "skip": "🚫", "kill": "🛑"},
}

LIVE = None


def char_width(ch):
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def format_elapsed(seconds):
    if seconds < 60:
        return f"{seconds:.1f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


class Live:
    """Per-host state (wait -> run -> ok/fail, or skip; a host still
    running when the run is interrupted ends up as kill), redrawn ~10x a
    second below whatever's been printed. The footer is drawn with no
    trailing newline, so erasing it is: clear this line, then up-and-clear
    once per extra line."""

    def __init__(self, hosts, style, color, keep_order=False):
        self.hosts = list(hosts)
        self.state = dict.fromkeys(self.hosts, "wait")
        self.started = {}
        # --sort: finished hosts whose output is still held back behind a
        # slower host earlier in the order.
        self.keep_order = keep_order
        self.printed = set()
        self.sym = PRETTY_STYLES[style]
        self.color = color
        self.lock = threading.RLock()
        self.drawn = 0
        self.paused = False
        self.interrupted = False
        self.frame = 0
        self.t0 = time.monotonic()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._tick, daemon=True)

    def __enter__(self):
        sys.stderr.write("\033[?25l")
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, tb):
        global LIVE
        self._stop.set()
        self._thread.join()
        with self.lock:
            LIVE = None
            if exc_type is KeyboardInterrupt or self.interrupted:
                head = "interrupted after"
            elif exc_type is not None:
                head = "stopped after"
            else:
                head = "done in"
            sys.stderr.write(self._erase() + "\033[?25h"
                             + self._final_line(head) + "\n")
            sys.stderr.flush()
        return False

    def running(self, host):
        with self.lock:
            self.state[host] = "run"
            self.started[host] = time.monotonic()

    def done(self, host, rc):
        with self.lock:
            if self.interrupted:
                self.state[host] = "kill"
            else:
                self.state[host] = "ok" if rc == 0 else "fail"

    def released(self, host):
        with self.lock:
            self.printed.add(host)

    def skipped(self, host):
        with self.lock:
            self.state[host] = "skip"

    def interrupt(self):
        with self.lock:
            self.interrupted = True

    def write(self, text, stream):
        with self.lock:
            sys.stderr.write(self._erase())
            sys.stderr.flush()
            stream.write(text)
            stream.flush()
            sys.stderr.write(self._render())
            sys.stderr.flush()

    @contextlib.contextmanager
    def pause(self):
        """Footer off while the confirm prompt waits for an answer."""
        with self.lock:
            sys.stderr.write(self._erase() + "\033[?25h")
            sys.stderr.flush()
            self.paused = True
        try:
            yield
        finally:
            with self.lock:
                self.paused = False
                sys.stderr.write("\033[?25l" + self._render())
                sys.stderr.flush()

    def _tick(self):
        while not self._stop.wait(0.1):
            with self.lock:
                if self.paused:
                    continue
                self.frame += 1
                sys.stderr.write(self._erase() + self._render())
                sys.stderr.flush()

    def _erase(self):
        if not self.drawn:
            return ""
        seq = "\r\033[2K" + "\033[1A\033[2K" * (self.drawn - 1)
        self.drawn = 0
        return seq

    def _render(self):
        if self.paused:
            return ""
        try:
            cols, rows = os.get_terminal_size(sys.stderr.fileno())
        except OSError:
            cols = rows = 0
        cols, rows = cols or 80, rows or 24  # a pty that never got a size
        # At most half the screen, so results stay visible above it.
        lines = self._lines(max(cols - 1, 20), max(rows // 2, 4))
        self.drawn = len(lines)
        return "\n".join(lines)

    def _paint(self, color, text):
        return f"{color}{text}{RESET}" if self.color and color else text

    def _fit(self, segments, width):
        """Join (text, color) segments, cut off at width display columns."""
        parts, used = [], 0
        for text, color in segments:
            piece = ""
            for ch in text:
                w = char_width(ch)
                if used + w > width:
                    break
                piece += ch
                used += w
            if piece:
                parts.append(self._paint(color, piece))
            if len(piece) < len(text):
                break
        return "".join(parts)

    def _lines(self, width, max_lines):
        sym, now = self.sym, time.monotonic()
        spin = sym["spin"][self.frame % len(sym["spin"])]
        running = [h for h in self.hosts if self.state[h] == "run"]
        waiting = [h for h in self.hosts if self.state[h] == "wait"]
        counts = collections.Counter(self.state.values())

        lines = [self._fit([("─" * 60, DIM)], width)]
        room = max_lines - 1 - (1 if waiting else 0) - 1
        shown = running if len(running) <= room else running[:max(room - 1, 0)]
        hw = label_width(shown)
        for h in shown:
            lines.append(self._fit([(f"{spin} ", CYAN), (f"{h:<{hw}}  ", None),
                                    (format_elapsed(now - self.started[h]), DIM)], width))
        if len(shown) < len(running):
            lines.append(self._fit([(f"{spin} +{len(running) - len(shown)} more running", CYAN)], width))
        if waiting:
            lines.append(self._fit([(f"{sym['wait']} waiting: ", DIM),
                                    (self._name_list(waiting, width - 12), None)], width))

        segs = [(f"{sym['ok']} {counts['ok']} ok", GREEN), ("  ", None),
                (f"{sym['fail']} {counts['fail']} failed", RED if counts["fail"] else None)]
        held = sum(1 for h in self.hosts
                   if self.state[h] in ("ok", "fail") and h not in self.printed)
        if self.keep_order and held:
            segs += [("  ", None), (f"({held} held)", DIM)]
        if counts["skip"]:
            segs += [("  ", None), (f"{sym['skip']} {counts['skip']} skipped", DIM)]
        segs += [("  ", None), (f"{spin} {len(running)} running", CYAN),
                 ("  ", None), (f"{sym['wait']} {len(waiting)} waiting", DIM),
                 ("  ", None), (format_elapsed(now - self.t0), DIM)]
        lines.append(self._fit(segs, width))
        return lines

    @staticmethod
    def _name_list(names, width):
        """As many names as fit in width columns, then (+N) for the rest."""
        shown = []
        for i, name in enumerate(names):
            rest = len(names) - i - 1
            candidate = ", ".join(shown + [name]) + (f" (+{rest})" if rest else "")
            if shown and len(candidate) > width:
                break
            shown.append(name)
        rest = len(names) - len(shown)
        return ", ".join(shown) + (f" (+{rest})" if rest else "")

    def _final_line(self, head):
        sym = self.sym
        counts = collections.Counter(self.state.values())
        killed = counts["kill"] + counts["run"]
        parts = [self._paint(GREEN, f"{sym['ok']} {counts['ok']} ok"),
                 self._paint(RED if counts["fail"] else None,
                             f"{sym['fail']} {counts['fail']} failed")]
        if counts["skip"]:
            parts.append(f"{sym['skip']} {counts['skip']} skipped")
        if killed:
            parts.append(self._paint(RED, f"{sym['kill']} {killed} killed"))
        if counts["wait"]:
            parts.append(f"{sym['wait']} {counts['wait']} not run")
        elapsed = format_elapsed(time.monotonic() - self.t0)
        return f"{PROG}: {head} {elapsed}: " + "  ".join(parts)


def determine_pretty(opts, default=False):
    """--pretty/--no-pretty > ~/.pewrc [pretty=] or default (an
    ALIAS_DEFAULTS name) > off. Then only if stderr, where the footer
    goes, is a terminal, and never for --dry-run, which has nothing to
    watch."""
    if opts.pretty is None:
        wanted = default or load_pewrc().get("pretty", "").lower() in TRUTHY
    else:
        wanted = opts.pretty
    return wanted and not opts.dry_run and sys.stderr.isatty()


def start_live(hosts, opts):
    """A Live for this run, honoring ~/.pewrc's pretty_style= and
    --no-color/$NO_COLOR for the footer's own colors."""
    style = load_pewrc().get("pretty_style", "unicode")
    if style not in PRETTY_STYLES:
        warn(f"unknown pretty_style={style!r} in {PEWRC_PATH}, using unicode")
        style = "unicode"
    color = getattr(opts, "color", None) is not False and "NO_COLOR" not in os.environ
    # Only parallel runs can finish out of order; serial --sort is just
    # the launch order.
    return Live(hosts, style, color, keep_order=opts.sort and opts.parallel)


def track(host, fn, *args):
    """fn(host, *args), reported to the --pretty footer as running, then
    done with fn's rc (the first element of what it returns)."""
    if LIVE:
        LIVE.running(host)
    result = fn(host, *args)
    if LIVE:
        LIVE.done(host, result[0])
    return result


def skip_host(host):
    out(f"*** {host}: SKIPPED ***")
    if LIVE:
        LIVE.skipped(host)


def paused():
    return LIVE.pause() if LIVE else contextlib.nullcontext()


def results(future_to_host, hosts, opts):
    """(host, result) for each finished future: as each completes, or
    with --sort in host order, each one as soon as it and every host
    before it are done (so one slow host holds back the rest)."""
    if not opts.sort:
        for fut in concurrent.futures.as_completed(future_to_host):
            yield future_to_host[fut], fut.result()
        return
    by_host = {h: fut for fut, h in future_to_host.items()}
    for h in hosts:
        yield h, by_host[h].result()
        if LIVE:
            LIVE.released(h)


@contextlib.contextmanager
def cancel_on_interrupt(ex):
    """On Ctrl-C, drop the executor's queued hosts instead of letting
    `with ThreadPoolExecutor` wait for every one of them to run."""
    try:
        yield
    except KeyboardInterrupt:
        if LIVE:
            LIVE.interrupt()
        ex.shutdown(wait=False, cancel_futures=True)
        raise


# ---------------------------------------------------------------------------
# Verbose/debug logging. Plain module globals (set once in main()) rather
# than threading opts through every function, matching the original mew's
# own $DEBUG global.
# ---------------------------------------------------------------------------

VERBOSE = False
DEBUG = False


def info(msg):
    if VERBOSE or DEBUG:
        err(f"{PROG}: {msg}")


def debug(msg):
    if DEBUG:
        err(f"{PROG}: debug: {msg}")


def debug_cmd(cmd):
    if DEBUG:
        err(f"{PROG}: debug: + {shlex.join(cmd)}")


def debug_json(label, data):
    if DEBUG:
        err(f"{PROG}: debug: {label}:")
        err(json.dumps(data, indent=2, sort_keys=True))


def debug_block(label, text):
    if DEBUG and text and text.strip():
        err(f"{PROG}: debug: {label}:")
        err(text.rstrip())


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


def natural_key(name):
    """node2 before node10: digit runs compare as numbers. re.split with a
    capture group alternates text, digits, text..., so tuples always line
    up str-with-str and int-with-int."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


def determine_sort(opts):
    """--sort/--no-sort > ~/.pewrc [sort=] > off. Sorting results can't
    mean anything for --parallel --follow's interleaved live lines, so an
    explicit --sort there is an error and ~/.pewrc's sort=yes still sorts
    the launch order but nothing more."""
    if opts.sort and getattr(opts, "parallel", False) and getattr(opts, "follow", False):
        die("--sort/-r can't be combined with --parallel --follow")
    if opts.sort is None:
        return load_pewrc().get("sort", "").lower() in TRUTHY
    return opts.sort


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
    hosts = sorted(hosts, key=natural_key) if sort else hosts
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


# Connection plugins that run ordinary modules like setup on the
# controller rather than on the target (network_cli/httpapi/netconf only
# route their own network modules to the device). Gathered "facts" for
# such a host describe the machine pew is running on, so they're never
# gathered: --where judges those hosts on inventory vars alone. Container/
# VM/Windows plugins (docker, incus, winrm, ...) run setup inside the
# target and are gathered as usual.
CONTROLLER_SIDE_CONNECTIONS = {"local", "network_cli", "httpapi", "netconf"}


def controller_side_hosts(hosts, inv_vars):
    """{host: connection} for hosts whose inventory ansible_connection is
    one of CONTROLLER_SIDE_CONNECTIONS, matched on its last dotted part
    (ansible.netcommon.network_cli). A templated value can't be judged
    unrendered, so it's gathered as usual."""
    skipped = {}
    for host in hosts:
        conn = inv_vars.get(host, {}).get("ansible_connection")
        if not isinstance(conn, str) or "{{" in conn:
            continue
        name = conn.rsplit(".", 1)[-1]
        if name in CONTROLLER_SIDE_CONNECTIONS:
            skipped[host] = name
    return skipped


def filter_by_where(hosts, wheres, opts):
    if not wheres:
        return hosts

    parsed = [(parse_path(k), op, v) for k, op, v in wheres]
    top_level_keys = {segs[0] for segs, _, _ in parsed if segs}

    inv_vars = gather_inventory_vars(hosts, opts)
    skipped = controller_side_hosts(hosts, inv_vars)
    if skipped:
        info(f"{len(skipped)} host(s) use a controller-side connection "
             f"({', '.join(sorted(set(skipped.values())))}): judged on "
             f"inventory vars only: {', '.join(sorted(skipped, key=natural_key))}")
    gatherable = [h for h in hosts if h not in skipped]
    facts = gather_facts(gatherable, top_level_keys, opts) if gatherable else {}

    matched, unreachable = [], []
    for host in hosts:
        # Gathered facts win over inventory vars on key collision, same
        # precedence order Ansible itself uses.
        merged = {**inv_vars.get(host, {}), **facts.get(host, {})}
        if all(where_matches(merged, segs, op, v) for segs, op, v in parsed):
            matched.append(host)
        elif host not in facts and host not in skipped:
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
    skipped = controller_side_hosts(hosts, inv_vars)
    gatherable = [h for h in hosts if h not in skipped]
    facts = gather_facts(gatherable, None, opts) if gatherable else {}

    unreachable = [h for h in gatherable if h not in facts]
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
            if label == "facts" and host in skipped:
                print(f"  {label}:")
                print(f"    (not gathered: {skipped[host]} runs modules on the "
                      f"controller, so its facts would describe this machine, "
                      f"not {host})")
                continue
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
    with paused():
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
# --tty: a remote pseudo-terminal, so tools color their output as if run
# interactively
# ---------------------------------------------------------------------------

TTY_MIN_COLS = 40
TTY_FALLBACK_COLS = 200


def determine_tty(opts, default=False):
    """--tty/--no-tty > $NO_COLOR > ~/.pewrc [tty=] or default (an
    ALIAS_DEFAULTS name) > off. An explicit --tty always applies; a
    default only when stdout is a terminal, so piped output stays plain."""
    if opts.tty is not None:
        return opts.tty
    if "NO_COLOR" in os.environ:
        return False
    wanted = default or load_pewrc().get("tty", "").lower() in TRUTHY
    return wanted and sys.stdout.isatty()


def tty_cols(width, opts):
    """Columns for the remote pty: pew's terminal width minus the prefix
    pew puts on each line (timestamp, then `host: ` padded to width), so
    remote output fits without wrapping. Off a terminal, $COLUMNS, then
    TTY_FALLBACK_COLS. Never below TTY_MIN_COLS, however long the
    hostnames."""
    try:
        cols = os.get_terminal_size(sys.stdout.fileno()).columns
    except OSError:
        env = os.environ.get("COLUMNS", "")
        cols = int(env) if env.isdigit() else TTY_FALLBACK_COLS
    cols -= len(timestamp_prefix(opts))
    if not (opts.quiet or opts.multiline):
        cols -= width + len(": ")
    return max(cols, TTY_MIN_COLS)


def tty_setup(cols):
    """Remote shell prefix for a --tty run. ssh -tt with no local terminal
    sends no window size, so set it; no CR before every LF; and plain cat
    for pagers, which would otherwise wait on a keypress that never comes."""
    return (f"stty -onlcr cols {cols} 2>/dev/null; "
            f"export COLUMNS={cols} PAGER=cat GIT_PAGER=cat SYSTEMD_PAGER=cat;")


def run_argv(host, args, opts):
    """ssh argv for `run`. With --tty: -tt (forced, since pew's stdin isn't
    a terminal) and LogLevel=ERROR, which drops the "Connection to ...
    closed." line ssh prints after a pty session but keeps real errors.
    Both go after the host's own options, so its ssh args can override."""
    argv = ssh_argv(host, opts)
    cols = getattr(opts, "tty_cols", None)
    if cols:
        argv = argv[:-1] + ["-tt", "-o", "LogLevel=ERROR", argv[-1], tty_setup(cols)]
    return argv + substitute(args, host)


def run_stdin(opts):
    """No stdin under --tty: ssh -tt on pew's own terminal would put it in
    raw mode, and parallel hosts would leave it that way."""
    return subprocess.DEVNULL if getattr(opts, "tty_cols", None) else None


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def substitute(args, host):
    return [a.replace("{}", host) for a in args]


def run_host(host, args, opts):
    argv = run_argv(host, args, opts)
    debug_cmd(argv)
    try:
        proc = subprocess.run(
            argv, stdin=run_stdin(opts), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, timeout=opts.timeout,
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
    argv = run_argv(host, args, opts)
    debug_cmd(argv)
    try:
        proc = subprocess.Popen(
            argv, stdin=run_stdin(opts), stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True,
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
    out(f"{ts}{line}" if quiet else f"{ts}{host:<{width}}: {line}")


def open_log(log_prefix, host):
    # buffering=1 (line-buffered) so --log-prefix files are flushed on every
    # line, not just on close — the whole point of pairing this with
    # --follow is that `tail -f PREFIX.hostname` works while it's running.
    return open(f"{log_prefix}.{host}", "a", buffering=1) if log_prefix else None


def do_run_follow_serial(hosts, args, opts, width):
    failures = {}
    state = {"yes_all": opts.yes}
    for host in hosts:
        desc = run_cmdline(host, args, opts)
        if not confirm(host, desc, state, width):
            skip_host(host)
            continue
        log_f = open_log(opts.log_prefix, host)

        def on_line(line):
            emit_line(host, line, opts.quiet, width, opts)
            if log_f:
                log_f.write(line + "\n")

        rc, note = track(host, run_host_streaming, args, opts, on_line)
        if log_f:
            log_f.close()
        if rc != 0:
            failures[host] = failure_detail(rc, note)
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
        rc, note = track(host, run_host_streaming, args, opts, on_line)
        q.put((host, DONE))
        return rc, note

    failures = {}
    max_workers = opts.jobs if opts.jobs else len(hosts)
    with (concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex,
          cancel_on_interrupt(ex)):
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
                failures[host] = failure_detail(rc, note)

    for f in log_files.values():
        if f:
            f.close()
    return failures


def emit(host, output, quiet, multiline, log_prefix, width=0, opts=None, rc=None):
    # Log files stay clean on purpose — timestamps (if any) are terminal-only.
    if log_prefix:
        with open(f"{log_prefix}.{host}", "a") as f:
            f.write(output)
            if output and not output.endswith("\n"):
                f.write("\n")

    ts = timestamp_prefix(opts)
    color = getattr(opts, "color_out", False)

    if quiet:
        for line in output.splitlines():
            out(f"{ts}{line}")
        return

    if multiline:
        out(f"{ts}### {paint(host.upper(), rc, color)} ###")
        for line in output.splitlines():
            out(f"{ts}{line}")
        return

    prefix = paint(host, rc, color) + " " * (width - len(host)) + ": "
    lines = output.splitlines()
    if not lines:
        out(f"{ts}{prefix}")
    for line in lines:
        out(f"{ts}{prefix}{line}")


def run_cmdline(host, args, opts):
    return shlex.join(run_argv(host, args, opts))


def do_run(hosts, args, opts):
    width = label_width(hosts)
    if opts.tty_on:
        opts.tty_cols = tty_cols(width, opts)

    if opts.dry_run:
        for host in hosts:
            emit(host, run_cmdline(host, args, opts), opts.quiet, opts.multiline, None, width, opts)
        return []

    if opts.follow:
        if opts.parallel:
            return do_run_follow_parallel(hosts, args, opts, width)
        return do_run_follow_serial(hosts, args, opts, width)

    failures = {}

    if opts.parallel:
        max_workers = opts.jobs if opts.jobs else len(hosts)
        with (concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex,
              cancel_on_interrupt(ex)):
            future_to_host = {
                ex.submit(track, h, run_host, args, opts): h for h in hosts
            }
            for host, (rc, output, note) in results(future_to_host, hosts, opts):
                emit(host, output, opts.quiet, opts.multiline, opts.log_prefix, width, opts, rc)
                if rc != 0:
                    failures[host] = failure_detail(rc, note)
        return failures

    state = {"yes_all": opts.yes}
    for host in hosts:
        desc = run_cmdline(host, args, opts)
        if not confirm(host, desc, state, width):
            skip_host(host)
            continue
        rc, output, note = track(host, run_host, args, opts)
        emit(host, output, opts.quiet, opts.multiline, opts.log_prefix, width, opts, rc)
        if rc != 0:
            failures[host] = failure_detail(rc, note)
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

    failures = {}

    def report_success(host, output):
        # rsync is silent on success unless --verbose/--debug added -v, in
        # which case output holds its real transfer log. Otherwise fall
        # back to a plain confirmation line so success isn't totally mute.
        text = output if output.strip() else f"copied {copy_description(host, targets, opts)}"
        emit(host, text, opts.quiet, False, opts.log_prefix, width, opts)

    if opts.parallel:
        max_workers = opts.jobs if opts.jobs else len(hosts)
        with (concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex,
              cancel_on_interrupt(ex)):
            future_to_host = {
                ex.submit(track, h, copy_host, targets, opts): h for h in hosts
            }
            for host, (rc, result) in results(future_to_host, hosts, opts):
                if rc != 0:
                    failures[host] = None
                    warn(f"{host}: rsync failed: {result}")
                else:
                    report_success(host, result)
        return failures

    state = {"yes_all": opts.yes}
    for host in hosts:
        desc = f"rsync {copy_description(host, targets, opts)}"
        if not confirm(host, desc, state, width):
            skip_host(host)
            continue
        rc, result = track(host, copy_host, targets, opts)
        if rc != 0:
            failures[host] = None
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

    failures = {}
    state = {"yes_all": opts.yes}
    for host in hosts:
        desc = f"diff {local_file} vs {remote_spec(host, opts)}:{local_file}"
        if not confirm(host, desc, state, width):
            skip_host(host)
            continue
        rc, output = diff_host(host, local_file, opts)
        if rc >= 2:
            failures[host] = None
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
# ping
# ---------------------------------------------------------------------------

AMBER = "\033[33m"
REVERSE = "\033[7m"

PING_DEFAULT_SLOW_MS = 200.0
PING_WINDOW = 20        # recent probes judged for amber (loss)
PING_RED_AFTER = 3      # consecutive losses that turn a tile red
PING_HISTORY = 1000     # probes kept: enough to fill any terminal's width
PING_TILE_MIN_WIDTH = 23   # PingBoard.STATS_W: the stats always fit
PING_TILE_GAP = 2       # columns between side-by-side tiles
PING_SPARK_MAX_ROWS = 12
PING_FPS = 4
PING_SYM = {"ok": "✔", "warn": "◐", "down": "✘", "wait": "◌"}
PING_COLOR = {"ok": GREEN, "warn": AMBER, "down": RED, "wait": DIM}
SPARK_BLOCKS = " ▁▂▃▄▅▆▇█"


class PingTarget:
    """One probed address (or address and port, for --tcp), standing for
    every inventory name that resolves to it. Stats are kept since the
    start or the last `r` reset; epoch tells a probe sent before a reset
    from one sent after, so a late reply can't land in fresh stats."""

    def __init__(self, names, hostname, port, family=None, sockaddr=None, error=None):
        self.names = names
        self.hostname = hostname
        self.port = port
        self.family = family
        self.sockaddr = sockaddr
        self.error = error
        self.seq = 0
        self.epoch = 0
        self.reset()

    def reset(self):
        self.epoch += 1
        self.done = self.received = self.refused = 0
        self.rtt_min = self.rtt_max = None
        self.rtt_sum = 0.0
        self.last = None
        self.history = collections.deque(maxlen=PING_HISTORY)

    @property
    def address(self):
        return self.sockaddr[0] if self.sockaddr else None

    def record(self, status, rtt):
        """status is ok, refused (TCP RST: up, port closed) or lost."""
        self.done += 1
        if status != "lost":
            self.received += 1
            self.rtt_sum += rtt
            self.rtt_min = rtt if self.rtt_min is None else min(self.rtt_min, rtt)
            self.rtt_max = rtt if self.rtt_max is None else max(self.rtt_max, rtt)
            if status == "refused":
                self.refused += 1
        self.last = (status, rtt)
        self.history.append((status, rtt))

    @property
    def loss(self):
        return 100.0 * (self.done - self.received) / self.done if self.done else 0.0

    def state(self, slow):
        if self.error:
            return "down"
        if not self.done:
            return "wait"
        recent = list(self.history)[-PING_WINDOW:]
        if self.received == 0 or (len(recent) >= PING_RED_AFTER and all(
                s == "lost" for s, _ in recent[-PING_RED_AFTER:])):
            return "down"
        status, rtt = self.last
        if (status != "ok" or rtt * 1000 > slow
                or any(s == "lost" for s, _ in recent)):
            return "warn"
        return "ok"

    def short_title_width(self):
        """Width of the shortest title that still shows every name or a
        `+N`, which is what a one-line tile needs to fit."""
        full = len(", ".join(self.names))
        return min(full, len(self.names[0]) + len(f" +{len(self.names) - 1}")) \
            if len(self.names) > 1 else full

    def title(self, width=None):
        """`web1, web1-alt`, or `web1 +1` and then a … cut to fit width."""
        title = ", ".join(self.names)
        if width is None or len(title) <= width:
            return title
        if len(self.names) > 1:
            title = f"{self.names[0]} +{len(self.names) - 1}"
        return title if len(title) <= width else title[:max(width - 1, 0)] + "…"


def format_rtt(seconds):
    if seconds is None:
        return "----"
    ms = seconds * 1000
    if ms < 100:
        return f"{ms:.1f}ms"
    if ms < 10000:
        return f"{ms:.0f}ms"
    return f"{seconds:.1f}s"


def ssh_config(host, opts):
    """ssh's effective config for host, from `ssh -G`: the inventory's
    rendered connection options plus ~/.ssh/config, as ssh itself would
    apply them. Parses config only, never connects. None if ssh fails."""
    argv = ["ssh", "-G"] + ssh_options(host, opts) + [remote_spec(host, opts)]
    debug_cmd(argv)
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL)
    except FileNotFoundError:
        die("ssh not found on PATH")
    if proc.returncode != 0:
        debug_block(f"ssh -G {host} stderr", proc.stderr)
        return None
    config = {}
    for line in proc.stdout.splitlines():
        key, _, value = line.partition(" ")
        config.setdefault(key.lower(), value)
    return config


def ping_resolve(host, opts):
    """(hostname, port, proxy, family, sockaddr, error) for one host."""
    config = ssh_config(host, opts)
    if config is None:
        warn(f"{host}: ssh -G failed, pinging its ansible_host as-is")
        conn = host_connection(host, opts)
        config = {"hostname": conn.get("address") or host,
                  "port": conn.get("port") or "22"}
    proxy = config.get("proxyjump") or config.get("proxycommand")
    if proxy and proxy != "none":
        return config["hostname"], None, proxy, None, None, None
    port = opts.port or int(config.get("port") or 22) if opts.tcp else None
    try:
        family, _, _, _, sockaddr = socket.getaddrinfo(
            config["hostname"], port or 0, proto=socket.IPPROTO_TCP)[0]
    except (socket.gaierror, UnicodeError) as e:
        debug(f"{host}: can't resolve {config['hostname']}: {e}")
        return config["hostname"], port, None, None, None, "unresolved"
    return config["hostname"], port, None, family, sockaddr, None


def ping_targets(hosts, opts):
    """Targets in host order (hosts sharing an address, and port for --tcp,
    share one), plus {host: proxy} for the hosts skipped as proxied."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as ex:
        resolved = list(ex.map(lambda h: ping_resolve(h, opts), hosts))
    targets = {}
    skipped = {}
    for host, (hostname, port, proxy, family, sockaddr, error) in zip(hosts, resolved):
        if proxy:
            skipped[host] = proxy
            continue
        key = (family, sockaddr[0], port) if sockaddr else ("error", host)
        if key in targets:
            targets[key].names.append(host)
        else:
            targets[key] = PingTarget([host], hostname, port, family, sockaddr, error)
        debug(f"{host}: ping {hostname} -> {sockaddr or error}")
    return list(targets.values()), skipped


def icmp_checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(int.from_bytes(data[i:i + 2], "big") for i in range(0, len(data), 2))
    total = (total >> 16) + (total & 0xFFFF)
    total += total >> 16
    return ~total & 0xFFFF


class IcmpProber:
    """Echo requests over unprivileged ICMP datagram sockets (one per
    address family, shared by every target): no root, no subprocesses.
    Replies are matched on (address, sequence). Linux rewrites the echo id
    to the socket's own, and only hands that socket its own replies;
    elsewhere (macOS) the id is checked here instead."""

    name = "icmp"

    def __init__(self, sel, report):
        self.sel = sel
        self.report = report
        self.socks = {}
        self.inflight = {}  # (address, seq) -> (target, epoch, sent, deadline)
        self.ident = os.getpid() & 0xFFFF

    @staticmethod
    def available():
        try:
            socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_ICMP).close()
            return True
        except OSError:
            return False

    def _sock(self, family):
        if family not in self.socks:
            proto = socket.IPPROTO_ICMPV6 if family == socket.AF_INET6 else socket.IPPROTO_ICMP
            s = socket.socket(family, socket.SOCK_DGRAM, proto)
            s.setblocking(False)
            self.sel.register(s, selectors.EVENT_READ, self._on_read)
            self.socks[family] = s
        return self.socks[family]

    def send(self, target, seq, timeout):
        v6 = target.family == socket.AF_INET6
        seq &= 0xFFFF
        payload = b"pew-ping" + struct.pack("!d", time.time())
        header = struct.pack("!BBHHH", 128 if v6 else 8, 0, 0, self.ident, seq)
        # The kernel fills in the ICMPv6 checksum (it covers a pseudo-header).
        checksum = 0 if v6 else icmp_checksum(header + payload)
        packet = struct.pack("!BBHHH", 128 if v6 else 8, 0, checksum, self.ident, seq) + payload
        sent = time.monotonic()
        try:
            self._sock(target.family).sendto(packet, target.sockaddr)
        except OSError as e:
            debug(f"{target.names[0]}: ping send failed: {e}")
            self.report(target, target.epoch, seq, "lost", None)
            return
        self.inflight[(target.address, seq)] = (target, target.epoch, sent, sent + timeout)

    def _on_read(self, sock):
        while True:
            try:
                data, addr = sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                continue
            now = time.monotonic()
            v6 = sock.family == socket.AF_INET6
            if not v6 and data and data[0] >> 4 == 4:  # macOS includes the IP header
                data = data[(data[0] & 0x0F) * 4:]
            if len(data) < 8:
                continue
            kind, _, _, ident, seq = struct.unpack("!BBHHH", data[:8])
            if kind != (129 if v6 else 0):
                continue
            if not sys.platform.startswith("linux") and ident != self.ident:
                continue
            entry = self.inflight.pop((addr[0], seq), None)
            if entry:
                target, epoch, sent, _ = entry
                self.report(target, epoch, seq, "ok", now - sent)

    def expire(self, now):
        for key in [k for k, v in self.inflight.items() if v[3] <= now]:
            target, epoch, _, _ = self.inflight.pop(key)
            self.report(target, epoch, key[1], "lost", None)

    def busy(self):
        return bool(self.inflight)

    def poll_interval(self):
        return None


class SystemPingProber:
    """Fallback when this system doesn't allow unprivileged ICMP sockets:
    one `ping -c 1` per probe, timed by its own `time=` output. -W is
    seconds for iputils/busybox and milliseconds on macOS/BSD."""

    name = "icmp (system ping)"
    TIME_RE = re.compile(r"time[=<]\s*([\d.]+)\s*ms")

    def __init__(self, sel, report):
        self.report = report
        self.inflight = []  # (proc, target, epoch, seq, deadline)

    def send(self, target, seq, timeout):
        if sys.platform.startswith("linux"):
            wait = str(max(1, math.ceil(timeout)))
        else:
            wait = str(max(1, int(timeout * 1000)))
        argv = ["ping", "-n", "-c", "1", "-W", wait, target.address]
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL, text=True)
        except FileNotFoundError:
            die("unprivileged ICMP sockets aren't allowed here and there's no "
                "ping on PATH; try --tcp")
        self.inflight.append((proc, target, target.epoch, seq,
                              time.monotonic() + timeout + 1))

    def expire(self, now):
        still = []
        for proc, target, epoch, seq, deadline in self.inflight:
            if proc.poll() is None and now < deadline:
                still.append((proc, target, epoch, seq, deadline))
                continue
            if proc.returncode is None:
                proc.kill()
            output = proc.communicate()[0] or ""
            m = self.TIME_RE.search(output)
            if proc.returncode == 0 and m:
                self.report(target, epoch, seq, "ok", float(m.group(1)) / 1000)
            else:
                self.report(target, epoch, seq, "lost", None)
        self.inflight = still

    def busy(self):
        return bool(self.inflight)

    def poll_interval(self):
        return 0.02 if self.inflight else None


class TcpProber:
    """A non-blocking connect() per probe, timed to the SYN-ACK and closed
    straight away with a RST (SO_LINGER 0) so nothing sits in TIME_WAIT.
    A refused connection is a reply too: the host is up, the port closed."""

    name = "tcp"

    def __init__(self, sel, report):
        self.sel = sel
        self.report = report
        self.inflight = {}  # socket -> (target, epoch, seq, sent, deadline)

    def send(self, target, seq, timeout):
        s = socket.socket(target.family, socket.SOCK_STREAM)
        s.setblocking(False)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
        sent = time.monotonic()
        rc = s.connect_ex(target.sockaddr)
        if rc in (errno.EINPROGRESS, errno.EWOULDBLOCK, errno.EAGAIN):
            self.inflight[s] = (target, target.epoch, seq, sent, sent + timeout)
            self.sel.register(s, selectors.EVENT_WRITE, self._on_write)
        else:
            self._finish(s, target, target.epoch, seq, sent, rc)

    def _finish(self, s, target, epoch, seq, sent, rc):
        rtt = time.monotonic() - sent
        s.close()
        if rc == 0:
            self.report(target, epoch, seq, "ok", rtt)
        elif rc == errno.ECONNREFUSED:
            self.report(target, epoch, seq, "refused", rtt)
        else:
            debug(f"{target.names[0]}: tcp probe: {os.strerror(rc)}")
            self.report(target, epoch, seq, "lost", None)

    def _on_write(self, s):
        target, epoch, seq, sent, _ = self.inflight.pop(s)
        self.sel.unregister(s)
        self._finish(s, target, epoch, seq, sent,
                     s.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR))

    def expire(self, now):
        for s in [s for s, v in self.inflight.items() if v[4] <= now]:
            target, epoch, seq, _, _ = self.inflight.pop(s)
            self.sel.unregister(s)
            s.close()
            self.report(target, epoch, seq, "lost", None)

    def busy(self):
        return bool(self.inflight)

    def poll_interval(self):
        return None


def ping_layout(n, cols, rows, one_line_width):
    """Fit n tiles to a cols x rows screen (less the header and footer),
    filling top to bottom first, then wrapping into more columns, so the
    sparklines get as much width as possible; spare height goes to taller
    sparklines. Shrinks the gap, then drops the sparkline, before giving
    up on fitting them all. Returns a dict: columns, per_col, tile_w,
    text_rows (1: name and stats on one line, 2: stacked), spark_rows,
    gap, and shown (how many tiles fit; any more become a +N more tile)."""
    avail = max(rows - 2, 1)
    for gap, spark in ((1, 1), (0, 1), (0, 0)):
        for c in range(1, n + 1):
            tile_w = (cols + PING_TILE_GAP) // c - PING_TILE_GAP if c > 1 else cols
            if c > 1 and tile_w < PING_TILE_MIN_WIDTH:
                break
            per_col = math.ceil(n / c)
            text = 1 if tile_w >= one_line_width else 2
            if per_col * (text + spark + gap) - gap <= avail:
                if spark:
                    spark = max(1, min(PING_SPARK_MAX_ROWS,
                                       (avail + gap) // per_col - text - gap))
                return dict(columns=c, per_col=per_col, tile_w=tile_w, text_rows=text,
                            spark_rows=spark, gap=gap, shown=n)
    c = max(1, (cols + PING_TILE_GAP) // (PING_TILE_MIN_WIDTH + PING_TILE_GAP))
    tile_w = (cols + PING_TILE_GAP) // c - PING_TILE_GAP if c > 1 else cols
    text = 1 if tile_w >= one_line_width else 2
    per_col = max(1, avail // text)
    return dict(columns=c, per_col=per_col, tile_w=tile_w, text_rows=text,
                spark_rows=0, gap=0, shown=max(c * per_col - 1, 0))


class PingBoard:
    """The full-screen tile grid, on the terminal's alternate screen (so
    the scrollback is left as it was) with the cursor hidden; both are
    restored on the way out, before the summary is printed."""

    STATS_W = 23  # "✔ 12345    100%  12.3ms"

    def __init__(self, targets, skipped, prober, opts, color):
        self.targets = targets
        self.skipped = skipped
        self.prober = prober
        self.opts = opts
        self.color = color
        self.t0 = time.monotonic()
        self.reset_at = None

    def __enter__(self):
        sys.stdout.write("\033[?1049h\033[?25l\033[H\033[2J")
        sys.stdout.flush()
        return self

    def __exit__(self, *exc):
        sys.stdout.write("\033[?25h\033[?1049l")
        sys.stdout.flush()
        return False

    def _paint(self, color, text):
        return f"{color}{text}{RESET}" if self.color and color and text else text

    @staticmethod
    def _fit(segments, width):
        """(text, color) segments cut and padded to exactly width columns."""
        fitted, used = [], 0
        for text, color in segments:
            text = text[:max(width - used, 0)]
            if text:
                fitted.append((text, color))
                used += len(text)
        if used < width:
            fitted.append((" " * (width - used), None))
        return fitted

    def _line(self, segments, width):
        return "".join(self._paint(c, t) for t, c in self._fit(segments, width))

    def _stats(self, t, state):
        if t.error:
            return [(t.error, RED)]
        sym = [(PING_SYM[state], PING_COLOR[state]), (f" {t.done:>5}", None)]
        if t.last and t.last[0] == "refused":
            mid = [(" ", None), ("REFUSED", REVERSE + AMBER)]
        else:
            mid = [(f" {t.loss:>6.0f}%", None)]
        rtt = t.last[1] if t.last else None
        return sym + mid + [(f" {format_rtt(rtt):>7}", None)]

    def _spark(self, t, width, rows):
        """rows lines of sparkline for t's last width probes, newest at the
        right edge; bars run 0 to the slowest round trip shown, in eighths
        of a row. A lost probe is a red dot on the bottom line."""
        slow = self.opts.slow
        hist = list(t.history)[-width:]
        scale = max((r for s, r in hist if s != "lost"), default=0) or 1e-6
        lines = []
        for row in range(rows - 1, -1, -1):
            segs = [(" " * (width - len(hist)), None)]
            for status, rtt in hist:
                if status == "lost":
                    segs.append(("·" if row == 0 else " ", RED))
                    continue
                level = max(1, round(rtt / scale * rows * 8)) - row * 8
                ch = SPARK_BLOCKS[min(max(level, 0), 8)]
                color = AMBER if status == "refused" or rtt * 1000 > slow else GREEN
                if segs[-1][1] == color:
                    segs[-1] = (segs[-1][0] + ch, color)
                else:
                    segs.append((ch, color))
            lines.append(segs)
        return lines

    def _tile(self, t, lay):
        """The tile's lines, as segment lists."""
        state = t.state(self.opts.slow)
        w = lay["tile_w"]
        stats = self._stats(t, state)
        if lay["text_rows"] == 1:
            name = t.title(w - self.STATS_W - 1)
            pad = w - len(name) - sum(len(s) for s, _ in stats)
            lines = [[(name, PING_COLOR[state]), (" " * max(pad, 1), None)] + stats]
        else:
            indent = "  " if w >= self.STATS_W + 2 else ""
            lines = [[(t.title(w), PING_COLOR[state])], [(indent, None)] + stats]
        if lay["spark_rows"]:
            lines += self._spark(t, w, lay["spark_rows"])
        return lines + [[]] * lay["gap"]

    def _counts(self):
        counts = collections.Counter(t.state(self.opts.slow) for t in self.targets)
        return [(f"{PING_SYM[k]} {counts[k]}  ", PING_COLOR[k])
                for k in ("ok", "warn", "down") if counts[k]]

    def render(self):
        try:
            cols, rows = os.get_terminal_size(sys.stdout.fileno())
        except OSError:
            cols, rows = 80, 24
        cols, rows = max(cols or 80, 20), max(rows or 24, 5)
        now = time.monotonic()
        one_line = max(len(t.title(t.short_title_width())) for t in self.targets) + 1 + self.STATS_W
        lay = ping_layout(len(self.targets), cols, rows, one_line)

        mode = self.prober.name
        ports = {t.port for t in self.targets if t.port}
        if len(ports) == 1:
            mode += f"/{ports.pop()}"
        head = (f"{PROG} ping · {mode} · {len(self.targets)} target(s) · "
                f"every {self.opts.interval:g}s · {format_elapsed(now - self.t0)}")
        if self.reset_at:
            head += f" · reset {format_elapsed(now - self.reset_at)} ago"
        counts = self._counts()
        cw = min(sum(len(s) for s, _ in counts), cols)
        lines = [self._line(self._fit([(head, None)], cols - cw) + counts, cols)]

        shown = self.targets[:lay["shown"]]
        tile_h = lay["text_rows"] + lay["spark_rows"] + lay["gap"]
        grid = [[] for _ in range(min(lay["per_col"] * tile_h, rows - 2))]
        for c in range(lay["columns"]):
            col = shown[c * lay["per_col"]:(c + 1) * lay["per_col"]]
            tiles = [self._tile(t, lay) for t in col]
            if c == lay["columns"] - 1 and lay["shown"] < len(self.targets):
                tiles.append(self._more(lay["shown"]))
            col_lines = [ln for tile in tiles for ln in tile]
            for i, row in enumerate(grid):
                if c:
                    row.append((" " * PING_TILE_GAP, None))
                row += self._fit(col_lines[i] if i < len(col_lines) else [], lay["tile_w"])
        lines += [self._line(row, cols) for row in grid]
        lines += [self._line([], cols)] * (rows - 1 - len(lines))

        keys = "q quit · r reset"
        right = ""
        if self.skipped:
            names = Live._name_list(sorted(self.skipped, key=natural_key),
                                    max(cols - len(keys) - 22, 10))
            right = f"skipped, proxied: {names}"
        lines.append(self._line(self._fit([(keys, DIM)], max(cols - len(right), 0))
                                + [(right, DIM)], cols))
        sys.stdout.write("\033[H" + "\n".join(lines))
        sys.stdout.flush()

    def _more(self, shown):
        rest = self.targets[shown:]
        down = sum(t.state(self.opts.slow) == "down" for t in rest)
        text = f"+{len(rest)} more" + (f" ({down} down)" if down else "")
        return [[(text, RED if down else DIM)]]


def ping_summary(targets, color, reset_at):
    """ping's own end-of-run summary, one line per inventory name."""
    if reset_at:
        clock = datetime.datetime.fromtimestamp(reset_at).strftime("%H:%M:%S")
        out(f"stats since reset at {clock}")
    width = label_width([n for t in targets for n in t.names])
    for t in targets:
        if t.error:
            text = f"{t.hostname} {t.error}"
        else:
            text = (f"{t.done} sent, {t.received} received, {t.loss:.0f}% loss")
            if t.received:
                avg = t.rtt_sum / t.received
                text += (", rtt min/avg/max " + "/".join(
                    f"{x * 1000:.1f}" for x in (t.rtt_min, avg, t.rtt_max)) + "ms")
            if t.refused:
                text += f" (port {t.port} refused)"
        rc = 0 if t.received else 1
        for name in t.names:
            out(paint(name, rc, color) + " " * (width - len(name)) + f": {text}")


def determine_slow(opts):
    """--slow > ~/.pewrc [slow=] > PING_DEFAULT_SLOW_MS, in milliseconds."""
    if opts.slow is not None:
        return opts.slow
    value = load_pewrc().get("slow")
    if value:
        try:
            return float(value)
        except ValueError:
            warn(f"slow={value!r} in {PEWRC_PATH} isn't a number of ms, "
                 f"using {PING_DEFAULT_SLOW_MS:g}")
    return PING_DEFAULT_SLOW_MS


@contextlib.contextmanager
def cbreak_stdin():
    """Single keypresses from stdin, unechoed, if it's a terminal; yields
    its fd, or None. Ctrl-C still raises KeyboardInterrupt."""
    if not sys.stdin.isatty():
        yield None
        return
    import termios
    import tty
    fd = sys.stdin.fileno()
    saved = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        yield fd
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


def raise_fd_limit(needed):
    try:
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if soft != resource.RLIM_INFINITY and soft < needed:
            resource.setrlimit(resource.RLIMIT_NOFILE, (min(needed, hard), hard))
    except (ImportError, ValueError, OSError):
        pass


def do_ping(hosts, opts):
    hosts = sorted(hosts, key=natural_key)
    opts.tcp = opts.tcp or opts.port is not None
    if opts.interval <= 0:
        die("--interval must be more than 0")
    opts.slow = determine_slow(opts)
    timeout = opts.timeout if opts.timeout is not None else min(1.0, opts.interval)
    targets, skipped = ping_targets(hosts, opts)
    if not targets:
        die(f"nothing to ping: all {len(skipped)} host(s) are reached through a proxy")
    board_mode = sys.stdout.isatty() and not opts.quiet
    color = (opts.color is not False and "NO_COLOR" not in os.environ
             and sys.stdout.isatty())

    def warn_skipped():
        if skipped:
            listed = ", ".join(f"{h} (via {p})" for h, p in
                               sorted(skipped.items(), key=lambda kv: natural_key(kv[0])))
            warn(f"skipped {len(skipped)} proxied host(s): {listed}")

    label_w = max(len(t.title()) for t in targets)

    def report(target, epoch, seq, status, rtt):
        if epoch != target.epoch:
            return
        target.record(status, rtt)
        if not board_mode and not opts.quiet:
            what = {"ok": f"time={format_rtt(rtt)}",
                    "refused": f"refused time={format_rtt(rtt)}",
                    "lost": "timeout"}[status]
            out(f"{target.title():<{label_w}}: seq={seq + 1} {what}")
            sys.stdout.flush()

    sel = selectors.DefaultSelector()
    if opts.tcp:
        prober = TcpProber(sel, report)
    elif IcmpProber.available():
        prober = IcmpProber(sel, report)
    else:
        info("unprivileged ICMP sockets not allowed, falling back to system ping")
        prober = SystemPingProber(sel, report)
    raise_fd_limit(len(targets) * (math.ceil(timeout / opts.interval) + 1) + 64)

    live = [t for t in targets if not t.error]
    start = time.monotonic()
    due = {id(t): start + i * opts.interval / len(live) for i, t in enumerate(live)}
    if not board_mode:
        warn_skipped()
        for t in targets:
            if t.error:
                out(f"{t.title():<{label_w}}: {t.hostname} {t.error}")

    board = PingBoard(targets, skipped, prober, opts, color) if board_mode else None
    reset_wall = None
    with contextlib.ExitStack() as stack:
        if board:
            stack.enter_context(board)
        keys_fd = stack.enter_context(cbreak_stdin()) if board else None
        if keys_fd is not None:
            sel.register(keys_fd, selectors.EVENT_READ, None)
        next_frame = start
        try:
            while True:
                now = time.monotonic()
                for t in live:
                    if opts.count is not None and t.seq >= opts.count:
                        continue
                    if due[id(t)] <= now:
                        prober.send(t, t.seq, timeout)
                        t.seq += 1
                        due[id(t)] = max(due[id(t)] + opts.interval, now)
                prober.expire(time.monotonic())
                finished = (opts.count is not None and not prober.busy()
                            and all(t.seq >= opts.count for t in live))
                if board and (now >= next_frame or finished):
                    board.render()
                    next_frame = now + 1 / PING_FPS
                if finished:
                    break
                wake = [due[id(t)] for t in live
                        if opts.count is None or t.seq < opts.count]
                if board:
                    wake.append(next_frame)
                if prober.busy():
                    wake.append(now + (prober.poll_interval() or 0.25))
                timeout_s = max(0.0, min(wake) - time.monotonic()) if wake else 0.25
                for key, _ in sel.select(timeout_s):
                    if key.data is None:
                        ch = os.read(keys_fd, 32).decode(errors="ignore").lower()
                        if "q" in ch:
                            raise KeyboardInterrupt
                        if "r" in ch:
                            for t in targets:
                                t.reset()
                            board.reset_at = time.monotonic()
                            reset_wall = time.time()
                            next_frame = 0
                    else:
                        key.data(key.fileobj)
        except KeyboardInterrupt:
            pass

    if not board and not opts.quiet:
        out()
    ping_summary(targets, color, reset_wall)
    if board:
        warn_skipped()
    return 0 if all(t.received for t in targets) else 1


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
             "hosts_default=/timestamp_format=/extra_vars=/color=/pretty=/tty=/sort=/slow= "
             "overrides. "
             "Same as "
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
    sort_mode = filter_parent.add_mutually_exclusive_group()
    sort_mode.add_argument(
        "--sort", "-r", action="store_true", default=None,
        help="Process hosts in natural hostname order (node2 before "
             "node10), and with --parallel print results in that order "
             "too: each host's as soon as it and every host before it "
             "have finished. Can't combine with --parallel --follow. "
             "Default from ~/.pewrc's sort=yes.",
    )
    sort_mode.add_argument(
        "--no-sort", dest="sort", action="store_false",
        help="Don't sort, overriding ~/.pewrc's sort=",
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
    pretty_mode = exec_parent.add_mutually_exclusive_group()
    pretty_mode.add_argument(
        "--pretty", action="store_true", default=None,
        help="While running, show a live status footer of which hosts are "
             "waiting, running (with elapsed time) and done; it's erased "
             "at the end, leaving just the results and one final status "
             "line. Only when stderr is a terminal. Implies --color where "
             "that applies. Default from ~/.pewrc's pretty=yes; "
             "pretty_style=emoji there for emoji instead of plain symbols.",
    )
    pretty_mode.add_argument(
        "--no-pretty", dest="pretty", action="store_false",
        help="No live status footer, overriding ~/.pewrc's pretty=",
    )
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
    color_mode = run_p.add_mutually_exclusive_group()
    color_mode.add_argument(
        "--color", "-C", action="store_true", default=None,
        help="Color each hostname by its exit status: green for 0, red "
             "otherwise. Only when stdout is a terminal. Can't combine with "
             "--follow (output is printed before the exit status is known) "
             "or --quiet (no hostname to color). Default from ~/.pewrc's "
             "color=yes; $NO_COLOR turns that default off.",
    )
    color_mode.add_argument(
        "--no-color", dest="color", action="store_false",
        help="Don't color output, overriding ~/.pewrc's color=",
    )
    tty_mode = run_p.add_mutually_exclusive_group()
    tty_mode.add_argument(
        "--tty", "-T", action="store_true", default=None,
        help="Run the command on a remote pseudo-terminal (ssh -tt), so tools "
             "color and format their output as they would interactively. Its "
             "width is pew's terminal minus the host prefix, so lines don't "
             "wrap; pagers are set to cat. Needs a POSIX shell on the remote "
             "end. Default from ~/.pewrc's tty=yes (only when stdout is a "
             "terminal); $NO_COLOR turns that default off.",
    )
    tty_mode.add_argument(
        "--no-tty", dest="tty", action="store_false",
        help="No remote pseudo-terminal, overriding ~/.pewrc's tty=",
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

    ping_p = sub.add_parser("ping", parents=[filter_parent],
                             usage="%(prog)s [options] [HOST]",
                             epilog=HOST_EPILOG,
                             help="Ping every host in parallel, on a live grid")
    ping_p.add_argument(
        "--tcp", action="store_true",
        help="Probe with a TCP connect to each host's ssh port (ansible_port, "
             "~/.ssh/config, else 22) instead of ICMP echo. A refused "
             "connection counts as a reply: the host is up, the port closed.",
    )
    ping_p.add_argument(
        "--port", "-p", type=int, metavar="PORT",
        help="TCP port to probe on every host (implies --tcp)",
    )
    ping_p.add_argument(
        "--interval", "-I", type=float, default=1.0, metavar="SECONDS",
        help="Time between probes to each host (default: 1)",
    )
    ping_p.add_argument(
        "--timeout", "-O", type=float, default=None, metavar="SECONDS",
        help="How long to wait for a reply before counting a probe lost "
             "(default: 1, or --interval if that's shorter)",
    )
    ping_p.add_argument(
        "--count", "-c", type=int, default=None, metavar="N",
        help="Stop after N probes per host (default: until q or Ctrl-C)",
    )
    ping_p.add_argument(
        "--slow", type=float, default=None, metavar="MS",
        help="Round trip, in milliseconds, above which a host shows amber "
             f"(default: ~/.pewrc's slow=, else {PING_DEFAULT_SLOW_MS:g})",
    )
    ping_p.add_argument(
        "--quiet", "-q", action="store_true",
        help="No grid or per-probe lines, just the summary at the end",
    )
    ping_p.add_argument(
        "--no-color", dest="color", action="store_false", default=None,
        help="No color in the grid or summary ($NO_COLOR does the same)",
    )

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
    global VERBOSE, DEBUG, PEWRC_PATH, LIVE
    live = None
    try:
        parser = build_parser()
        # shell's command is split off at the first -- before argparse sees
        # it: a REMAINDER positional after HOST would swallow any options
        # given after HOST (`pew shell web1 --root -- sudo -i`). The
        # subcommand is always argv[0], the top-level parser has no options.
        argv = sys.argv[1:]
        prog_name = os.path.basename(sys.argv[0])
        argv[0:0] = PROG_ALIASES.get(prog_name, [])
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

        opts.sort = determine_sort(opts)
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

        if opts.action == "ping":
            return do_ping(hosts, opts)

        alias_defaults = ALIAS_DEFAULTS.get(prog_name, set())
        pretty = (opts.action in ("run", "copy")
                  and determine_pretty(opts, "pretty" in alias_defaults))

        # The --pretty footer (if any) closes on the way out of here: after
        # the failure summary, and on Ctrl-C/die() too, always ending with
        # its final status line.
        with contextlib.ExitStack() as stack:
            if opts.action == "run":
                command = opts.command
                if command and command[0] == "--":
                    command = command[1:]
                if not command:
                    die("no command specified")
                color = determine_color(opts, "color" in alias_defaults or pretty)
                opts.color_out = color
                opts.color_err = color and sys.stderr.isatty()
                opts.tty_on = determine_tty(opts, "tty" in alias_defaults)
                if pretty:
                    live = LIVE = stack.enter_context(start_live(hosts, opts))
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
                if pretty:
                    live = LIVE = stack.enter_context(start_live(hosts, opts))
                failures = do_copy(hosts, targets, opts)

            elif opts.action == "diff":
                failures = do_diff(hosts, opts.local_file, opts)

            if failures:
                color = getattr(opts, "color_err", False)
                listed = ", ".join(
                    paint(h, 1, color) + (f" ({failures[h]})" if failures[h] else "")
                    for h in sorted(failures, key=natural_key))
                warn(f"{len(failures)}/{len(hosts)} host(s) failed: {listed}")
                return 1
            return 0
    except KeyboardInterrupt:
        if live is None:  # the footer's final line already ended the ^C line
            print()
        return 130

