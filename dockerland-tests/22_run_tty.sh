#!/usr/bin/env bash
# Tests: pew run --tty/-T (ssh -tt with a remote stty/pager prefix), the
# remote pty's width (pew's terminal minus the host prefix, $COLUMNS off a
# terminal, never below 40), stdin at /dev/null, no "Connection to ...
# closed." noise, rc/timeout pass-through, --follow, and the soft defaults
# from ~/.pewrc's tty=yes and the pez alias (terminal only, $NO_COLOR and
# --no-tty turn them off).
#
# Soft-default cases need pew's stdout on a tty, so they run pew under
# script(1), like 18_run_color.sh.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
LOCAL_TMP="/tmp/pew-tty-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
BIN="$LOCAL_TMP/bin"
cleanup() {
    dt::cleanup "${CONTAINERS[@]}"
    rm -rf "$LOCAL_TMP"
}
trap cleanup EXIT
mkdir -p "$FAKE_HOME" "$BIN"
ln -s "$DT_PEW" "$BIN/pez"

dt::log "bringing up one ubuntu ssh container..."
UBUNTU="$(dt::up ubuntu-latest-ssh.toml)" || exit 1
CONTAINERS+=("$UBUNTU")

INV="$(dt::generate_inventory)"
eval "$(dt::activate_cmd)"
IARGS=(-i "$INV" -U testuser --hosts ubuntu_latest_ssh --yes)
HOST="$("$DT_PEW" list -i "$INV" --hosts ubuntu_latest_ssh)"
PREFIX=$(( ${#HOST} + 2 ))

# Run pew on a pseudo-terminal COLS wide, output (stdout+stderr) on our stdout.
on_tty() {
    local cols="$1"; shift
    script -qec "stty cols $cols; $(printf '%q ' "$@")" /dev/null | tr -d '\r'
}

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

# --- -T: the remote command is on a tty, and tools color for it ---
out="$(HOME="$FAKE_HOME" COLUMNS=120 "$DT_PEW" run "${IARGS[@]}" -T -- 'tty >/dev/null && echo on-a-tty' 2>&1)"
check "-T gives the remote command a tty" "$out" "$HOST: on-a-tty"
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -T -- ls -d / --color=auto 2>&1)"
check "-T: ls --color=auto colors its output" "$out" $'\e['
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -- 'tty' 2>&1)"
check "without -T there's no tty" "$out" "not a tty"

# --- no CRs, no "Connection to ... closed." ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -T -- 'echo one; echo two' 2>&1)"
check_not "-T output has no CRs" "$out" $'\r'
check_not "-T drops ssh's 'Connection to ... closed.'" "$out" "closed."

# --- width: COLUMNS off a terminal, minus the host prefix ---
out="$(HOME="$FAKE_HOME" COLUMNS=150 "$DT_PEW" run "${IARGS[@]}" -T -- 'stty size; echo cols=$COLUMNS' 2>&1)"
want=$(( 150 - PREFIX ))
check "remote stty width is \$COLUMNS minus the host prefix" "$out" "$HOST: 0 $want"
check "remote \$COLUMNS matches" "$out" "cols=$want"
out="$(HOME="$FAKE_HOME" COLUMNS=150 "$DT_PEW" run "${IARGS[@]}" -T -q -- 'stty size' 2>&1)"
check "--quiet gets the full width" "$out" "0 150"
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -T -- 'stty size' 2>&1)"
check "unset \$COLUMNS off a terminal falls back to 200" "$out" "0 $(( 200 - PREFIX ))"
out="$(HOME="$FAKE_HOME" COLUMNS=20 "$DT_PEW" run "${IARGS[@]}" -T -- 'stty size' 2>&1)"
check "width never goes below 40" "$out" "0 40"

# --- width: pew's own terminal, minus the timestamp too ---
out="$(HOME="$FAKE_HOME" on_tty 130 "$DT_PEW" run "${IARGS[@]}" -T --ts -- 'stty size')"
check "on a terminal: its width minus timestamp and host prefix" "$out" "0 $(( 130 - 15 - PREFIX ))"

# --- pagers are cat, stdin is /dev/null ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -T -- 'echo $PAGER $GIT_PAGER $SYSTEMD_PAGER' 2>&1)"
check "pagers are cat" "$out" "$HOST: cat cat cat"
# read -t: on a pty, EOF from /dev/null may never arrive, so don't wait on it
out="$(echo piped | HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -T -- 'read -t 2 x; echo "got=$x"' 2>&1)"
check "-T reads stdin from /dev/null" "$out" "$HOST: got="
check_not "-T doesn't pass pew's stdin through" "$out" "piped"

# --- rc, timeout and --follow pass through ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -T -- 'exit 3' 2>&1)"
check "-T keeps the remote rc" "$out" "1/1 host(s) failed: $HOST (3)"
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -T --timeout 1 -- sleep 10 2>&1)"
check "-T honors --timeout" "$out" "$HOST (124, timed out after 1"
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -T -f -- 'echo a; echo b' 2>&1)"
check "-T works with --follow" "$out" "$HOST: a"$'\n'"$HOST: b"

# --- soft defaults: ~/.pewrc tty=yes and pez, terminal only ---
echo "tty=yes" > "$FAKE_HOME/.pewrc"
out="$(HOME="$FAKE_HOME" on_tty 100 "$DT_PEW" run "${IARGS[@]}" -- tty)"
check_not "~/.pewrc tty=yes turns it on, on a terminal" "$out" "not a tty"
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -- tty 2>&1)"
check "~/.pewrc tty=yes is off when piped" "$out" "not a tty"
out="$(HOME="$FAKE_HOME" on_tty 100 "$DT_PEW" run "${IARGS[@]}" --no-tty -- tty)"
check "--no-tty overrides ~/.pewrc tty=yes" "$out" "not a tty"
out="$(NO_COLOR=1 HOME="$FAKE_HOME" on_tty 100 "$DT_PEW" run "${IARGS[@]}" -- tty)"
check "\$NO_COLOR overrides ~/.pewrc tty=yes" "$out" "not a tty"
rm -f "$FAKE_HOME/.pewrc"
out="$(HOME="$FAKE_HOME" on_tty 100 "$BIN/pez" "${IARGS[@]}" -- tty)"
check_not "pez turns it on, on a terminal" "$out" "not a tty"
out="$(HOME="$FAKE_HOME" "$BIN/pez" "${IARGS[@]}" -- tty 2>&1)"
check "pez leaves it off when piped" "$out" "not a tty"

dt::summary
