# pew

Run a command, push a file, or diff a file (optionally in parallel) across a set of systems —
using your existing Ansible inventory as the source of truth for what
those systems are.

If you want to pew pew your foot off, pew is the tool for you!

`pew` is a from-scratch, modern rewrite of the ideas in an old internal
Perl tool called `mew` (1998–2001), which did the same job by reading a
hand-maintained `.mewrc` file of hosts and groups. `pew` drops that
entirely in favor of Ansible inventory, drops legacy rsh/PKIDO transport
in favor of plain SSH, and is dependency-free (pure standard library —
see **Install** below for the couple of ways to get it onto your
machine). It is **not** a drop-in replacement — flags and behavior are
deliberately reimagined, not ported 1:1. The old `mew` remains as-is for
anything still depending on it.

## Requirements

- Python 3.10+ (already on any current Debian/Ubuntu/Raspberry Pi OS box)
- `ansible-core` (for the `ansible` command — used only to resolve host
  patterns; `pew` never reads inventory files itself)
- `ssh` / `scp` / `rsync` / `diff` on `PATH`, with whatever host-key/
  agent/config setup you already use to reach these systems manually
  (`rsync` is used for `copy`'s file transfer; `scp` is only used for
  `diff`'s fetch)

No third-party Python dependencies — it only shells out to the tools
above.

## Install

Symlink it onto your `PATH` (the `pew` script resolves symlinks to find
its sibling `src/` directory, so this works from wherever you keep the
clone, and a `git pull` here updates it in place):

```sh
ln -s "$(pwd)/pew" ~/bin/pew      # or /usr/local/bin, wherever you keep personal tools
```

Or, for an isolated install that doesn't depend on keeping the clone
around, use [pipx](https://pipx.pypa.io/):

```sh
pipx install .
```

(`pipx install --editable .` instead, if you're developing pew itself —
edits to `src/pew.py` take effect immediately without reinstalling.)

For a system-wide install available to every user on the box (instead of
just yourself), add `--global`. This installs to `/opt/pipx` and
`/usr/local/bin` rather than your own `~/.local/...`, so it needs root:

```sh
sudo pipx install --global .
```

## Testing against real hosts

`dockerland-tests/` has a suite of scripts (one per feature area, plus a
10-container stress test) that exercise `pew` end-to-end against real
`sshd` containers — ubuntu and rocky — brought up on demand via
`dockerland`, a companion container-harness tool, rather than
hand-rolled shims. Run `dockerland-tests/run_all.sh` for the whole
suite, or any `dockerland-tests/NN_*.sh` script individually.

## How host selection works

Every subcommand's `--hosts`/`-l` (or, for `list`, a positional
argument) takes an **Ansible host pattern**, resolved via:

```sh
ansible '<pattern>' --list-hosts
```

That means anything Ansible's pattern language supports works here,
with no extra logic in `pew` itself:

| Pattern | Meaning |
|---|---|
| `webservers` | everything in the `webservers` group |
| `all` | every host in inventory |
| `web1,web2,db1` | an explicit list |
| `webservers:dbservers` | union of two groups |
| `webservers:&staging` | intersection — in `webservers` *and* `staging` |
| `webservers:!canary` | `webservers` *except* `canary` |
| `~web\d+\.example\.com` | regex match |

`--hosts`/`-l` itself is optional if `~/.pewrc` sets `hosts_default=` —
useful if you mostly run pew against the same group and don't want to
type `--hosts` every time. An explicit `--hosts` on the command line
always overrides it. With neither set, pew errors out rather than
silently defaulting to `all`.

### Which inventory does it use?

In order, first one found wins:

1. `--inventory PATH` / `-i PATH` on the command line
2. `$PEW_INVENTORY` environment variable
3. `inventory=` line in `~/.pewrc`
4. Whatever Ansible itself resolves on its own (`ANSIBLE_INVENTORY` env
   var, `ansible.cfg`'s `inventory=` setting, falling back to
   `/etc/ansible/hosts`) — this is **current-directory sensitive**,
   since Ansible searches for `./ansible.cfg` first.

For a tool you expect to run identically no matter what directory
you're standing in, set a durable default with `~/.pewrc`:

```sh
cat > ~/.pewrc <<'EOF'
inventory=/etc/ansible/hosts
EOF
```

`~/.pewrc` is `key=value` lines; blank lines and lines starting with
`#` are ignored. The value for `inventory` can point at a single
inventory file *or* a directory (Ansible will merge every inventory
source found inside it, same as it does for `ansible.cfg`'s
`inventory=` setting). **Point it at the actual inventory file or a
dedicated inventory-only directory — not your whole Ansible project
root.** If you hand Ansible a directory, it treats every file inside as
a potential inventory source (recursively), so pointing at a project
root that also has `roles/`, `playbooks/`, etc. makes Ansible try to
parse all of that as inventory too, which fails in confusing ways.

`$PEW_INVENTORY` is handy for a one-off shell session (e.g. testing
against a different inventory) without touching `~/.pewrc` or having
to remember `--inventory` on every invocation.

Pass `--pewrc PATH` (or set `$PEW_PEWRC`; `--pewrc` wins if both are
set) to use a different rc file entirely — it replaces `~/.pewrc` for
every setting on this page (`inventory=`, `ansible_config=`,
`hosts_default=`, `timestamp_format=`). Useful for per-project config,
or for testing.

### What about the rest of `ansible.cfg`?

`inventory=` only tells `pew` which inventory *source* to use — it
doesn't give Ansible your project's `ansible.cfg`, which may carry
settings `pew` has no other way to pass through: `vault_password_file`
(needed if any inventory/group_vars/host_vars content is
vault-encrypted), `roles_path`, callback plugin config, etc. Ansible
only auto-discovers `./ansible.cfg` relative to your *current
directory* — which is why a command can work when you `cd` into your
Ansible project tree and fail everywhere else.

Set `ansible_config=` in `~/.pewrc` to fix this the same durable,
cwd-independent way:

```sh
cat >> ~/.pewrc <<'EOF'
ansible_config=/path/to/your/ansible-project/ansible.cfg
EOF
```

`pew` exports this as the `ANSIBLE_CONFIG` environment variable before
every `ansible`/`ansible-inventory` call it makes — unless
`ANSIBLE_CONFIG` is already set in your environment, in which case that
always wins and `~/.pewrc` is left alone.

## Remote user

Ansible resolves `ansible_user` per host from inventory when it
connects. `pew` doesn't — it only ever asks Ansible for bare hostnames
(`ansible ... --list-hosts`), never host variables, so it has no idea
what user Ansible would have used. Plain `ssh`/`scp`/`rsync` just fall
back to your local username, which is frequently not what you want.

`--remote-user`/`-U USER` (available on all five subcommands, including
`list`/`facts` — since `--where`'s fact-gathering opens SSH connections of its
own, even `list --where` needs this) sets one user for every matched
host in the invocation:

```sh
pew run --hosts webservers --remote-user deploy -- systemctl status app
```

`--root` is shorthand for `--remote-user root`. The two are mutually
exclusive — passing both is an error rather than one silently winning.

The confirm-loop prompt and `--dry-run` output always show the actual
connection spec being used (e.g. `ssh root@web1 ...`), so you can see
exactly which user you're about to act as before anything runs. The
hostname *label* on each line of real output (the aligned `host:`
column, log-file names) always stays the bare hostname regardless —
only the connection itself changes.

## Targeting by fact or variable — `--where`

`--hosts` selects by Ansible inventory group/pattern. `--where` narrows
that selection further by matching against gathered Ansible **facts**
(actual system state — OS, version, hardware) *and* **inventory
variables** (`ansible_user`, anything from `group_vars`/`host_vars`,
inline inventory vars) merged into one namespace, gathered facts winning
on key collision — same precedence Ansible itself uses:

```sh
pew run --hosts all --where ansible_distribution=Ubuntu \
    --where ansible_distribution_major_version=26 -- uptime

pew list --hosts all --where custom_group_var=some_value
```

`KEY` is a dotted path, so nested facts/vars are reachable too — `[]`
means "any element of this list", `[N]` a specific index:

```sh
pew list --hosts all --where ansible_default_ipv4.address=10.0.0.5
pew list --hosts all --where ansible_mounts[].mount=/data
pew list --hosts all --where ansible_mounts[0].fstype=ext4
```

`KEY<op>VALUE`, repeatable and AND-ed together. `<op>` is one of:

| op | meaning |
|---|---|
| `=` | equality (exact string match) |
| `!=` | not equal — on a `[]` list path, true only if *no* element equals the value (the logical negation of `=`, not just "flip the comparison") |
| `<` `>` `<=` `>=` | numeric comparison if both sides parse as numbers, string/lexicographic comparison otherwise |
| `~=` | regex search (`re.search`, unanchored — add `^`/`$` yourself for anchoring); invalid patterns are rejected immediately with a clear error, before anything runs |

```sh
pew list --hosts all --where 'ansible_distribution_major_version>=24'
pew list --hosts all --where 'ansible_distribution!=Ubuntu'
pew list --hosts all --where 'ansible_kernel~=^6\.'
```

Quote the whole `KEY<op>VALUE` — `<`, `>`, and `!` are shell-special in
most shells (redirection, history expansion) and will misbehave
unquoted.

Available on `list`/`run`/`copy`/`diff`/`facts` alike, same as `--hosts`
itself — `pew list --hosts X --where Y` to preview exactly who'll be hit, then
reuse the same flags on `run`/`copy`/`diff`.

How it works: after `--hosts` resolves the candidate list, `pew` runs
`ansible-inventory --list` (cheap — just reads config, no connection)
for inventory variables, and `ansible <candidates> -m setup -a
"filter=..."` against just that candidate set (not your whole
inventory) for facts, then filters locally against the merged result.
This is a genuine live gather every time, not a cached read — `ansible
-m setup` run ad-hoc like this always connects and re-gathers,
regardless of any `fact_caching` you have configured in `ansible.cfg`.
(`fact_caching`'s cache-skip behavior is implemented as part of a
*play's* implicit `gather_facts` step, which ad-hoc mode doesn't have —
it always executes the exact module you asked for. A successful gather
here does still write through and refresh that cache for your other,
playbook-based Ansible usage, it just never reads from it itself.) `pew`
doesn't maintain any cache of its own. Fact-gathering honors
`--remote-user`/`--root`, same as everything else; the
inventory-variable read needs no connection at all.

A host that can't be reached during fact-gathering is only excluded (with
a warning) if it *also* doesn't match on inventory variables alone — if
your `--where` clauses are fully satisfied by inventory data, an
unreachable host still matches normally, since no connection was ever
needed to answer that particular question.

