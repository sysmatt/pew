#!/usr/bin/env bash
# Tests: pew run --remote-user/-U, the --root shortcut, and their mutual
# exclusivity. Note: we deliberately don't try to prove "--remote-user also
# governs --where's fact-gathering" here - pew's --where shells out to the
# real `ansible` CLI, and in this harness dockerland's generated inventory
# sets a group-level ansible_user which ansible-core's own variable
# precedence honors over the `-u`/--user flag pew passes it, so a wrong
# --remote-user doesn't actually break fact-gathering in this particular
# setup. That's an Ansible inventory-precedence quirk, not something to
# assert on reliably.
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
BASE=(-i "$INV" --hosts ubuntu_latest_ssh --yes)

# --- --remote-user connects as the specified user ---
out="$("$DT_PEW" run "${BASE[@]}" --remote-user testuser -- whoami)"
if echo "$out" | grep -q "testuser"; then
    dt::pass "--remote-user testuser connects as testuser"
else
    dt::fail "--remote-user testuser: got: $out"
fi

# --- --root is a shortcut for --remote-user root ---
out="$("$DT_PEW" run "${BASE[@]}" --root -- whoami)"
if echo "$out" | grep -q "root"; then
    dt::pass "--root connects as root"
else
    dt::fail "--root: got: $out"
fi

# --- --root grants access testuser doesn't have ---
if "$DT_PEW" run "${BASE[@]}" --remote-user testuser -- cat /etc/shadow >/dev/null 2>&1; then
    dt::fail "testuser: expected /etc/shadow read to be denied, but it succeeded"
else
    dt::pass "testuser cannot read /etc/shadow (permission denied)"
fi
out="$("$DT_PEW" run "${BASE[@]}" --root -- head -1 /etc/shadow 2>&1)"
if echo "$out" | grep -q "^pew-ubuntu-latest-ssh-[a-z]*: root:"; then
    dt::pass "--root can read /etc/shadow (root:... line present)"
else
    dt::fail "--root reading /etc/shadow: got: $out"
fi

# --- --remote-user and --root are mutually exclusive ---
if "$DT_PEW" run "${BASE[@]}" --remote-user testuser --root -- whoami >/dev/null 2>&1; then
    dt::fail "--remote-user + --root: expected argparse error, got success"
else
    dt::pass "--remote-user and --root together correctly error out"
fi

# --- a genuinely nonexistent remote user fails authentication ---
if "$DT_PEW" run "${BASE[@]}" --remote-user nosuchuser -- whoami >/dev/null 2>&1; then
    dt::fail "--remote-user nosuchuser: expected auth failure, got success"
else
    dt::pass "--remote-user nosuchuser fails authentication as expected"
fi

dt::summary
