#!/bin/bash
set -e

# Generate host keys if this is a fresh container (idempotent).
ssh-keygen -A >/dev/null

# The throwaway test-harness public key is bind-mounted read-only at a
# staging path; copy it into place with correct ownership so sshd's
# StrictModes checks pass (a bind-mounted file keeps host-side ownership,
# which usually won't match the container's uid for these accounts).
install -d -m 700 -o testuser -g testuser /home/testuser/.ssh
install -m 600 -o testuser -g testuser /run/test-pubkey/id_test.pub /home/testuser/.ssh/authorized_keys

install -d -m 700 -o root -g root /root/.ssh
install -m 600 -o root -g root /run/test-pubkey/id_test.pub /root/.ssh/authorized_keys

mkdir -p /var/run/sshd

# Pre-create the shared command log, root-owned, world-writable — before
# either testuser or root ever touches it. Otherwise whoever connects
# first creates it with their own umask, and the other user can fail to
# append to it later. Re-running this on every container start also heals
# a stale-permission file left over from a previous run of the same
# bind-mounted logs/ directory.
#
# The directory itself is deliberately NOT world-writable/sticky (755,
# root-owned) — nothing needs to create arbitrary new files here, only
# append to this one well-known log. A sticky world-writable directory
# would trip the kernel's fs.protected_regular hardening (on by default
# on modern Debian/Ubuntu): it refuses an O_CREAT open of an *existing*
# file in such a directory unless the file is owned by either the opener
# or the directory's owner — and since this directory is bind-mounted
# from a host path, its owner is whatever the host uid happens to map to
# inside the container (not necessarily root), which broke this exact
# way the first time around.
mkdir -p /var/log/test-harness
chown root:root /var/log/test-harness
chmod 755 /var/log/test-harness
touch /var/log/test-harness/commands.log
chown root:root /var/log/test-harness/commands.log
chmod 666 /var/log/test-harness/commands.log

exec /usr/sbin/sshd -D -e