## Verbose and debug output

Available on all five subcommands, both writing to stderr so they
never mix into piped/redirected stdout:

- `--verbose`/`-v` — high-level narration of what `pew` is doing:
  resolving hosts, gathering facts, how many matched, etc.
- `--debug`/`-d` — everything `--verbose` shows, plus raw data: the
  exact `ansible`/`ssh`/`scp`/`rsync` command line for every operation,
  and a pretty-printed JSON dump of whatever `--where` gathered from
  each host.

```sh
$ pew list --hosts webservers --where ansible_distribution=Ubuntu --debug
pew: debug: inventory from --inventory: ...
pew: resolving hosts for pattern 'webservers'
pew: debug: + ansible webservers --list-hosts
pew: resolved 3 host(s)
pew: debug: + ansible-inventory --list
pew: gathering facts (ansible_distribution) for 3 host(s)
pew: debug: + ansible web1,web2,web3 -m setup -a filter=ansible_distribution
pew: debug: gathered facts:
{
  "web1": { "ansible_distribution": "Ubuntu" },
  ...
}
pew: 2/3 host(s) matched --where filter(s)
web1
web2
```

Silent by default — neither flag adds any output unless passed.

## Subcommands

### `pew list` — see who a `--hosts` pattern resolves to

```sh
pew list --hosts webservers
pew list --hosts 'webservers:&staging' --sort
```

