#!/usr/bin/env bash
# Tests: pew groups prints one aligned `group : host host ...` line per
# inventory group, with child groups' hosts folded in, `all` left out and
# `ungrouped` shown only when it has hosts.
#
# No containers needed: groups only reads the inventory.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

LOCAL_TMP="/tmp/pew-groups-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
trap 'rm -rf "$LOCAL_TMP"' EXIT
mkdir -p "$FAKE_HOME"

INV="$LOCAL_TMP/inventory.ini"
cat > "$INV" <<'INI'
[routers]
alpha-rtr.example.test
[web]
web2.example.test
web1.example.test
[database]
db1.example.test
[prod:children]
web
database
INI

expected="database : db1.example.test
prod     : db1.example.test web1.example.test web2.example.test
routers  : alpha-rtr.example.test
web      : web1.example.test web2.example.test"

out="$(HOME="$FAKE_HOME" "$DT_PEW" groups -i "$INV")"
if [ "$out" = "$expected" ]; then
    dt::pass "groups: aligned, sorted, children folded in, no all/ungrouped"
else
    dt::fail "groups: expected:
$expected
got:
$out"
fi

echo "lonely.example.test" > "$INV.2"
cat "$INV" >> "$INV.2"
out="$(HOME="$FAKE_HOME" "$DT_PEW" groups -i "$INV.2")"
if echo "$out" | grep -qxF "ungrouped : lonely.example.test"; then
    dt::pass "groups: ungrouped shown when it has hosts"
else
    dt::fail "groups: ungrouped missing: $out"
fi

out="$(HOME="$FAKE_HOME" "$DT_PEW" g -i "$INV")"
if [ "$out" = "$expected" ]; then
    dt::pass "groups: prefix g expands to groups"
else
    dt::fail "groups via 'g': got: $out"
fi

dt::summary
