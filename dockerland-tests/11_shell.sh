#!/usr/bin/env bash
# Tests: pew shell - exactly-one-host resolution (too many / none / short
# name and substring fallbacks), the plain login-shell path, the `-- COMMAND` path (ssh -t),
# exit-code passthrough, and option parsing after HOST.
#
# The truly interactive part (a real TTY login) can't be asserted on from a
# script; the login-shell path is exercised by feeding commands on stdin,
# which ssh hands to the remote shell exactly as typed input would be.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
LOCAL_TMP="/tmp/pew-shell-test-$$"
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

# Dockerland's inventory with dotted names (<container>.dl.test), so the
# container's own name is the short name, plus never-connected hosts: two
# sharing a short name for the ambiguity check, and a decoy containing the
# container's name as a substring (short-name match must still win).
SHELL_INV="$LOCAL_TMP/shell-inventory.ini"
FQDN="${UBUNTU}.dl.test"
sed "s/^${UBUNTU} /${FQDN} /" "$INV" > "$SHELL_INV"
cat >> "$SHELL_INV" <<'EOF'

[dups]
dup.one.test
dup.two.test
EOF
DECOY="${UBUNTU}-decoy.other.test"
printf '\n[decoys]\n%s\n' "$DECOY" >> "$SHELL_INV"

PEW() { HOME="$FAKE_HOME" "$DT_PEW" "$@"; }
I=(-i "$SHELL_INV")

# --- a pattern matching several hosts aborts and lists them ---
out="$(PEW shell "${I[@]}" dups 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "matched 2 hosts; shell needs exactly one" \
   && echo "$out" | grep -qx "  dup.one.test" && echo "$out" | grep -qx "  dup.two.test"; then
    dt::pass "multi-host pattern aborts and lists the matched hosts"
else
    dt::fail "multi-host abort (rc=$rc): got: $out"
fi

# --- an ambiguous short name aborts the same way ---
out="$(PEW shell "${I[@]}" dup 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "matched 2 hosts"; then
    dt::pass "ambiguous short name (dup -> dup.*) aborts"
else
    dt::fail "ambiguous short name (rc=$rc): got: $out"
fi

# --- no match at all ---
out="$(PEW shell "${I[@]}" no-such-host 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "matched no hosts"; then
    dt::pass "unmatched pattern aborts"
else
    dt::fail "unmatched pattern (rc=$rc): got: $out"
fi

# --- no host given ---
if PEW shell "${I[@]}" >/dev/null 2>&1; then
    dt::fail "no HOST: expected an error, got success"
else
    dt::pass "shell with no HOST errors out"
fi

# --- HOST and --hosts together ---
if PEW shell "${I[@]}" "$UBUNTU" --hosts "$FQDN" >/dev/null 2>&1; then
    dt::fail "HOST + --hosts: expected an error, got success"
else
    dt::pass "HOST and --hosts together error out"
fi

# --- short name resolves to the full inventory name ---
out="$(PEW shell "${I[@]}" "$UBUNTU" --dry-run 2>&1)"
if echo "$out" | grep -qF "pew: $UBUNTU → $FQDN" && echo "$out" | grep -qE "^ssh .*testuser@[0-9.]+$"; then
    dt::pass "short name resolves (${UBUNTU} -> ${FQDN}) and dry-run prints a bare ssh login"
else
    dt::fail "short-name dry-run: got: $out"
fi

# --- the short-name match wins even though a substring match would be ambiguous ---
# ($UBUNTU is a substring of both $FQDN and $DECOY, but only $FQDN is $UBUNTU.*)
if echo "$out" | grep -qF "$DECOY"; then
    dt::fail "short name should not have considered the substring decoy: $out"
else
    dt::pass "short-name match takes precedence over substring matches"
fi

# --- substring fallback (case-insensitive) when no short name matches ---
out="$(PEW shell "${I[@]}" "LATEST-SSH-ALPHA.DL" --dry-run 2>&1)"
if echo "$out" | grep -qF "pew: LATEST-SSH-ALPHA.DL → $FQDN" && echo "$out" | grep -qE "^ssh .*testuser@[0-9.]+$"; then
    dt::pass "case-insensitive substring resolves to the single containing host"
else
    dt::fail "substring fallback: got: $out"
fi

# --- an ambiguous substring aborts and lists every containing host ---
out="$(PEW shell "${I[@]}" "latest-ssh" 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "matched 2 hosts" \
   && echo "$out" | grep -qx "  $FQDN" && echo "$out" | grep -qx "  $DECOY"; then
    dt::pass "ambiguous substring aborts and lists the matches"
else
    dt::fail "ambiguous substring (rc=$rc): got: $out"
fi

# --- -- COMMAND runs with -t, {} is the inventory name, options after HOST parse ---
out="$(PEW shell "${I[@]}" "$UBUNTU" --root --dry-run -- echo '{}' 2>&1)"
if echo "$out" | grep -qE "^ssh -t .*root@[0-9.]+ echo ${FQDN}$"; then
    dt::pass "-- COMMAND adds -t, substitutes {}, and --root after HOST still applies"
else
    dt::fail "-- COMMAND dry-run: got: $out"
fi

# --- a command without -- is rejected rather than guessed at ---
if PEW shell "${I[@]}" "$UBUNTU" whoami >/dev/null 2>&1; then
    dt::fail "command without --: expected an argparse error, got success"
else
    dt::pass "a command without -- is rejected"
fi

# --- real connection: -- COMMAND as inventory's ansible_user ---
out="$(PEW shell "${I[@]}" "$UBUNTU" -- whoami 2>/dev/null </dev/null | tr -d '\r')"
if [ "$out" = "testuser" ]; then
    dt::pass "shell -- whoami connects as inventory's ansible_user"
else
    dt::fail "shell -- whoami: got: $out"
fi

# --- ssh's exit code is pew's exit code ---
PEW shell "${I[@]}" "$UBUNTU" -- sh -c 'exit 7' >/dev/null 2>&1 </dev/null; rc=$?
if [ "$rc" -eq 7 ]; then
    dt::pass "remote exit code passes through (7)"
else
    dt::fail "exit code passthrough: expected 7, got $rc"
fi

# --- plain login shell reads its input from stdin, as typed input would be ---
out="$(printf 'whoami\nhostname\n' | PEW shell "${I[@]}" "$UBUNTU" 2>/dev/null | tr -d '\r')"
if echo "$out" | grep -qx "testuser" && [ "$(echo "$out" | wc -l)" -ge 2 ]; then
    dt::pass "plain shell (no command) runs a remote login shell"
else
    dt::fail "plain shell via stdin: got: $out"
fi

# --- --where narrows to one host ---
out="$(PEW shell "${I[@]}" "$FQDN,dup.one.test" --where "ansible_user=testuser" --dry-run 2>&1)"
if echo "$out" | grep -qE "^ssh .*testuser@[0-9.]+$"; then
    dt::pass "--where narrows a multi-host pattern to the one matching host"
else
    dt::fail "--where narrowing: got: $out"
fi

dt::summary
