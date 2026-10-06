#!/usr/bin/env bash
# Tests: a host pattern matching nothing lists similar hosts and groups on
# stderr before erroring out - substring matches and close spellings, for
# the pattern's unknown names only - from both resolve paths (list/run/...
# and shell).
#
# No containers needed: resolving a pattern against a static inventory
# never opens an ssh connection.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

LOCAL_TMP="/tmp/pew-nomatch-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
trap 'rm -rf "$LOCAL_TMP"' EXIT
mkdir -p "$FAKE_HOME"

INV="$LOCAL_TMP/inventory.ini"
cat > "$INV" <<'EOF'
[routers]
alpha-rtr.example.test
qth0-router.example.test
[web]
web1.example.test
web2.example.test
[staging]
web2.example.test
[database]
db1.example.test
EOF

# Runs pew, capturing stderr only; passes if it failed and every given
# line appears (whole-line match) in stderr.
expect() {
    local label="$1"; shift
    local -a lines=()
    while [ "$1" != "--" ]; do lines+=("$1"); shift; done; shift
    local err rc
    err="$(HOME="$FAKE_HOME" "$DT_PEW" "$@" 2>&1 >/dev/null)"; rc=$?
    if [ "$rc" -eq 0 ]; then
        dt::fail "$label: expected failure, got success"; return
    fi
    for line in "${lines[@]}"; do
        if ! echo "$err" | grep -qxF -- "$line"; then
            dt::fail "$label: missing line '$line' in: $err"; return
        fi
    done
    dt::pass "$label"
}
I=(-i "$INV")

expect "substring of a host" \
    "pew: error: host pattern 'alpha' matched no hosts" "similar hosts:" "  alpha-rtr.example.test" -- \
    list "${I[@]}" alpha
expect "typo of a name part" \
    "similar hosts:" "  alpha-rtr.example.test" -- list "${I[@]}" aplha
expect "typo of a group" \
    "similar groups:" "  database" -- list "${I[@]}" datbase
expect "only the unknown term of a compound pattern" \
    "similar groups:" "  staging" -- list "${I[@]}" 'web:&stagin'
expect "run HOST gets suggestions too" \
    "  web1.example.test" -- run "${I[@]}" -n wbe1 uptime
expect "shell gets suggestions too" \
    "  alpha-rtr.example.test" "  qth0-router.example.test" -- shell "${I[@]}" -n rtr9

err="$(HOME="$FAKE_HOME" "$DT_PEW" list "${I[@]}" zzz 2>&1 >/dev/null)"
if [ "$err" = "pew: error: host pattern 'zzz' matched no hosts" ]; then
    dt::pass "nothing similar: just the error line"
else
    dt::fail "nothing similar: got: $err"
fi
out="$(HOME="$FAKE_HOME" "$DT_PEW" list "${I[@]}" alpha 2>/dev/null)"
if [ -z "$out" ]; then
    dt::pass "suggestions go to stderr, not stdout"
else
    dt::fail "stdout not empty: $out"
fi

dt::summary
