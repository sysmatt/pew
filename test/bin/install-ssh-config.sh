#!/bin/bash
# Adds (or refreshes) a clearly-marked Host block in ~/.ssh/config so bare
# `ssh test-ubuntu`/`ssh test-rocky` — and therefore pew, which execs plain
# `ssh <host> ...` directly and never reads Ansible inventory vars — resolve
# to the right loopback port with the right throwaway key, without prompting
# for host-key verification (containers get fresh host keys on every
# rebuild). Idempotent: safe to re-run any time (e.g. after up.sh).
#
# This is the one part of the test harness that touches a file outside this
# project. It only ever touches the block between the BEGIN/END markers
# below — nothing else in ~/.ssh/config is read or modified. Run
# uninstall-ssh-config.sh to remove it cleanly.
set -euo pipefail
cd "$(dirname "$0")/.."   # -> test/

KEY_PATH="$(pwd)/ssh/id_test"
if [ ! -f "$KEY_PATH" ]; then
    echo "test/ssh/id_test not found — run test/bin/up.sh first." >&2
    exit 1
fi

MARKER_BEGIN="# BEGIN pew-test-harness ($(pwd))"
MARKER_END="# END pew-test-harness"

SSH_CONFIG="$HOME/.ssh/config"
mkdir -p "$HOME/.ssh"
touch "$SSH_CONFIG"
chmod 600 "$SSH_CONFIG"

TMP="$(mktemp)"
awk '
    $0 ~ /^# BEGIN pew-test-harness/ { skipping=1 }
    skipping && $0 ~ /^# END pew-test-harness/ { skipping=0; next }
    !skipping { print }
' "$SSH_CONFIG" > "$TMP"

{
    cat "$TMP"
    echo "$MARKER_BEGIN"
    for pair in "test-ubuntu:2201" "test-rocky:2203"; do
        host="${pair%%:*}"
        port="${pair##*:}"
        cat <<EOF
Host $host
    HostName 127.0.0.1
    Port $port
    User testuser
    IdentityFile $KEY_PATH
    IdentitiesOnly yes
    StrictHostKeyChecking no
    UserKnownHostsFile /dev/null
    LogLevel ERROR

EOF
    done
    echo "$MARKER_END"
} > "$SSH_CONFIG"

rm -f "$TMP"
echo "Installed pew-test-harness block in $SSH_CONFIG"
echo "Try: ssh test-ubuntu whoami"
echo "For root (IdentityFile etc. still apply, only the user changes):"
echo "  ssh root@test-ubuntu whoami"
