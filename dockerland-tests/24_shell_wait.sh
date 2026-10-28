#!/usr/bin/env bash
# Tests: pew shell waiting for the ssh port (spinner/plain line, "is up
# after", the bell only after a wait), the no-wait fast path (silent, no
# bell), --no-wait, --wait-timeout, and --persist reconnecting after any
# session end (a 255 drop and an exit-0 logout alike) until Ctrl-C.
#
# The container's sshd is its PID 1, so "rebooting" is simulated with a
# TCP forwarder in front of it that only starts listening after a delay:
# pew waits on the forwarder's port, then a real ssh session goes through.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
LOCAL_TMP="/tmp/pew-shell-wait-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
cleanup() {
    [ -n "${FWD_PID:-}" ] && kill "$FWD_PID" 2>/dev/null
    dt::cleanup "${CONTAINERS[@]}"
    rm -rf "$LOCAL_TMP"
}
trap cleanup EXIT
mkdir -p "$FAKE_HOME"

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

# forward LISTEN_PORT TARGET_PORT DELAY: after DELAY seconds, forward
# 127.0.0.1:LISTEN_PORT to 127.0.0.1:TARGET_PORT until killed.
forward() {
    python3 - "$@" <<'PY' &
import socket, sys, threading, time
listen, target, delay = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])
time.sleep(delay)
srv = socket.socket(); srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
srv.bind(("127.0.0.1", listen)); srv.listen(8)
def pipe(a, b):
    try:
        while (data := a.recv(65536)):
            b.sendall(data)
    except OSError:
        pass
    for s in (a, b):
        try: s.shutdown(socket.SHUT_RDWR)
        except OSError: pass
while True:
    c, _ = srv.accept()
    u = socket.create_connection(("127.0.0.1", target))
    threading.Thread(target=pipe, args=(c, u), daemon=True).start()
    threading.Thread(target=pipe, args=(u, c), daemon=True).start()
PY
    FWD_PID=$!
}

free_port() {
    python3 -c 'import socket; s=socket.socket(); s.bind(("127.0.0.1",0)); print(s.getsockname()[1])'
}

dt::log "bringing up one ubuntu ssh container..."
UBUNTU="$(dt::up ubuntu-latest-ssh.toml)" || exit 1
CONTAINERS+=("$UBUNTU")
INV="$(dt::generate_inventory)"
eval "$(dt::activate_cmd)"
IARGS=(-i "$INV" -U testuser)
HOST="$("$DT_PEW" list -i "$INV" --hosts ubuntu_latest_ssh)"

# The container's mapped ssh port, from the command pew would run.
PORT="$("$DT_PEW" shell "${IARGS[@]}" "$HOST" -n | grep -o 'Port=[0-9]*' | cut -d= -f2)"
FWD="$(free_port)"
FWD_INV="$LOCAL_TMP/fwd.ini"
sed "s/ansible_port=$PORT\b/ansible_port=$FWD/" "$INV" > "$FWD_INV"
if [ -n "$PORT" ] && grep -q "ansible_port=$FWD" "$FWD_INV"; then
    dt::pass "found the container's ssh port ($PORT), forwarder on $FWD"
else
    dt::fail "couldn't find the container's ssh port (got '$PORT')"; dt::summary; exit 1
fi

# --- already up: straight in, no waiting line, no bell ---
err="$(HOME="$FAKE_HOME" "$DT_PEW" shell "${IARGS[@]}" "$HOST" -- echo fast-path 2>&1 >/dev/null </dev/null)"
out="$(HOME="$FAKE_HOME" "$DT_PEW" shell "${IARGS[@]}" "$HOST" -- echo fast-path 2>/dev/null </dev/null)"
check "up already: the session runs" "$out" "fast-path"
check_not "up already: no waiting line" "$err" "waiting for"
check_not "up already: no bell" "$err" $'\a'

