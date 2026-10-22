#!/usr/bin/env bash
# Tests: --sort/-r - natural hostname order (n2 before n10) for list and
# serial runs; with --parallel, results printed in that order rather than
# completion order; the --parallel --follow error; ~/.pewrc's sort= and
# --no-sort; and natural order in the failure summary.
#
# No containers needed: a fake ssh on PATH runs each "remote" command
# locally. n2 is the slowest host and n10/n11 fail, so completion order
# differs from natural order and alphabetical order differs from both.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

LOCAL_TMP="/tmp/pew-sort-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
BIN="$LOCAL_TMP/bin"
trap 'rm -rf "$LOCAL_TMP"' EXIT
mkdir -p "$FAKE_HOME" "$BIN"

cat > "$BIN/ssh" <<'SSH'
#!/bin/bash
# Skip ssh options, take the first non-option as the host, run the rest.
while [ $# -gt 0 ]; do
    case "$1" in -o|-p|-l|-i|-F) shift 2;; -*) shift;; *) host="${1#*@}"; shift; break;; esac
done
case "$host" in
    n2.test) sleep 1.5;;
    n10.test|n11.test) sleep 0.2; exit 4;;
    *) sleep 0.3;;
esac
eval "$*"
SSH
chmod +x "$BIN/ssh"
export PATH="$BIN:$PATH"

INV="$LOCAL_TMP/inventory.ini"
cat > "$INV" <<'INI'
[n]
n10.test
n2.test
n1.test
n11.test
n3.test
INI

NATURAL="n1.test n2.test n3.test n10.test n11.test"
INVENTORY_ORDER="n10.test n2.test n1.test n11.test n3.test"
# Hostnames, in the order their output lines appeared.
order() { sed -nE 's/^(n[0-9]+\.test) *:.*/\1/p' <<< "$1" | tr '\n' ' ' | sed 's/ $//'; }

check() {
    local label="$1" expected="$2" got="$3"
    if [ "$got" = "$expected" ]; then dt::pass "$label"; else dt::fail "$label: expected '$expected', got '$got'"; fi
}

check "list keeps inventory order without --sort" "$INVENTORY_ORDER" \
    "$(HOME="$FAKE_HOME" "$DT_PEW" list -i "$INV" n | tr '\n' ' ' | sed 's/ $//')"
check "list -r sorts naturally (n2 before n10)" "$NATURAL" \
    "$(HOME="$FAKE_HOME" "$DT_PEW" list -i "$INV" n -r | tr '\n' ' ' | sed 's/ $//')"

out="$(HOME="$FAKE_HOME" "$DT_PEW" run -i "$INV" n -Z -X -r -- echo hi 2>&1)"
check "serial run -r prints in natural order" "$NATURAL" "$(order "$out")"

out="$(HOME="$FAKE_HOME" "$DT_PEW" run -i "$INV" n -P -- echo hi 2>&1)"
got="$(order "$out")"
if [ "$got" != "$NATURAL" ] && [ "${got##* }" = "n2.test" ]; then
    dt::pass "-P without --sort prints in completion order (slow n2 last)"
else
    dt::fail "-P without --sort: expected completion order ending in n2.test, got '$got'"
fi

out="$(HOME="$FAKE_HOME" "$DT_PEW" run -i "$INV" n -P -r -- echo hi 2>&1)"
check "-P -r holds results back into natural order" "$NATURAL" "$(order "$out")"
if echo "$out" | grep -qF "2/5 host(s) failed: n10.test (4), n11.test (4)"; then
    dt::pass "failure summary lists hosts in natural order"
else
    dt::fail "failure summary order: got: $out"
fi

out="$(HOME="$FAKE_HOME" "$DT_PEW" copy -i "$INV" n -P -r "$INV" "$LOCAL_TMP/dst.ini" 2>&1)"
check "copy -P -r prints in natural order" "n1.test n2.test n3.test" "$(order "$out")"

if out="$(HOME="$FAKE_HOME" "$DT_PEW" run -i "$INV" n -P -f -r -- echo hi 2>&1)"; then
    dt::fail "-P -f -r: expected an error, got: $out"
elif echo "$out" | grep -q "can't be combined with --parallel --follow"; then
    dt::pass "-P -f -r errors out"
else
    dt::fail "-P -f -r: wrong error: $out"
fi

echo "sort=yes" > "$FAKE_HOME/.pewrc"
check "~/.pewrc sort=yes sorts by default" "$NATURAL" \
    "$(HOME="$FAKE_HOME" "$DT_PEW" list -i "$INV" n | tr '\n' ' ' | sed 's/ $//')"
check "--no-sort overrides ~/.pewrc sort=yes" "$INVENTORY_ORDER" \
    "$(HOME="$FAKE_HOME" "$DT_PEW" list -i "$INV" n --no-sort | tr '\n' ' ' | sed 's/ $//')"
out="$(HOME="$FAKE_HOME" "$DT_PEW" run -i "$INV" n -P -f -- echo hi 2>&1)"
if ! echo "$out" | grep -q "can't be combined" && echo "$out" | grep -q "n1.test *: hi"; then
    dt::pass "~/.pewrc sort=yes doesn't break -P -f"
else
    dt::fail "~/.pewrc sort=yes + -P -f: got: $out"
fi
rm -f "$FAKE_HOME/.pewrc"

dt::summary
