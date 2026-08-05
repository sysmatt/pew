# pew

Run a command, push a file, or diff a file across a set of systems —
using your existing Ansible inventory as the source of truth for what
those systems are.

`pew` is a from-scratch, modern rewrite of the ideas in an old internal
Perl tool called `mew` (1998–2001), which did the same job by reading a
hand-maintained `.mewrc` file of hosts and groups. `pew` drops that
entirely in favor of Ansible inventory, drops legacy rsh/PKIDO transport
in favor of plain SSH, and is a single, dependency-free Python file. It
is **not** a drop-in replacement — flags and behavior are deliberately
reimagined, not ported 1:1. The old `mew` remains as-is for anything
still depending on it.

## Requirements

- Python 3.10+ (already on any current Debian/Ubuntu/Raspberry Pi OS box)
- `ansible-core` (for the `ansible` command — used only to resolve host
  patterns; `pew` never reads inventory files itself)
- `ssh` / `scp` / `rsync` / `diff` on `PATH`, with whatever host-key/
  agent/config setup you already use to reach these systems manually
  (`rsync` is used for `copy`'s file transfer; `scp` is only used for
  `diff`'s fetch)

No other dependencies. No virtualenv, no `pip install`, nothing to keep
in sync.

## Install

```sh
cp pew ~/bin/pew      # or /usr/local/bin, wherever you keep personal tools
chmod +x ~/bin/pew
```

That's it. It's one file.

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
`#` are ignored. Currently the only key read is `inventory`. The value
can point at a single inventory file *or* a directory (Ansible will
merge every inventory source found inside it, same as it does for
`ansible.cfg`'s `inventory=` setting).

`$PEW_INVENTORY` is handy for a one-off shell session (e.g. testing
against a different inventory) without touching `~/.pewrc` or having
to remember `--inventory` on every invocation.

## Remote user

Ansible resolves `ansible_user` per host from inventory when it
connects. `pew` doesn't — it only ever asks Ansible for bare hostnames
(`ansible ... --list-hosts`), never host variables, so it has no idea
what user Ansible would have used. Plain `ssh`/`scp`/`rsync` just fall
back to your local username, which is frequently not what you want.

`--remote-user`/`-U USER` (available on `run`, `copy`, and `diff` —
not `list`, which never opens a connection) sets one user for every
matched host in the invocation:

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
separate positional pattern argument or implicit "all hosts" default
anywhere — `--hosts` is always required.

Prints one hostname per line — pipe it into `xargs`, a `for` loop,
whatever.

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
once) — output is buffered per host and printed as each one finishes,
so lines from different hosts never get interleaved mid-line.

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

`--dry-run`/`-n` is available on `run`, `copy`, and `diff` (not `list`,
which already just shows the resolved host set with nothing to
preview). It prints the exact command(s) that would run on each host,
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

`--parallel`, `--jobs`, `--continue-on-fail`, and `--log-prefix` have
nothing to do during a dry run and are silently ignored — hosts are
always previewed serially, in order, and nothing is written to a log
file.

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
