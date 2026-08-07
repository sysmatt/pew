# Testing pew against real containers

This is a small, disposable local test lab: two Docker containers running
real `sshd` (Ubuntu and Rocky Linux, so `--where` fact-targeting has real
distro variety to work against), with a throwaway SSH keypair generated
just for this lab. It exists so `pew` can be exercised end-to-end — real
`ssh`/`scp`/`rsync`/`ansible -m setup` — instead of hand-rolled shell
shims standing in for them.

This pattern isn't specific to `pew`. It's meant to be portable: copy the
`test/` directory into another project, adjust the Dockerfiles/inventory
for whatever that project needs to talk to, and the same `up.sh`/`down.sh`
workflow applies.

## Dependencies (install these yourself — nothing here installs anything)

- **Docker Engine + the Compose plugin**, i.e. a working `docker compose
  version`. Not the legacy standalone `docker-compose` (v1) tool — this
  uses the modern `docker compose` (v2, plugin) syntax throughout.
- **OpenSSH client** (`ssh`, `ssh-keygen`, `scp`) — you almost certainly
  already have this if you're using `pew` at all.
- **`ansible-core`** — likewise, already a `pew` requirement.
- Nothing else. No Python packages, no Molecule, no Vagrant.

## What gets built

```
test/
  docker-compose.yml       Two services: ubuntu, rocky
  containers/
    ubuntu/Dockerfile      Ubuntu 24.04 + openssh-server, python3, rsync, diffutils
    rocky/Dockerfile       Rocky Linux 9, same package set
    common/                Shared by both images (see "How logging works")
  bin/
    up.sh                  Generate keypair, build+start, write inventory.ini
    down.sh                Save logs, stop and remove containers
    status.sh              Is it up? Where's the inventory? How big are the logs?
    save-logs.sh            Dump each container's own sshd log without stopping anything
    install-ssh-config.sh   Add a Host block to ~/.ssh/config (see below)
    uninstall-ssh-config.sh Remove that block
    pew                     Runs ../pew with --inventory already filled in
    ping.sh                 Quick Ansible connectivity check (all hosts)
    shell.sh                Quick interactive ssh into a lab host
    tail-logs.sh             Live-tail both containers' command logs together
    exec.sh                 Run something directly inside a container (docker exec, bypasses ssh)
  ssh/          generated, gitignored — throwaway keypair, not your real one
  logs/         generated, gitignored (dir structure kept via .gitkeep)
  inventory.ini generated, gitignored
```

Both containers run `sshd` directly (no systemd), with:
- `testuser` — passwordless `sudo`, for realistic non-root operator testing
- `root` — direct key-based login (`PermitRootLogin prohibit-password`:
  key only, no password), so `pew`'s `--root`/`--remote-user` flows are
  testable too
- Both accept only the one throwaway key generated into `test/ssh/`

## Quick start

```sh
test/bin/up.sh
```

This generates `test/ssh/id_test` (once — reused on subsequent runs),
builds and starts both containers, waits for `sshd` to accept connections,
and writes `test/inventory.ini`.

At this point **Ansible** already works against the lab (it reads
`ansible_ssh_private_key_file` etc. straight from the generated
inventory):

```sh
test/bin/ping.sh
# equivalent to: ansible testlab -i test/inventory.ini -m ping
```

**`pew` itself won't yet**, and this is worth understanding rather than
just working around: `pew` never reads Ansible inventory variables (that
whole design decision is why `--remote-user`/`--root` exist in the first
place — see the main README). It execs plain `ssh <hostname> ...`
directly. So for `pew list --hosts testlab` to actually be usable with
`pew run`/`copy`/`diff` against this lab, the bare hostnames Ansible
resolves (`test-ubuntu`, `test-rocky`) need to mean something to your own
SSH client — which means your `~/.ssh/config`, not the inventory.

```sh
test/bin/install-ssh-config.sh
```

This adds a clearly-delimited block to `~/.ssh/config` (between
`# BEGIN pew-test-harness (...)` and `# END pew-test-harness`) mapping
`test-ubuntu`/`test-rocky` to the right loopback port, the throwaway key,
and disables host-key checking for just those two names (the containers
get fresh host keys on every rebuild, so this avoids known_hosts
churn/warnings for what is, deliberately, a disposable local lab). It's
the **only** part of this whole harness that touches anything outside
`test/` — everything else lives entirely inside the project directory.
It only ever touches the lines between its own markers; nothing else in
your SSH config is read or changed. `test/bin/uninstall-ssh-config.sh`
removes it cleanly.

