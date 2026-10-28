#!/usr/bin/env bash
# Tests: pew ping. ICMP and --tcp probes, the ssh port from ansible_port
# via ssh -G, -p/--port, refused connections, unresolvable names, hosts
# behind ProxyJump skipped, names sharing an address sharing a target,
# hosts always sorted, --count, -q, the exit code, and the grid on a tty
# (alternate screen in and out, r to reset, q to quit).
#
# Off a terminal pew ping prints a line per probe, which is what most of
# these check. The container gives a real sshd behind a mapped port; the
# rest use a static inventory of loopback/unresolvable/proxied names.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
LOCAL_TMP="/tmp/pew-ping-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
cleanup() {
    dt::cleanup "${CONTAINERS[@]}"
    rm -rf "$LOCAL_TMP"
}
trap cleanup EXIT
mkdir -p "$FAKE_HOME"

STATIC="$LOCAL_TMP/static.ini"
cat > "$STATIC" <<'INI'
[pingt]
lo10 ansible_host=127.0.0.10
lo2 ansible_host=127.0.0.2
lo1 ansible_host=127.0.0.1
lo1-alt ansible_host=127.0.0.1
nowhere ansible_host=no-such-host.invalid
behind-bastion ansible_host=10.9.9.9 ansible_ssh_common_args="-o ProxyJump=bastion.example.test"
INI
SARGS=(-i "$STATIC")

check() {
    local label="$1" out="$2" want="$3"
    if [[ "$out" == *"$want"* ]]; then
        dt::pass "$label"
    else
        dt::fail "$label: wanted *$want*, got: $(echo "$out" | cat -v)"
    fi
}

check_not() {
    local label="$1" out="$2" unwanted="$3"
    if [[ "$out" != *"$unwanted"* ]]; then
        dt::pass "$label"
    else
        dt::fail "$label: didn't want *$unwanted*, got: $(echo "$out" | cat -v)"
    fi
}

# --- static inventory: ICMP to loopback, sorting, shared address ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" ping "${SARGS[@]}" 'lo*' -c 2 -I 0.2 2>&1)"
rc=$?
check "ICMP probe lines" "$out" "lo2         : seq=1 time="
check "names sharing an address share a target" "$out" "lo1, lo1-alt: seq=2 time="
check "summary has a line per name" "$out" "lo1-alt: 2 sent, 2 received, 0% loss"
summary="$(echo "$out" | sed -n '/^$/,$p' | cut -d: -f1 | tr -d ' ' | grep . | tr '\n' ' ')"
check "summary is in natural sort order" "$summary" "lo1 lo1-alt lo2 lo10"
[ "$rc" -eq 0 ] && dt::pass "all replying: exit 0" || dt::fail "all replying: rc=$rc"

out="$(HOME="$FAKE_HOME" "$DT_PEW" ping "${SARGS[@]}" pingt -c 1 -I 0.2 2>&1)"
rc=$?
check "proxied host skipped with a warning" "$out" \
    "skipped 1 proxied host(s): behind-bastion (via bastion.example.test)"
check_not "proxied host isn't probed" "$out" "behind-bastion:"
check "unresolvable name reported" "$out" "nowhere     : no-such-host.invalid unresolved"
[ "$rc" -eq 1 ] && dt::pass "a host that never replied: exit 1" || dt::fail "unresolved: rc=$rc"

out="$(HOME="$FAKE_HOME" "$DT_PEW" ping "${SARGS[@]}" behind-bastion 2>&1)"
check "all proxied is an error" "$out" "nothing to ping"

out="$(HOME="$FAKE_HOME" "$DT_PEW" ping "${SARGS[@]}" lo1:lo2 -q -c 2 -I 0.2 2>&1)"
check_not "-q: no per-probe lines" "$out" "seq="
check "-q: just the summary" "$out" "lo2: 2 sent, 2 received"

out="$(HOME="$FAKE_HOME" "$DT_PEW" ping "${SARGS[@]}" lo1 -p 1 -c 2 -I 0.2 2>&1)"
rc=$?
check "closed port: probe shows refused" "$out" "seq=1 refused time="
check "closed port: summary notes it" "$out" "(port 1 refused)"
[ "$rc" -eq 0 ] && dt::pass "refused counts as a reply: exit 0" || dt::fail "refused: rc=$rc"

# --- a real sshd: --tcp finds the mapped port through ansible_port/ssh -G ---
dt::log "bringing up one ubuntu ssh container..."
UBUNTU="$(dt::up ubuntu-latest-ssh.toml)" || exit 1
CONTAINERS+=("$UBUNTU")
INV="$(dt::generate_inventory)"
eval "$(dt::activate_cmd)"
IARGS=(-i "$INV" --hosts ubuntu_latest_ssh)
HOST="$("$DT_PEW" list "${IARGS[@]}")"

out="$(HOME="$FAKE_HOME" "$DT_PEW" ping "${IARGS[@]}" --tcp -c 2 -I 0.3 -d 2>&1)"
check "--tcp reaches the container's sshd" "$out" "$HOST: seq=2 time="
check "--tcp asks ssh -G for the effective config" "$out" "ssh -G"
out="$(HOME="$FAKE_HOME" "$DT_PEW" ping "${IARGS[@]}" -c 2 -I 0.3 2>&1)"
check "ICMP to the container's address" "$out" "$HOST: 2 sent, 2 received"

# --- the grid on a tty: alternate screen, r, q ---
raw="$( (sleep 2; printf r; sleep 1; printf q) | HOME="$FAKE_HOME" \
    script -qec "stty rows 24 cols 100; $(printf '%q ' "$DT_PEW" ping "${IARGS[@]}" -I 0.3)" /dev/null)"
check "grid enters the alternate screen" "$raw" $'\e[?1049h'
check "grid leaves the alternate screen" "$raw" $'\e[?1049l'
check "grid header" "$raw" "pew ping · icmp · 1 target(s)"
check "r resets" "$raw" "stats since reset at"
check "q quits with the summary" "$raw" "$HOST: "

# --- the container goes away: its port now refuses or times out ---
dt::down "$UBUNTU"
CONTAINERS=()
out="$(HOME="$FAKE_HOME" "$DT_PEW" ping -i "$INV" --hosts ubuntu_latest_ssh --tcp -c 2 -I 0.3 2>&1)"
check_not "stopped container: no successful probe" "$out" ": seq=1 time="
check "stopped container: summary shows it" "$out" "$HOST: 2 sent"

dt::summary
