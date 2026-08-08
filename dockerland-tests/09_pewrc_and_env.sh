#!/usr/bin/env bash
# Tests: ~/.pewrc (inventory=, ansible_config=, timestamp_format=) and
# $PEW_INVENTORY, including their documented precedence:
#   inventory: --inventory flag > $PEW_INVENTORY > ~/.pewrc [inventory=] > ansible default
#   ANSIBLE_CONFIG: already-set env var > ~/.pewrc [ansible_config=]
#   timestamp format: --strftime > --ts > --tss > --timestamp (using
#     ~/.pewrc [timestamp_format=] if set, else the built-in default)
#
# ~/.pewrc is a fixed path (os.path.expanduser("~/.pewrc")), not
# cwd-relative, so every pewrc-dependent call here runs with HOME
# overridden to an isolated scratch directory - never touches the real
# user's actual ~/.pewrc.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
LOCAL_TMP="/tmp/pew-pewrc-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
cleanup() {
    dt::cleanup "${CONTAINERS[@]}"
    rm -rf "$LOCAL_TMP"
}
trap cleanup EXIT
mkdir -p "$FAKE_HOME"

dt::log "bringing up one ubuntu ssh container..."
UBUNTU="$(dt::up ubuntu-latest-ssh.toml)" || exit 1
CONTAINERS+=("$UBUNTU")

INV="$(dt::generate_inventory)"
eval "$(dt::activate_cmd)"

# --- ~/.pewrc's inventory= is used when no --inventory/-$PEW_INVENTORY ---
cat > "$FAKE_HOME/.pewrc" <<EOF
inventory=$INV
EOF
out="$(HOME="$FAKE_HOME" "$DT_PEW" list --hosts ubuntu_latest_ssh -U testuser)"
if echo "$out" | grep -q "pew-ubuntu-latest-ssh"; then
    dt::pass "~/.pewrc's inventory= resolves hosts with no --inventory flag"
else
    dt::fail "~/.pewrc inventory=: got: $out"
fi

# --- $PEW_INVENTORY takes precedence over ~/.pewrc's inventory= ---
cat > "$FAKE_HOME/.pewrc" <<EOF
inventory=/nonexistent/broken-inventory.ini
EOF
out="$(HOME="$FAKE_HOME" PEW_INVENTORY="$INV" "$DT_PEW" list --hosts ubuntu_latest_ssh -U testuser 2>&1)"
if echo "$out" | grep -q "pew-ubuntu-latest-ssh"; then
    dt::pass "\$PEW_INVENTORY overrides a broken ~/.pewrc inventory="
else
    dt::fail "\$PEW_INVENTORY precedence: got: $out"
fi

# --- --inventory flag takes precedence over both $PEW_INVENTORY and ~/.pewrc ---
out="$(HOME="$FAKE_HOME" PEW_INVENTORY=/nonexistent/also-broken.ini "$DT_PEW" list --hosts ubuntu_latest_ssh -U testuser -i "$INV" 2>&1)"
if echo "$out" | grep -q "pew-ubuntu-latest-ssh"; then
    dt::pass "--inventory flag overrides both \$PEW_INVENTORY and ~/.pewrc"
else
    dt::fail "--inventory precedence: got: $out"
fi

# --- ~/.pewrc's ansible_config= is exported as $ANSIBLE_CONFIG ---
FAKE_CFG="$LOCAL_TMP/fake-ansible.cfg"
cat > "$FAKE_CFG" <<EOF
[defaults]
host_key_checking = False
EOF
cat > "$FAKE_HOME/.pewrc" <<EOF
inventory=$INV
ansible_config=$FAKE_CFG
EOF
out="$(HOME="$FAKE_HOME" "$DT_PEW" list --hosts ubuntu_latest_ssh -U testuser --debug 2>&1)"
if echo "$out" | grep -qF "ANSIBLE_CONFIG from $FAKE_HOME/.pewrc: $FAKE_CFG"; then
    dt::pass "~/.pewrc's ansible_config= is exported as \$ANSIBLE_CONFIG"
else
    dt::fail "ansible_config= wiring: got: $out"
fi

# --- an already-set $ANSIBLE_CONFIG wins over ~/.pewrc's ansible_config= ---
out="$(HOME="$FAKE_HOME" ANSIBLE_CONFIG=/already/set.cfg "$DT_PEW" list --hosts ubuntu_latest_ssh -U testuser --debug 2>&1)"
if echo "$out" | grep -qF "ANSIBLE_CONFIG already set in environment: /already/set.cfg"; then
    dt::pass "a caller-set \$ANSIBLE_CONFIG wins over ~/.pewrc's ansible_config="
else
    dt::fail "ANSIBLE_CONFIG precedence: got: $out"
fi

# --- ~/.pewrc's timestamp_format= overrides the built-in --timestamp default ---
cat > "$FAKE_HOME/.pewrc" <<EOF
inventory=$INV
timestamp_format=[%H-%M]
EOF
out="$(HOME="$FAKE_HOME" "$DT_PEW" run --hosts ubuntu_latest_ssh -U testuser --yes --timestamp -- echo hi)"
if echo "$out" | grep -qE '^\[[0-9]{2}-[0-9]{2}\] pew-ubuntu-latest-ssh-[a-z]+: hi$'; then
    dt::pass "~/.pewrc's timestamp_format= overrides the default --timestamp format"
else
    dt::fail "timestamp_format=: got: $out"
fi

# --- without a pewrc override, --timestamp falls back to the built-in default ---
rm -f "$FAKE_HOME/.pewrc"
cat > "$FAKE_HOME/.pewrc" <<EOF
inventory=$INV
EOF
out="$(HOME="$FAKE_HOME" "$DT_PEW" run --hosts ubuntu_latest_ssh -U testuser --yes --timestamp -- echo hi)"
if echo "$out" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} pew-ubuntu-latest-ssh-[a-z]+: hi$'; then
    dt::pass "with no timestamp_format= set, --timestamp uses the built-in default"
else
    dt::fail "default timestamp format: got: $out"
fi

dt::summary
