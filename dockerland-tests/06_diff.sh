#!/usr/bin/env bash
# Tests: pew diff - identical files ("no differences"), differing files
# (unified diff, still exits 0 - diff output is informational, not a
# failure signal), a missing remote path (scp fails, exit 1), --quiet
# (drops the "==> host <==" banner), and --dry-run (prints the commands
# without running them). Note: diff always compares the local file against
# the SAME absolute path on the remote host - there's no separate remote
# path argument, unlike copy.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
LOCAL_TMP="/tmp/pew-diff-test-$$"
cleanup() {
    dt::cleanup "${CONTAINERS[@]}"
    rm -rf "$LOCAL_TMP"
}
trap cleanup EXIT
mkdir -p "$LOCAL_TMP/difftest"

dt::log "bringing up one ubuntu ssh container..."
UBUNTU="$(dt::up ubuntu-latest-ssh.toml)" || exit 1
CONTAINERS+=("$UBUNTU")

INV="$(dt::generate_inventory)"
eval "$(dt::activate_cmd)"
IARGS=(-i "$INV" -U testuser --hosts ubuntu_latest_ssh --yes)

# diff compares against the SAME path remotely, so mirror the local
# scratch path (LOCAL_TMP/difftest) as an absolute remote path too.
REMOTE_DIR="$LOCAL_TMP/difftest"
dt::exec "$UBUNTU" bash -c "mkdir -p '$REMOTE_DIR' && chown testuser:testuser '$REMOTE_DIR'" >/dev/null

# --- identical files: "(no differences)", exit 0 ---
printf 'line1\nline1\n' > "$REMOTE_DIR/same.txt"
dt::run_mailslot_script "$UBUNTU" /dev/stdin <<EOF >/dev/null
mkdir -p '$REMOTE_DIR'
printf 'line1\nline1\n' > '$REMOTE_DIR/same.txt'
chown testuser:testuser '$REMOTE_DIR/same.txt'
EOF

out="$("$DT_PEW" diff "${IARGS[@]}" "$REMOTE_DIR/same.txt" 2>&1)"
rc=$?
if [ "$rc" -eq 0 ] && echo "$out" | grep -q "no differences"; then
    dt::pass "diff on identical files reports 'no differences', exit 0"
else
    dt::fail "diff identical: rc=$rc, out: $out"
fi

# --- differing files: unified diff shown, still exits 0 ---
dt::run_mailslot_script "$UBUNTU" /dev/stdin <<EOF >/dev/null
printf 'line1\nCHANGED\n' > '$REMOTE_DIR/same.txt'
chown testuser:testuser '$REMOTE_DIR/same.txt'
EOF
out="$("$DT_PEW" diff "${IARGS[@]}" "$REMOTE_DIR/same.txt" 2>&1)"
rc=$?
if [ "$rc" -eq 0 ] && echo "$out" | grep -q -- "-line1" && echo "$out" | grep -q -- "+CHANGED"; then
    dt::pass "diff on differing files shows a unified diff, still exits 0"
else
    dt::fail "diff differing: rc=$rc, out: $out"
fi

# --- --quiet drops the '==> host <==' banner ---
out="$("$DT_PEW" diff "${IARGS[@]}" --quiet "$REMOTE_DIR/same.txt" 2>&1)"
if ! echo "$out" | grep -q "==>"; then
    dt::pass "--quiet drops the '==> host <==' banner"
else
    dt::fail "--quiet: banner still present: $out"
fi

# --- missing remote file: scp fails, exit nonzero ---
if "$DT_PEW" diff "${IARGS[@]}" "$REMOTE_DIR/does-not-exist.txt" >/dev/null 2>&1; then
    dt::fail "diff on a missing remote file: expected nonzero exit, got 0"
else
    dt::pass "diff on a missing remote file exits nonzero"
fi

# --- --dry-run prints commands without running them ---
out="$("$DT_PEW" diff "${IARGS[@]}" --dry-run "$REMOTE_DIR/same.txt" 2>&1)"
if echo "$out" | grep -q "scp " && echo "$out" | grep -q "diff -u"; then
    dt::pass "--dry-run prints the scp+diff commands without executing them"
else
    dt::fail "--dry-run: unexpected output: $out"
fi

dt::summary
