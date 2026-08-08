#!/usr/bin/env bash
# Tests: pew copy - basic single-file copy, multiple sources into a
# directory dest, --same (absolute-path push), --owner/--group/--mode
# overrides, and the fs.protected_regular regression test: rsync-based
# copy (atomic temp-file + rename) succeeding where a plain scp overwrite
# of a pre-existing, other-owned file in a sticky world-writable directory
# is blocked by the kernel's fs.protected_regular hardening.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
LOCAL_TMP="/tmp/pew-copy-test-$$"
cleanup() {
    dt::cleanup "${CONTAINERS[@]}"
    rm -rf "$LOCAL_TMP"
}
trap cleanup EXIT
mkdir -p "$LOCAL_TMP"

dt::log "bringing up one ubuntu ssh container..."
UBUNTU="$(dt::up ubuntu-latest-ssh.toml)" || exit 1
CONTAINERS+=("$UBUNTU")

INV="$(dt::generate_inventory)"
eval "$(dt::activate_cmd)"
IARGS=(-i "$INV" -U testuser --hosts ubuntu_latest_ssh --yes)
IARGS_ROOT=(-i "$INV" --root --hosts ubuntu_latest_ssh --yes)

dt::exec "$UBUNTU" bash -c 'mkdir -p /tmp/copytest && chown testuser:testuser /tmp/copytest' >/dev/null

# --- basic single-file copy ---
echo "hello from pew copy" > "$LOCAL_TMP/basic.txt"
chmod 640 "$LOCAL_TMP/basic.txt"
"$DT_PEW" copy "${IARGS[@]}" "$LOCAL_TMP/basic.txt" /tmp/copytest/basic.txt >/dev/null
remote_content="$(dt::exec "$UBUNTU" cat /tmp/copytest/basic.txt 2>&1 | grep -v 'Output logged')"
if [ "$remote_content" = "hello from pew copy" ]; then
    dt::pass "basic single-file copy: content matches"
else
    dt::fail "basic copy: expected 'hello from pew copy', got: $remote_content"
fi
remote_mode="$(dt::exec "$UBUNTU" stat -c '%a' /tmp/copytest/basic.txt 2>&1 | grep -v 'Output logged')"
if [ "$remote_mode" = "640" ]; then
    dt::pass "basic copy mirrors local file mode (640)"
else
    dt::fail "basic copy mode: expected 640, got: $remote_mode"
fi

# --- multiple sources into a directory dest ---
echo "file-a" > "$LOCAL_TMP/a.txt"
echo "file-b" > "$LOCAL_TMP/b.txt"
"$DT_PEW" copy "${IARGS[@]}" "$LOCAL_TMP/a.txt" "$LOCAL_TMP/b.txt" /tmp/copytest/ >/dev/null
a="$(dt::exec "$UBUNTU" cat /tmp/copytest/a.txt 2>&1 | grep -v 'Output logged')"
b="$(dt::exec "$UBUNTU" cat /tmp/copytest/b.txt 2>&1 | grep -v 'Output logged')"
if [ "$a" = "file-a" ] && [ "$b" = "file-b" ]; then
    dt::pass "multiple sources land correctly in directory dest"
else
    dt::fail "multi-source copy: got a='$a' b='$b'"
fi

# --- --same: absolute-path source, no DEST ---
mkdir -p "$LOCAL_TMP/tmp/copytest"
echo "same-path-content" > "$LOCAL_TMP/tmp/copytest/same.txt"
# --same pushes to the identical ABSOLUTE path on the remote host, so the
# source itself must live at that absolute path locally too.
mkdir -p /tmp/copytest
cp "$LOCAL_TMP/tmp/copytest/same.txt" /tmp/copytest/same.txt
"$DT_PEW" copy "${IARGS[@]}" --same /tmp/copytest/same.txt >/dev/null
remote_same="$(dt::exec "$UBUNTU" cat /tmp/copytest/same.txt 2>&1 | grep -v 'Output logged')"
if [ "$remote_same" = "same-path-content" ]; then
    dt::pass "--same pushes to the identical absolute remote path"
else
    dt::fail "--same: expected 'same-path-content', got: $remote_same"
fi
rm -f /tmp/copytest/same.txt

# --- --owner/--group/--mode overrides (needs --root to actually chown) ---
echo "owned-content" > "$LOCAL_TMP/owned.txt"
"$DT_PEW" copy "${IARGS_ROOT[@]}" --owner testuser --group testuser --mode 600 "$LOCAL_TMP/owned.txt" /tmp/copytest/owned.txt >/dev/null
stat_out="$(dt::exec "$UBUNTU" stat -c '%U:%G:%a' /tmp/copytest/owned.txt 2>&1 | grep -v 'Output logged')"
if [ "$stat_out" = "testuser:testuser:600" ]; then
    dt::pass "--owner/--group/--mode overrides applied correctly"
else
    dt::fail "--owner/--group/--mode: expected 'testuser:testuser:600', got: $stat_out"
fi

# --- fs.protected_regular regression: rsync-based copy succeeds where a
# plain scp overwrite of a pre-existing, differently-owned file in a
# sticky world-writable dir is blocked by kernel hardening ---
dt::exec "$UBUNTU" bash -c '
    mkdir -p /tmp/lockedbox
    chown testuser:testuser /tmp/lockedbox
    chmod 1777 /tmp/lockedbox
    echo original > /tmp/lockedbox/target.txt
    chown root:root /tmp/lockedbox/target.txt
    chmod 666 /tmp/lockedbox/target.txt
' >/dev/null

echo "updated-via-scp" > "$LOCAL_TMP/scp-src.txt"
if scp -F ~/.config/dockerland/ssh_config "$LOCAL_TMP/scp-src.txt" "pew-ubuntu-latest-ssh-alpha:/tmp/lockedbox/target.txt" >/dev/null 2>&1; then
    dt::fail "regression baseline: expected raw scp overwrite to be blocked by fs.protected_regular, but it succeeded"
else
    dt::pass "regression baseline: raw scp overwrite of locked file is blocked (reproduces the original bug condition)"
fi
after_scp="$(dt::exec "$UBUNTU" cat /tmp/lockedbox/target.txt 2>&1 | grep -v 'Output logged')"
if [ "$after_scp" != "original" ]; then
    dt::fail "regression baseline: file content changed despite blocked scp: $after_scp"
fi

mkdir -p "$LOCAL_TMP/tmp/lockedbox"
echo "updated-via-pew" > "$LOCAL_TMP/tmp/lockedbox/target.txt"
cp "$LOCAL_TMP/tmp/lockedbox/target.txt" /tmp/lockedbox_src_for_same.txt 2>/dev/null || true
"$DT_PEW" copy "${IARGS[@]}" "$LOCAL_TMP/tmp/lockedbox/target.txt" /tmp/lockedbox/target.txt >/dev/null 2>&1
after_pew="$(dt::exec "$UBUNTU" cat /tmp/lockedbox/target.txt 2>&1 | grep -v 'Output logged')"
if [ "$after_pew" = "updated-via-pew" ]; then
    dt::pass "pew copy (rsync-based) succeeds where raw scp was blocked - the fs.protected_regular fix"
else
    dt::fail "pew copy over locked file: expected 'updated-via-pew', got: $after_pew"
fi
rm -f /tmp/lockedbox_src_for_same.txt

dt::summary
