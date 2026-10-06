#!/usr/bin/env bash
# Tests: run/copy/diff/list/facts take HOST as their first positional
# unless --hosts/-l is given (`pew run web uptime`), with options allowed
# before or after it, `-` meaning ~/.pewrc's hosts_default=, and HOST plus
# --hosts/-l together an error.
#
# No containers needed: every check is list/--dry-run against a static
# inventory, which resolves hosts and renders commands locally without
# ever opening an ssh connection.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

LOCAL_TMP="/tmp/pew-hostpos-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
NO_RC_HOME="$LOCAL_TMP/norc"
BIN="$LOCAL_TMP/bin"
trap 'rm -rf "$LOCAL_TMP"' EXIT
mkdir -p "$FAKE_HOME" "$NO_RC_HOME" "$BIN"
ln -s "$DT_PEW" "$BIN/per"
echo "hosts_default=db" > "$FAKE_HOME/.pewrc"

INV="$LOCAL_TMP/inventory.ini"
cat > "$INV" <<'EOF'
[web]
web1.example.test
web2.example.test
[db]
db1.example.test
EOF
FILE="$LOCAL_TMP/f.conf"
echo x > "$FILE"

PEW() { HOME="$FAKE_HOME" "$DT_PEW" "$@"; }
I=(-i "$INV")

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
check_fails() {
    local label="$1" pattern="$2"; shift 2
    local out rc
    out="$(PEW "$@" 2>&1)"; rc=$?
    if [ "$rc" -ne 0 ] && echo "$out" | grep -qE -- "$pattern"; then
        dt::pass "$label"
    else
        dt::fail "$label (rc=$rc): got: $out"
    fi
}

# --- HOST as the first positional ---
check "list HOST"                       '^web2\.example\.test$'                       list "${I[@]}" web
check "list with no HOST uses hosts_default" '^db1\.example\.test$'                  list "${I[@]}"
check "run HOST -- cmd"                 'web2\.example\.test: ssh web2\.example\.test uptime$' run "${I[@]}" web -n -- uptime
check "run HOST cmd, options after HOST" ': ssh web1\.example\.test uptime$'          run web1.example.test "${I[@]}" -n uptime
check "run: options after the command are the command's" ': ssh web1\.example\.test uptime -n$' run "${I[@]}" -n web1.example.test uptime -n
check "copy HOST SOURCE DEST"           ': rsync .*web1\.example\.test:/tmp/'          copy web1.example.test -n "${I[@]}" "$FILE" /tmp/
check "diff HOST LOCAL_FILE"            ': scp web1\.example\.test:'                   diff web1.example.test -n "${I[@]}" "$FILE"
out="$(HOME="$FAKE_HOME" "$BIN/per" web1.example.test "${I[@]}" -n uptime 2>&1)"
if echo "$out" | grep -qE ': ssh web1\.example\.test uptime$'; then
    dt::pass "per HOST cmd"
else
    dt::fail "per HOST cmd: got: $out"
fi

# --- with --hosts/-l there is no HOST positional ---
check "-l: first positional is the command" ': ssh web1\.example\.test uptime$'       run "${I[@]}" -l web1.example.test -n uptime
check "bundled -nl takes the next token"   ': ssh web1\.example\.test uptime$'        run "${I[@]}" -nl web1.example.test uptime
check "--host= (abbreviated --hosts)"     ': ssh web1\.example\.test uptime$'         run "${I[@]}" --host=web1.example.test -n uptime
check_fails "HOST and -l together is an error" 'not both'                             run "${I[@]}" web -l db -n uptime

# --- `-` and `--` fall back to hosts_default= ---
check "HOST - uses hosts_default"       'db1\.example\.test: ssh db1\.example\.test uptime$' run "${I[@]}" - -n uptime
check "nothing after -- is HOST"        'db1\.example\.test: ssh db1\.example\.test web$'    run "${I[@]}" -n -- web
out="$(HOME="$NO_RC_HOME" "$DT_PEW" run "${I[@]}" -n -- uptime 2>&1)"; rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -q "no HOST or --hosts/-l given"; then
    dt::pass "no HOST and no hosts_default is an error"
else
    dt::fail "no HOST, no hosts_default (rc=$rc): got: $out"
fi

dt::summary
