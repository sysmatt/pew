#!/usr/bin/env bash
# Tests: pew run basics - hostname labeling, {} placeholder substitution,
# --quiet, --dry-run (no-op), commands with their own flags via `--`,
# and exit-code propagation on failure.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
cleanup() { dt::cleanup "${CONTAINERS[@]}"; }
trap cleanup EXIT

dt::log "bringing up one ubuntu ssh container..."
UBUNTU="$(dt::up ubuntu-latest-ssh.toml)" || exit 1
CONTAINERS+=("$UBUNTU")

INV="$(dt::generate_inventory)"
eval "$(dt::activate_cmd)"
IARGS=(-i "$INV" -U testuser --hosts ubuntu_latest_ssh --yes)

# --- basic run, hostname-labeled output ---
out="$("$DT_PEW" run "${IARGS[@]}" -- whoami)"
if echo "$out" | grep -q "testuser"; then
    dt::pass "run whoami returns testuser"
else
    dt::fail "run whoami: got: $out"
fi
if echo "$out" | grep -qE "^pew-ubuntu-latest-ssh-[a-z]+:"; then
    dt::pass "run output is hostname-labeled by default"
else
    dt::fail "run output not hostname-labeled: $out"
fi

# --- {} hostname placeholder substitution ---
out="$("$DT_PEW" run "${IARGS[@]}" -- echo "i-am-{}")"
if echo "$out" | grep -qE "i-am-pew-ubuntu-latest-ssh-[a-z]+"; then
    dt::pass "{} placeholder substitutes the hostname"
else
    dt::fail "{} placeholder: got: $out"
fi

# --- --quiet suppresses hostname labeling ---
# (filter out ssh's own "Permanently added ... known hosts" diagnostic -
# that's local ssh-client noise, not remote command output, and --quiet
# only promises to drop the "hostname:" prefix pew itself adds)
out="$("$DT_PEW" run "${IARGS[@]}" --quiet -- whoami | grep -v "Permanently added")"
if [ "$(echo "$out" | tr -d '[:space:]')" = "testuser" ]; then
    dt::pass "--quiet suppresses hostname labeling"
else
    dt::fail "--quiet: expected bare 'testuser', got: $out"
fi

# --- --dry-run does not execute the command ---
marker="/tmp/pew-dryrun-marker-$$"
dt::exec "$UBUNTU" rm -f "$marker" >/dev/null 2>&1 || true
"$DT_PEW" run "${IARGS[@]}" --dry-run -- touch "$marker" >/dev/null
if dt::exec "$UBUNTU" test -e "$marker" >/dev/null 2>&1; then
    dt::fail "--dry-run: marker file was created, command actually ran"
else
    dt::pass "--dry-run does not execute the command"
fi

# --- command with its own flags, requires -- before it ---
out="$("$DT_PEW" run "${IARGS[@]}" -- ls -la /tmp)"
if echo "$out" | grep -q "total"; then
    dt::pass "command with its own flags (ls -la) runs correctly after --"
else
    dt::fail "ls -la /tmp: got: $out"
fi

# --- exit code propagates on failure ---
if "$DT_PEW" run "${IARGS[@]}" -- false >/dev/null 2>&1; then
    dt::fail "run -- false: expected nonzero exit, got 0"
else
    dt::pass "run -- false: nonzero exit propagated"
fi

dt::summary
