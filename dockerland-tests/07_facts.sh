#!/usr/bin/env bash
# Tests: pew facts - default fact dump, --match/--match-keys/--match-values
# filtering, and the fact/inventory-var collision warning (facts always
# win, but pew surfaces the collision on both sides rather than silently
# shadowing the inventory var).
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
LOCAL_TMP="/tmp/pew-facts-test-$$"
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
IARGS=(-i "$INV" -U testuser --hosts ubuntu_latest_ssh)

# --- default facts dump includes well-known ansible facts ---
out="$("$DT_PEW" facts "${IARGS[@]}")"
if echo "$out" | grep -q "ansible_distribution = Ubuntu" && echo "$out" | grep -q "ansible_architecture ="; then
    dt::pass "facts (unfiltered) includes gathered ansible facts"
else
    dt::fail "facts unfiltered: missing expected keys"
fi

# --- --match-keys filters by key path ---
out="$("$DT_PEW" facts "${IARGS[@]}" --match-keys distribution)"
if echo "$out" | grep -q "ansible_distribution = Ubuntu" && ! echo "$out" | grep -q "ansible_architecture"; then
    dt::pass "--match-keys distribution narrows to distribution-related keys only"
else
    dt::fail "--match-keys distribution: got: $out"
fi

# --- --match-values filters by value content ---
out="$("$DT_PEW" facts "${IARGS[@]}" --match-values Ubuntu)"
if echo "$out" | grep -q "= Ubuntu" && ! echo "$out" | grep -qE "= [0-9]+$"; then
    dt::pass "--match-values Ubuntu narrows to rows whose value contains 'Ubuntu'"
else
    dt::fail "--match-values Ubuntu: got: $out"
fi

# --- --match matches key OR value ---
out="$("$DT_PEW" facts "${IARGS[@]}" --match '^ansible_distribution$')"
if echo "$out" | grep -q "ansible_distribution = Ubuntu" && ! echo "$out" | grep -q "ansible_distribution_release"; then
    dt::pass "--match '^ansible_distribution\$' matches the exact key only"
else
    dt::fail "--match '^ansible_distribution\$': got: $out"
fi

# --- fact/inventory-var collision warning ---
COLLIDE_INV="$LOCAL_TMP/inv-collide.ini"
cp "$INV" "$COLLIDE_INV"
echo "ansible_distribution=FakeDistro" >> "$COLLIDE_INV"
out="$("$DT_PEW" facts -i "$COLLIDE_INV" -U testuser --hosts ubuntu_latest_ssh --match-keys '^ansible_distribution$')"
if echo "$out" | grep -qi "collides with an inventory variable" \
    && echo "$out" | grep -qi "collides with an inventory fact" \
    && echo "$out" | grep -q "ansible_distribution = Ubuntu" \
    && echo "$out" | grep -q "ansible_distribution = FakeDistro"; then
    dt::pass "colliding fact/inventory-var pair triggers warnings on both sides, facts win"
else
    dt::fail "collision warning: got: $out"
fi

dt::summary
