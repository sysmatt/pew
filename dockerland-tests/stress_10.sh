#!/usr/bin/env bash
# Stress test: bring up 10 dockerland containers at once (8 ubuntu-latest-ssh
# + 2 rocky-latest-ssh) to exercise dockerland's instance-naming/allocation,
# port-allocation, and ssh_config/inventory regeneration at scale, then run
# pew across all 10 concurrently (list, --where distro targeting, --parallel
# run, and a parallel copy) to confirm nothing that works with 1-3 containers
# quietly breaks once there are 10 in play at once.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
LOCAL_TMP="/tmp/pew-stress10-test-$$"
cleanup() {
    dt::log "tearing down ${#CONTAINERS[@]} stress-test containers..."
    dt::cleanup "${CONTAINERS[@]}"
    rm -rf "$LOCAL_TMP"
}
trap cleanup EXIT
mkdir -p "$LOCAL_TMP"

dt::log "bringing up 10 containers (8 ubuntu-latest-ssh + 2 rocky-latest-ssh)..."
start=$SECONDS
for i in 1 2 3 4 5 6 7 8; do
    c="$(dt::up ubuntu-latest-ssh.toml)" || { dt::fail "container $i (ubuntu) failed to come up"; exit 1; }
    CONTAINERS+=("$c")
done
for i in 1 2; do
    c="$(dt::up rocky-latest-ssh.toml)" || { dt::fail "container $((i + 8)) (rocky) failed to come up"; exit 1; }
    CONTAINERS+=("$c")
done
up_elapsed=$((SECONDS - start))
dt::log "all 10 containers up in ${up_elapsed}s"

if [ "${#CONTAINERS[@]}" -eq 10 ]; then
    dt::pass "dockerland brought up all 10 containers (${up_elapsed}s)"
else
    dt::fail "expected 10 containers, dt::up succeeded for ${#CONTAINERS[@]}"
fi

# --- all 10 got distinct names (instance-allocation didn't collide) ---
distinct_names=$(printf '%s\n' "${CONTAINERS[@]}" | sort -u | wc -l)
if [ "$distinct_names" -eq 10 ]; then
    dt::pass "all 10 containers have distinct names (no instance-allocation collision)"
else
    dt::fail "expected 10 distinct names, got $distinct_names: ${CONTAINERS[*]}"
fi

# --- dockerland list shows all 10 ---
list_out="$(dockerland list --project "$DT_PROJECT" --format table)"
list_count=$(printf '%s\n' "$list_out" | grep -c "sysmatt-pew-")
if [ "$list_count" -eq 10 ]; then
    dt::pass "dockerland list shows all 10 containers"
else
    dt::fail "dockerland list: expected 10 rows, got $list_count"
fi

# --- ansible-inventory groups all 10 correctly by type, 8 ubuntu + 2 rocky ---
INV="$(dt::generate_inventory)"
ubuntu_count=$(grep -c "^pew-ubuntu-latest-ssh-" "$INV")
rocky_count=$(grep -c "^pew-rocky-latest-ssh-" "$INV")
if [ "$ubuntu_count" -eq 8 ] && [ "$rocky_count" -eq 2 ]; then
    dt::pass "generated inventory has 8 ubuntu + 2 rocky hosts"
else
    dt::fail "inventory counts: ubuntu=$ubuntu_count rocky=$rocky_count (expected 8/2)"
fi

eval "$(dt::activate_cmd)"
IARGS=(-i "$INV" -U testuser --hosts pew)

# --- pew list resolves all 10 across both types ---
out="$("$DT_PEW" list "${IARGS[@]}")"
count=$(echo "$out" | grep -c "pew-")
if [ "$count" -eq 10 ]; then
    dt::pass "pew list --hosts pew resolves all 10 hosts"
else
    dt::fail "pew list: expected 10 hosts, got $count. Output: $out"
fi

# --- --where distro targeting still isolates 8 vs 2 correctly at this scale ---
out="$("$DT_PEW" list "${IARGS[@]}" --where 'ansible_distribution=Ubuntu')"
ubuntu_matched=$(echo "$out" | grep -c "pew-ubuntu-latest-ssh-")
rocky_matched=$(echo "$out" | grep -c "pew-rocky-latest-ssh-")
if [ "$ubuntu_matched" -eq 8 ] && [ "$rocky_matched" -eq 0 ]; then
    dt::pass "--where ansible_distribution=Ubuntu isolates exactly the 8 ubuntu hosts"
else
    dt::fail "--where at scale: ubuntu_matched=$ubuntu_matched rocky_matched=$rocky_matched (expected 8/0)"
fi

# --- pew run --parallel across all 10 gets a distinct response from each ---
out="$("$DT_PEW" run "${IARGS[@]}" --parallel -- hostname)"
distinct=$(echo "$out" | grep -oE '(ubuntu|rocky)-latest-ssh-[a-z]+' | sort -u | wc -l)
if [ "$distinct" -eq 10 ]; then
    dt::pass "pew run --parallel gets a distinct response from all 10 hosts"
else
    dt::fail "--parallel at scale: expected 10 distinct hosts, got $distinct"
fi

# --- a parallel copy lands correctly on all 10 hosts ---
for c in "${CONTAINERS[@]}"; do
    dt::exec "$c" bash -c 'mkdir -p /tmp/stresscopy && chown testuser:testuser /tmp/stresscopy' >/dev/null
done
echo "stress-copy-payload" > "$LOCAL_TMP/payload.txt"
"$DT_PEW" copy "${IARGS[@]}" --parallel "$LOCAL_TMP/payload.txt" /tmp/stresscopy/payload.txt >/dev/null
ok_count=0
for c in "${CONTAINERS[@]}"; do
    content="$(dt::exec "$c" cat /tmp/stresscopy/payload.txt 2>&1 | grep -v 'Output logged')"
    [ "$content" = "stress-copy-payload" ] && ok_count=$((ok_count + 1))
done
if [ "$ok_count" -eq 10 ]; then
    dt::pass "parallel copy delivered the payload correctly to all 10 hosts"
else
    dt::fail "parallel copy: only $ok_count/10 hosts got the correct payload"
fi

dt::summary
