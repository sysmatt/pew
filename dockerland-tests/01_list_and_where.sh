#!/usr/bin/env bash
# Tests: pew list, --hosts group targeting (project group vs per-type group
# from dockerland's ansible-inventory output), and --where narrowing against
# both a gathered Ansible fact and a plain inventory variable.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
cleanup() { dt::cleanup "${CONTAINERS[@]}"; }
trap cleanup EXIT

dt::log "bringing up ubuntu + rocky ssh containers..."
UBUNTU="$(dt::up ubuntu-latest-ssh.toml)" || exit 1
CONTAINERS+=("$UBUNTU")
ROCKY="$(dt::up rocky-latest-ssh.toml)" || exit 1
CONTAINERS+=("$ROCKY")

INV="$(dt::generate_inventory)"
# Append a plain inventory var (not a fact) to the whole-project group, to
# exercise --where's inventory-var matching path distinctly from its
# fact-gathering path.
echo "env=staging" >> "$INV"

eval "$(dt::activate_cmd)"
IARGS=(-i "$INV" -U testuser)

# --- pew list resolves the whole project group ---
out="$("$DT_PEW" list --hosts pew "${IARGS[@]}")"
if echo "$out" | grep -q "ubuntu" && echo "$out" | grep -q "rocky"; then
    dt::pass "list --hosts pew resolves both containers"
else
    dt::fail "list --hosts pew: expected both hosts, got: $out"
fi

# --- pew list resolves just the ubuntu type-group ---
out="$("$DT_PEW" list --hosts ubuntu_latest_ssh "${IARGS[@]}")"
if echo "$out" | grep -q "ubuntu" && ! echo "$out" | grep -q "rocky"; then
    dt::pass "list --hosts ubuntu_latest_ssh resolves only ubuntu"
else
    dt::fail "list --hosts ubuntu_latest_ssh: got: $out"
fi

# --- pew list resolves just the rocky type-group ---
out="$("$DT_PEW" list --hosts rocky_latest_ssh "${IARGS[@]}")"
if echo "$out" | grep -q "rocky" && ! echo "$out" | grep -q "ubuntu"; then
    dt::pass "list --hosts rocky_latest_ssh resolves only rocky"
else
    dt::fail "list --hosts rocky_latest_ssh: got: $out"
fi

# --- --where narrows by a gathered fact (=) ---
out="$("$DT_PEW" list --hosts pew --where 'ansible_distribution=Ubuntu' "${IARGS[@]}")"
if echo "$out" | grep -q "ubuntu" && ! echo "$out" | grep -q "rocky"; then
    dt::pass "--where ansible_distribution=Ubuntu narrows to ubuntu host"
else
    dt::fail "--where ansible_distribution=Ubuntu: got: $out"
fi

# --- --where narrows by a gathered fact (!=) ---
out="$("$DT_PEW" list --hosts pew --where 'ansible_distribution!=Ubuntu' "${IARGS[@]}")"
if echo "$out" | grep -q "rocky" && ! echo "$out" | grep -q "ubuntu"; then
    dt::pass "--where ansible_distribution!=Ubuntu narrows to rocky host"
else
    dt::fail "--where ansible_distribution!=Ubuntu: got: $out"
fi

# --- --where narrows by a gathered fact (~= regex) ---
out="$("$DT_PEW" list --hosts pew --where 'ansible_distribution~=^Ub' "${IARGS[@]}")"
if echo "$out" | grep -q "ubuntu" && ! echo "$out" | grep -q "rocky"; then
    dt::pass "--where ansible_distribution~=^Ub (regex) narrows to ubuntu host"
else
    dt::fail "--where ansible_distribution~=^Ub: got: $out"
fi

# --- --where narrows by a plain inventory var, not a fact ---
out="$("$DT_PEW" list --hosts pew --where 'env=staging' "${IARGS[@]}")"
if echo "$out" | grep -q "ubuntu" && echo "$out" | grep -q "rocky"; then
    dt::pass "--where env=staging (inventory var) matches both hosts"
else
    dt::fail "--where env=staging: got: $out"
fi

# --- --where on a value that matches nothing errors out (rather than
# silently printing an empty list) ---
out="$("$DT_PEW" list --hosts pew --where 'ansible_distribution=Nonexistent' "${IARGS[@]}" 2>&1)"
rc=$?
if [ "$rc" -ne 0 ] && echo "$out" | grep -qi "no hosts matched"; then
    dt::pass "--where with no matches errors out with 'no hosts matched'"
else
    dt::fail "--where ansible_distribution=Nonexistent: expected nonzero exit + 'no hosts matched', got rc=$rc: $out"
fi

dt::summary