Once that's installed, use `test/bin/pew` instead of `../pew` — it's the
real `pew`, it just fills in `--inventory test/inventory.ini` for you:

```sh
test/bin/pew list --hosts testlab --where ansible_distribution=Ubuntu
test/bin/pew run --hosts testlab --yes -- uptime
test/bin/pew run --hosts testlab --root --yes -- whoami
test/bin/pew copy --hosts testlab --yes some-file.txt /tmp/some-file.txt
```

(`--hosts testlab` works because `test/inventory.ini` defines a
`testlab` group containing both hosts — see `up.sh`. `--hosts ubuntu` or
`--hosts rocky` targets just one distro family.)

For quick one-off debugging without going through `pew` at all:

```sh
test/bin/shell.sh ubuntu          # interactive ssh as testuser
test/bin/shell.sh ubuntu root     # interactive ssh as root
test/bin/exec.sh ubuntu whoami    # docker exec — bypasses ssh/logging entirely,
                                   # for debugging the container itself
```

## How logging works

The whole point of this harness (beyond "real sshd instead of fake
shims") is a durable, host-visible record of **everything that actually
ran inside each container**, so a test session can be reviewed after the
fact — including after the containers themselves are gone.

Each container bind-mounts `test/logs/<name>/` to `/var/log/test-harness`
inside it, and `sshd` is configured (via
`containers/common/sshd-test-harness.conf`) with:

- `ForceCommand /usr/local/bin/log-and-run.sh` — every SSH session's
  command gets timestamped and appended to `commands.log` *before* it
  actually runs. This catches plain `ssh host cmd` (`pew run`) and
  rsync-over-ssh (`pew copy`, which execs `rsync --server ...` as a
  normal command) — plus anything Ansible itself execs over SSH, like the
  `setup` module for `--where`.
- `Subsystem sftp /usr/local/bin/log-sftp.sh` — `ForceCommand` does
  **not** apply to SSH subsystem requests, so without this, SFTP-based
  transfers would be silently invisible to the log entirely. `scp`
  defaults to the SFTP protocol on modern OpenSSH, and `pew diff` uses
  `scp` for its fetch — so this matters. **Limitation:** this only
  records that an SFTP session happened, not which files were touched
  within it — individual file operations travel inside the binary SFTP
  protocol, not as a command string, so per-file detail would need
  `sftp-server`'s own verbose logging routed through syslog, which this
  harness doesn't set up (diminishing returns for a dev/test lab).

Check what actually happened:

```sh
test/bin/tail-logs.sh       # both containers' commands.log, live, together
test/bin/status.sh          # line counts for both logs
test/bin/save-logs.sh       # also dump each container's own sshd/connection log
```

`commands.log` persists on the host regardless of container state (it's a
bind mount, not something inside the container), so it survives
`down.sh`/rebuilds — delete `test/logs/*/commands.log` yourself if you
want a clean slate.

## Day to day

```sh
test/bin/status.sh    # what's running, where's the inventory, log sizes
test/bin/ping.sh      # quick Ansible connectivity check
test/bin/tail-logs.sh # live-tail both command logs together
test/bin/down.sh      # stop + remove containers (saves logs first)
test/bin/up.sh        # start again — reuses the existing keypair
```

Rebuilding after a Dockerfile change: `test/bin/up.sh` again (it always
passes `--build`).

Adding a third distro: copy `containers/ubuntu/` (or `rocky/`, whichever
package manager family is closer) to a new directory, adjust the install
command, add a matching service block to `docker-compose.yml` with the
next free port (2201, 2203 are taken — use 2205 or similar), and add it
to `up.sh`'s port-wait loop and generated inventory.

## Why not X?

- **Molecule** (the official Ansible testing framework) does something
  similar but is built around testing roles/playbooks specifically, and
  brings its own config format and test-runner conventions. This harness
  is deliberately lighter — a plain `docker-compose.yml` plus a handful of
  shell scripts — to match `pew`'s own "no ceremony" design.
- **`ANSIBLE_STDOUT_CALLBACK=json`** doesn't work for testing purposes
  here — that's an Ansible *output format* concern, unrelated to this
  harness (which just needs real hosts to point commands at, not a
  particular Ansible output shape).