# --- port opens 3s later: waits, says so, rings, then connects ---
forward "$FWD" "$PORT" 3
start=$SECONDS
out="$(HOME="$FAKE_HOME" "$DT_PEW" shell -i "$FWD_INV" -U testuser "$HOST" -- echo after-wait 2>&1 </dev/null)"
rc=$?
check "waits for the port" "$out" "waiting for $HOST (127.0.0.1) to open tcp/$FWD"
check "says when it's up" "$out" "$HOST is up after"
check "rings the bell after a wait" "$out" $'\a'
check "then the session runs" "$out" "after-wait"
[ "$rc" -eq 0 ] && [ $((SECONDS - start)) -ge 2 ] && dt::pass "waited, then exit 0" \
    || dt::fail "wait: rc=$rc after $((SECONDS - start))s"
kill "$FWD_PID"; wait "$FWD_PID" 2>/dev/null; FWD_PID=

# --- nothing listening: --no-wait fails straight away, --wait-timeout gives up ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" shell -i "$FWD_INV" -U testuser "$HOST" --no-wait -- true 2>&1 </dev/null)"
rc=$?
check_not "--no-wait doesn't wait" "$out" "waiting for"
[ "$rc" -eq 255 ] && dt::pass "--no-wait: ssh's own 255" || dt::fail "--no-wait: rc=$rc"
out="$(HOME="$FAKE_HOME" "$DT_PEW" shell -i "$FWD_INV" -U testuser "$HOST" --wait-timeout 2 -- true 2>&1 </dev/null)"
rc=$?
check "--wait-timeout gives up" "$out" "gave up waiting for $HOST"
[ "$rc" -ne 0 ] && dt::pass "--wait-timeout: non-zero exit" || dt::fail "--wait-timeout: rc=0"

# --- --persist: reconnects whatever the exit code, until Ctrl-C ---
# Session 1 runs 11s (long enough to count as having been up, for the
# bell) and ends like a drop (255), session 2 like a logout (0), and later
# ones log out after 2s; Ctrl-C, repeated, lands in a countdown between
# them and ends it (one landing mid-session is ignored by pew). pew runs
# with SIGINT back at its default: a background job here starts with it
# ignored, which would stop kill -INT ever reaching it.
script='n=$(cat /tmp/pew-persist 2>/dev/null || echo 0); echo $((n+1)) > /tmp/pew-persist
echo "session-$((n+1))"; [ "$n" -ge 2 ] && sleep 2; [ "$n" -eq 0 ] && { sleep 11; exit 255; }; exit 0'
dt::exec "$UBUNTU" rm -f /tmp/pew-persist >/dev/null 2>&1
HOME="$FAKE_HOME" python3 -c 'import os, signal, sys
signal.signal(signal.SIGINT, signal.SIG_DFL); os.execvp(sys.argv[1], sys.argv[1:])' \
    "$DT_PEW" shell "${IARGS[@]}" "$HOST" --persist --persist-delay 1 -- "$script" \
    > "$LOCAL_TMP/persist.out" 2>&1 </dev/null &
PEW_PID=$!
for _ in $(seq 80); do grep -q "session-3" "$LOCAL_TMP/persist.out" && break; sleep 0.5; done
for _ in $(seq 20); do kill -INT "$PEW_PID" 2>/dev/null || break; sleep 0.5; done
wait "$PEW_PID"; rc=$?
out="$(cat "$LOCAL_TMP/persist.out")"
check "--persist: says a drop was a disconnect" "$out" "session to $HOST was disconnected · exit 255"
check "--persist: reconnects after a drop" "$out" "session-2"
check "--persist: says a logout ended it" "$out" "session to $HOST ended · exit 0"
check "--persist: reconnects after a logout too" "$out" "session-3"
check "--persist: bell on the way back" "$out" $'\a'
check "--persist: Ctrl-C says it's exiting" "$out" "exiting · last session exit"
kill "$PEW_PID" 2>/dev/null
dt::exec "$UBUNTU" rm -f /tmp/pew-persist >/dev/null 2>&1

out="$(HOME="$FAKE_HOME" "$DT_PEW" shell "${IARGS[@]}" "$HOST" --persist -n -- true)"
check "--persist adds keepalives" "$out" "ServerAliveInterval=5"

dt::summary
