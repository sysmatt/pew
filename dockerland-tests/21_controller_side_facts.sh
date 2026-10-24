#!/usr/bin/env bash
# Tests: hosts whose ansible_connection runs modules on the controller
# (local, network_cli, httpapi, netconf) never get facts gathered - setup
# would report the machine pew runs on, so e.g. a network_cli router
# "matched" --where ansible_distribution=Ubuntu. --where judges them on
# inventory vars only, -v says so, and `pew facts` shows a note instead
# of the controller's facts.
#
# No containers needed: ansible_connection=local reproduces this exactly,
# with no network at all.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

LOCAL_TMP="/tmp/pew-ctlside-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
trap 'rm -rf "$LOCAL_TMP"' EXIT
mkdir -p "$FAKE_HOME"

INV="$LOCAL_TMP/inventory.ini"
cat > "$INV" <<'INI'
[routers]
router1.example.test ansible_connection=local router_os=routeros
router2.example.test ansible_connection=ansible.netcommon.network_cli router_os=routeros
INI

# This machine's own ansible_distribution, i.e. what setup would wrongly
# report for the routers.
DISTRO="$(ansible localhost -c local -i localhost, -m setup -a filter=ansible_distribution 2>/dev/null \
    | sed -nE 's/.*"ansible_distribution": "([^"]+)".*/\1/p')"
[ -n "$DISTRO" ] || { dt::fail "couldn't determine this machine's ansible_distribution"; dt::summary; exit 1; }

out="$(HOME="$FAKE_HOME" "$DT_PEW" list -i "$INV" routers --where "ansible_distribution=$DISTRO" 2>&1)"
if [ $? -ne 0 ] && ! echo "$out" | grep -q "^router"; then
    dt::pass "controller-side hosts don't match the controller's ansible_distribution=$DISTRO"
else
    dt::fail "routers matched the controller's own facts: $out"
fi
if ! echo "$out" | grep -q "could not gather facts"; then
    dt::pass "controller-side hosts aren't reported as unreachable"
else
    dt::fail "controller-side hosts reported as unreachable: $out"
fi

out="$(HOME="$FAKE_HOME" "$DT_PEW" list -i "$INV" routers --where router_os=routeros -v 2>&1)"
if [ "$(echo "$out" | grep -c '^router[12]\.example\.test$')" -eq 2 ]; then
    dt::pass "--where still matches them on inventory vars"
else
    dt::fail "inventory-var --where on controller-side hosts: $out"
fi
if echo "$out" | grep -q "2 host(s) use a controller-side connection (local, network_cli): judged on inventory vars only"; then
    dt::pass "-v explains why they were judged on inventory vars only"
else
    dt::fail "-v note missing: $out"
fi

out="$(HOME="$FAKE_HOME" "$DT_PEW" facts -i "$INV" router1.example.test 2>&1)"
if echo "$out" | grep -q "not gathered: local runs modules on the controller" \
        && ! echo "$out" | grep -q "ansible_distribution = " \
        && echo "$out" | grep -q "router_os = routeros"; then
    dt::pass "pew facts shows a note and inventory vars, not the controller's facts"
else
    dt::fail "pew facts on a controller-side host: $out"
fi

dt::summary
