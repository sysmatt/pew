#!/usr/bin/env bash
# Tests: subcommands accept any unique prefix of their name (r -> run,
# sh -> shell), exact names always win, and an ambiguous prefix is an
# error rather than a guess.
#
# No containers needed: every check is list/--dry-run against a static
# inventory, which resolves hosts and renders connection vars locally
# without ever opening an ssh connection. Ambiguity can't arise between
# the current subcommand names (all distinct first letters), so that path
# is exercised directly against expand_subcommand() with a made-up name
# list.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

LOCAL_TMP="/tmp/pew-prefix-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
trap 'rm -rf "$LOCAL_TMP"' EXIT
mkdir -p "$FAKE_HOME"

INV="$LOCAL_TMP/inventory.ini"
cat > "$INV" <<'EOF'
[web]
web1.example.test
web2.example.test
EOF

PEW() { HOME="$FAKE_HOME" "$DT_PEW" "$@"; }
H=(-i "$INV" --hosts web1.example.test)

# --- one-letter and longer prefixes reach the right subcommand ---
check() {
    local label="$1" pattern="$2"; shift 2
    local out
    out="$(PEW "$@" 2>&1)"
    if echo "$out" | grep -qE -- "$pattern"; then
        dt::pass "$label"
    else
        dt::fail "$label: got: $out"
    fi
}
check "l -> list"           '^web1\.example\.test$'                  l "${H[@]}"
check "li -> list"          '^web1\.example\.test$'                  li "${H[@]}"
check "r -> run"            ': ssh web1\.example\.test uptime$'      r "${H[@]}" -n -- uptime
check "c -> copy"           ': rsync .*web1\.example\.test:/tmp/'    c "${H[@]}" -n "$INV" /tmp/
check "d -> diff"           ': scp web1\.example\.test:'             d "${H[@]}" -n "$INV"
check "s -> shell (with --)" '^ssh -t web1\.example\.test htop$'      s -i "$INV" -n web1.example.test -- htop
check "sh -> shell"         '^ssh web1\.example\.test$'              sh -i "$INV" -n web1
check "full name still works" '^web1\.example\.test$'                list "${H[@]}"

# --- an unknown word is left to argparse's own invalid-choice error ---
out="$(PEW x "${H[@]}" 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "invalid choice: 'x'"; then
    dt::pass "unknown subcommand gets argparse's invalid-choice error"
else
    dt::fail "unknown subcommand (rc=$rc): got: $out"
fi

# --- ambiguity and exact-wins, against a made-up name list ---
EXPAND=(python3 -c '
import sys
sys.path.insert(0, sys.argv[1])
import pew
print(pew.expand_subcommand(sys.argv[2], sys.argv[3].split(",")))
' "$DT_REPO_ROOT/src")
out="$("${EXPAND[@]}" s shell,sync 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "subcommand 's' is ambiguous: shell, sync"; then
    dt::pass "ambiguous prefix (s: shell, sync) aborts and names the candidates"
else
    dt::fail "ambiguous prefix (rc=$rc): got: $out"
fi
out="$("${EXPAND[@]}" sy shell,sync 2>&1)"
if [ "$out" = "sync" ]; then
    dt::pass "a longer prefix disambiguates (sy -> sync)"
else
    dt::fail "longer prefix: got: $out"
fi
out="$("${EXPAND[@]}" co co,copy 2>&1)"
if [ "$out" = "co" ]; then
    dt::pass "an exact name wins over being a prefix of another (co, copy)"
else
    dt::fail "exact-name precedence: got: $out"
fi

dt::summary