`--hosts`/`-l` works identically across every subcommand — `list`
included. That's deliberate: run `pew list --hosts X` to see exactly
who `X` resolves to, then reuse that same `--hosts X` on `run`/`copy`/
`diff` and know you're hitting the same set of systems. There's no
separate positional pattern argument, and no implicit "all hosts"
default — omitting `--hosts` falls back to `~/.pewrc`'s
`hosts_default=` if set (see **How host selection works** above), or
errors out if that isn't set either.

Prints one hostname per line — pipe it into `xargs`, a `for` loop,
whatever.

### `pew facts` — explore facts and inventory variables

```sh
pew facts --hosts webservers
pew facts --hosts webservers --match-keys 'ansible_mounts' --match-values 'ext4'
```

Dumps everything `--where` can see for each host — gathered facts *and*
inventory variables, kept as two separate, clearly labeled sections
(unlike `--where`'s merged view) so you can see exactly which source a
value came from, and immediately spot a collision if one exists. Every
key is shown flattened to the same dotted/bracket path `--where` expects
(`ansible_mounts[0].fstype`, `ansible_default_ipv4.address`, ...) — a
line out of `pew facts` is directly usable as a `--where` clause by just
appending `<op>value` — that symmetry is the whole point of this command.

Unfiltered, this is a *lot* of output — every fact `ansible -m setup`
gathers, for every matched host. Narrow it with:

- `--match REGEX` — show a row if REGEX matches the key **or** the value
- `--match-keys REGEX` — only the key path
- `--match-values REGEX` — only the value

All three are repeatable and AND together (including with each other):
`--match-keys foo --match-keys bar --match-values baz` means key matches
`foo` AND key matches `bar` AND value matches `baz`. Same regex engine
and unanchored-search semantics as `--where`'s `~=`, validated at parse
time. `--hosts`/`--where`/`--remote-user`/`--root` all work here too, to
narrow *which hosts* get dumped — same as everywhere else.

If a section ends up with nothing after filtering, that's shown
explicitly (`(no matches out of N facts discovered)`), not silently
omitted — so you can tell "checked, found nothing" apart from an error.
`--verbose` adds a per-host summary line (`N/M facts matched`, `N/M vars
matched`) to stderr.

**Collisions**: if an inventory variable happens to share a name with a
gathered fact (rare, but a user-defined `group_vars`/`host_vars` entry
can do this), both sections show a `*** WARNING ***` line above the
colliding rows, since — as with `--where` — the fact silently wins and
the variable is shadowed. This is the one place `pew` actively surfaces
that shadowing rather than just applying it quietly.

### `pew run` — run a command on each host

```sh
pew run --hosts webservers -- uptime
```

By default this asks per host before running (see **Confirm loop**
below). Add `--yes`/`-Z` to just run everywhere without asking.

Use `{}` in the command as a hostname placeholder:

```sh
pew run --hosts webservers --yes -- echo "I am {}"
pew run --hosts webservers --yes -- tail -n 50 /var/log/{}-app.log
```

If `{}` isn't present, the identical command runs on every host as-is.

Because the command has its own flags/arguments, put `--` before it so
`pew`'s own argument parser doesn't try to interpret them:

```sh
pew run --hosts webservers --yes -- df -h /var
```

Useful flags:

```sh
# Run concurrently across every matched host instead of one at a time
pew run --hosts webservers --parallel -- systemctl status nginx

# Cap concurrency (default with --parallel is "all hosts at once")
pew run --hosts webservers --parallel --jobs 5 -- apt list --upgradable

# Kill any host's command that hasn't finished in 10s
pew run --hosts webservers --yes --timeout 10 -- some-slow-check

# By default a failure on one host stops the run; keep going anyway
pew run --hosts webservers --yes --continue-on-fail -- risky-command

# Also save each host's output to backup.<hostname>
pew run --hosts webservers --yes --log-prefix backup -- tar -cf - /etc | gzip

# One '### HOSTNAME ###' banner per host instead of prefixing every line
pew run --hosts webservers --yes --multiline -- cat /etc/os-release

# No hostname labeling at all, just raw output
pew run --hosts webservers --yes --quiet -- hostname
```

`--parallel` always runs without prompting (there's no meaningful way to
ask "run on this host?" one at a time when they're all launched at
once) — by default, output is buffered per host and printed as each one
finishes, so lines from different hosts never get interleaved mid-line.

#### `--follow` — stream output live instead of waiting

```sh
pew run --hosts webservers --yes --follow -- some-slow-command
pew run --hosts webservers --yes --parallel --follow -- tail -n0 -f /var/log/app.log
```

By default `pew run` captures a host's *entire* output and only shows
it once that host's command has finished. `--follow`/`-f` instead
prints each line as it's produced — same name as `tail -f`/`docker
compose logs -f`/`kubectl logs -f`, and it means the same thing here.
It's independent of `--parallel`: serial `--follow` still confirms and
runs one host at a time, you just see that host's output live instead
of in silence until it completes; `--parallel --follow` streams every
host's output live and genuinely interleaved (like `docker compose logs
-f` across multiple services). The one thing it can't combine with is
`--multiline`, which needs the complete buffered output up front to
print its banner-then-block — `pew` will refuse that combination
outright rather than guess what you meant.

**The best part of this pairing**: combine `--follow` with
`--log-prefix`, and each `PREFIX.hostname` file is written to
line-by-line, flushed immediately, *as the command runs* — not just
written once at the end. That means you can genuinely `tail -f
PREFIX.hostname` in another terminal and watch a specific host's
progress in real time, even in the middle of a `--parallel` run across
dozens of hosts:

```sh
pew run --hosts webservers --yes --parallel --follow --log-prefix /tmp/rollout -- ./deploy.sh
# in another terminal:
tail -f /tmp/rollout.web3
```

#### Timestamps

Available on `run` and `copy` (the two subcommands with real output to
timestamp — `list`/`diff`/`facts` don't have this option). Most useful
with `--follow`/`--parallel`, where knowing exactly *when* a line showed
up is otherwise easy to lose track of, but it's harmless and available
across the board:

```sh
pew run --hosts webservers --yes --timestamp -- some-command
pew run --hosts webservers --yes --parallel --follow --ts -- some-command
```

Four mutually exclusive options (pick one — combining them is
ambiguous, so `pew` rejects that outright rather than silently picking
a winner):

- `--timestamp`/`-t` — prefixes each line with `2026-08-07 14:23:01`
  (`%Y-%m-%d %H:%M:%S`) by default. Full date and time, sortable as
  plain text, deliberately locale-independent — *not* `%c` ("locale's
  appropriate representation"), which would silently format differently
  depending on whoever's `$LC_TIME` happens to be set to, which is
  exactly what you don't want from a shared ops tool's timestamps.
- `--strftime FORMAT` — same idea, your own [strftime
  format](https://docs.python.org/3/library/datetime.html#strftime-and-strptime-format-codes).
- `--ts` — "Matt's correct timestamp." Shorthand for `--strftime
  '%Y%m%d%H%M%S'` (compact, sortable, filename-safe).
- `--tss` — Matt's correct timestamp, with subseconds. Shorthand for
  `--strftime '%Y%m%d%H%M%S.%f'` (same, with microsecond precision —
  `%f` is Python's only fractional-seconds directive, so this is 6
  digits, not milliseconds).

Want a different default for bare `--timestamp` (e.g. `DD/MM/YYYY` if
that's your convention)? Set it once in `~/.pewrc`:

```sh
cat >> ~/.pewrc <<'EOF'
timestamp_format=%d/%m/%Y %H:%M:%S
EOF
```

`--strftime`/`--ts`/`--tss` still override this when given explicitly —
`timestamp_format=` only changes what bare `--timestamp` defaults to.

**Timestamps are terminal-only.** They're never written into
`--log-prefix` files, which stay exactly as clean as they'd be without
`--timestamp` — the whole point of a plain per-host log file is that it
reads as if you'd run the command directly on that host, and timestamp
noise on every line works against that.

### `pew copy` — push file(s) to each host

```sh
pew copy --hosts webservers app.conf /etc/app/app.conf
pew copy --hosts webservers file1.txt file2.txt /etc/app/configs/
```

`SOURCE... DEST` — one or more local files followed by the remote
destination. With more than one source file (or a destination path
ending in `/`), the destination is treated as a directory and each file
keeps its own basename; with a single source and a destination that
doesn't end in `/`, it's an exact target path (a rename), same as `cp`.

The transfer itself is done with `rsync` (not `scp`) specifically
because `rsync` writes to a temp file in the destination directory and
atomically renames it into place, rather than opening the existing
destination file directly. That means a pre-existing destination with
restrictive permissions (e.g. `0400` left over from an earlier push)
is never a problem — `rename()` only needs write access to the
*directory*, not the file being replaced. `rsync` over SSH also execs
the remote `rsync` binary directly, same as any other `ssh host <cmd>`,
so it isn't affected by hardened setups that lock down the `sftp-server`
subsystem `scp` relies on.

#### Ownership and mode

After the transfer, `pew` makes a best-effort attempt to `chown` the
remote file, then `chmod`s it. By default both are mirrored from the
**local** source file's own owner:group and mode — but since nothing
runs as root anymore, that's frequently not what you actually want
(your local uid/gid rarely means anything useful on the remote host).
Override any subset explicitly:

```sh
pew copy --hosts webservers --owner app --group app --mode 640 \
    app.conf /etc/app/app.conf
```

Any of `--owner`/`--group`/`--mode` you omit falls back to mirroring
the local file. The `chown` step is still not guaranteed to succeed —
if the SSH user doesn't have permission to change ownership, `pew`
prints a warning and moves on having still applied the mode bits.
There's no `sudo` escalation built in; if you need that, run `pew copy`
as a user that already has the necessary rights, or fix ownership
out-of-band.

#### Output and `--log-prefix`

`rsync` is silent on success by default, so there's normally nothing to
show beyond a plain confirmation line. Add `--verbose` (or `--debug`,
which implies it, same as elsewhere in `pew`) to get `rsync`'s own
transfer output instead:

```sh
pew copy --hosts webservers --verbose app.conf /etc/app/app.conf
```

`--log-prefix PREFIX` works here exactly as it does on `run` — each
host's output (the real `rsync` output if `--verbose`/`--debug` was
used, otherwise the fallback confirmation line) is also appended to
`PREFIX.hostname`.

#### `--same` — push to the identical remote path

```sh
pew copy --hosts webservers --same /etc/app/app.conf /etc/app/other.conf
```

No `DEST` argument — each `SOURCE` (which must be an absolute path)
is pushed to that exact same path on the remote host. This is the
original `mew -c`'s behavior (it only ever supported same-path pushes,
never an arbitrary destination); `--same` is here for when you want
that "push in place" shape explicitly rather than typing the
destination path twice.

### `pew diff` — compare a local file against each host

```sh
pew diff --hosts webservers /etc/app/app.conf
```

Fetches the file from each host into a temp file, runs `diff -u`
against your local copy, and prints the result labeled by host, e.g.:

```
==> web1 <==
--- /etc/app/app.conf
+++ web1:/etc/app/app.conf
@@ -3,1 +3,1 @@
-listen 8080
+listen 8081
==> web2 <==
(no differences)
```

Differences found is normal output, not a failure — `pew diff` only
reports a host as failed if it couldn't actually fetch the file (e.g.
permissions, file missing, host unreachable).

## Dry run

`--dry-run`/`-n` is available on `run`, `copy`, and `diff` (not `list`
or `facts` — both already just read and print, with nothing to preview;
neither ever changes anything). It prints the exact command(s) that would run on each host,
in the same layout your real output would use — respecting `--quiet`/
`--multiline` for `run` — but never touches the network and never
prompts, regardless of `--yes`:

```sh
$ pew run --hosts webservers --dry-run -- systemctl restart app
web1: ssh web1 systemctl restart app
web2: ssh web2 systemctl restart app

$ pew copy --hosts webservers --dry-run app.conf /etc/app/app.conf
web1: rsync app.conf web1:/etc/app/app.conf
web1: ssh web1 'chown deploy:deploy /etc/app/app.conf; chmod 640 /etc/app/app.conf'
```

`--parallel`, `--jobs`, `--continue-on-fail`, `--log-prefix`, and
`--follow` have nothing to do during a dry run and are silently ignored
— hosts are always previewed serially, in order, and nothing is written
to a log file.

## Confirm loop

`run`, `copy`, and `diff` all ask before acting on each host unless
`--yes`/`-Z` is given (or `--parallel` is used, which implies it):

```
web1: ssh web1 uptime ???
```

- `y` / Enter — do it (default)
- `n` — skip this host
- `q` — quit immediately
- `a` — do it, and don't ask again for the rest of this run
- `?` — show this help

## Exit codes

`0` if every host succeeded, `1` if any host failed (or, for `run`, if
the run was stopped early after a failure without `--continue-on-fail`).
A summary line is printed listing which hosts failed.

## What's deliberately different from the old `mew`

- Host/group config lives in Ansible inventory, not a hand-maintained,
  `require()`'d-as-Perl-code `.mewrc`.
- SSH/SCP only — no rsh, no PKIDO.
- `-n`/`-o` (new/old system groups) are gone; `-n` is reused here for
  `--dry-run` instead.
- No `eff`-version-check mode.
- Group/include/exclude selection is Ansible's own pattern language,
  not a bespoke reimplementation.
- `{}` is an explicit, unambiguous hostname placeholder instead of a
  bare configurable single-character token that could collide with the
  command text.
- Output is always captured and shown (buffered, not a live
  character-by-character stream) rather than connected straight to the
  terminal — simpler and safer, at the cost of not seeing partial output
  from a still-running long command until it finishes.
- No `sudo`/privilege-escalation handling in `copy`'s ownership fixup —
  it's best-effort as the SSH login user, consistent with everyone
  running as non-root now.
- `pew copy`'s default `SOURCE... DEST` (arbitrary destination) is new
  — the original `mew -c` only ever pushed to the identical path on the
  remote. That original behavior lives on here as `--same`.
- `--owner`/`--group`/`--mode` on `copy` are new, to make explicit what
  used to be implicit (and usually wrong, non-root) — mirroring
  whichever local user happened to run the command.
