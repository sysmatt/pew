#!/usr/bin/env bash
# Tests: pew run --color/-C (green hostname on rc 0, red otherwise, only on
# a terminal), --no-color, ~/.pewrc's color=yes and $NO_COLOR, the
# --follow/--quiet exclusions, and the one-line failure summary carrying
# each failed host's rc in parentheses (no per-host "returned N" lines).
#
# Color only applies when stdout is a tty, so tty cases run pew under
# script(1), which gives it a pseudo-terminal for stdout and stderr both.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
LOCAL_TMP="/tmp/pew-color-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
cleanup() {
    dt::cleanup "${CONTAINERS[@]}"
    rm -rf "$LOCAL_TMP"
}
trap cleanup EXIT
mkdir -p "$FAKE_HOME"

dt::log "bringing up one ubuntu ssh container..."
UBUNTU="$(dt::up ubuntu-latest-ssh.toml)" || exit 1
CONTAINERS+=("$UBUNTU")

INV="$(dt::generate_inventory)"
eval "$(dt::activate_cmd)"
IARGS=(-i "$INV" -U testuser --hosts ubuntu_latest_ssh --yes)
HOST="$("$DT_PEW" list -i "$INV" --hosts ubuntu_latest_ssh)"

GREEN=$'\e[32m'
RED=$'\e[31m'
RESET=$'\e[0m'

# Run pew on a pseudo-terminal, output (stdout+stderr) on our stdout.
on_tty() {
    script -qec "$(printf '%q ' "$@")" /dev/null | tr -d '\r'
}

# --- -C on a tty: green hostname on success ---
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -C -- echo hi)"
if [[ "$out" == *"${GREEN}${HOST}${RESET}: hi"* ]]; then
    dt::pass "-C colors the hostname green on rc 0"
else
    dt::fail "-C success: got: $(echo "$out" | cat -v)"
fi

# --- -C on a tty: red hostname on failure, and red in the summary too ---
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -C -- false)"
if [[ "$out" == *"${RED}${HOST}${RESET}:"* ]]; then
    dt::pass "-C colors the hostname red on non-zero rc"
else
    dt::fail "-C failure: got: $(echo "$out" | cat -v)"
fi
if [[ "$out" == *"1/1 host(s) failed: ${RED}${HOST}${RESET} (1)"* ]]; then
    dt::pass "-C colors failed hostnames red in the summary, rc uncolored"
else
    dt::fail "-C summary: got: $(echo "$out" | cat -v)"
fi

# --- -C -m: the ### HOST ### banner is colored ---
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -C -m -- echo hi)"
upper="$(echo "$HOST" | tr '[:lower:]' '[:upper:]')"
if [[ "$out" == *"### ${GREEN}${upper}${RESET} ###"* ]]; then
    dt::pass "-C -m colors the hostname in the multiline banner"
else
    dt::fail "-C -m: got: $(echo "$out" | cat -v)"
fi

# --- -C piped (not a tty): no escape codes at all ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -C -- false 2>&1)"
if [[ "$out" != *$'\e['* ]]; then
    dt::pass "-C is a no-op when output isn't a terminal"
else
    dt::fail "-C piped: escape codes leaked: $(echo "$out" | cat -v)"
fi

# --- ~/.pewrc color=yes turns it on; --no-color and $NO_COLOR turn it off ---
echo "color=yes" > "$FAKE_HOME/.pewrc"
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -- echo hi)"
if [[ "$out" == *"${GREEN}${HOST}${RESET}: hi"* ]]; then
    dt::pass "~/.pewrc color=yes colors output without -C"
else
    dt::fail "pewrc color=yes: got: $(echo "$out" | cat -v)"
fi
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" --no-color -- echo hi)"
if [[ "$out" != *$'\e['* ]]; then
    dt::pass "--no-color overrides ~/.pewrc color=yes"
else
    dt::fail "--no-color: got: $(echo "$out" | cat -v)"
fi
out="$(NO_COLOR=1 HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -- echo hi)"
if [[ "$out" != *$'\e['* ]]; then
    dt::pass "\$NO_COLOR overrides ~/.pewrc color=yes"
else
    dt::fail "NO_COLOR: got: $(echo "$out" | cat -v)"
fi
out="$(NO_COLOR=1 HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -C -- echo hi)"
if [[ "$out" == *"${GREEN}${HOST}${RESET}: hi"* ]]; then
    dt::pass "explicit -C wins over \$NO_COLOR"
else
    dt::fail "-C with NO_COLOR: got: $(echo "$out" | cat -v)"
fi

# --- color=yes from ~/.pewrc is quietly dropped for --follow/--quiet ---
if out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" --follow -- echo hi)" \
        && [[ "$out" == *"hi"* && "$out" != *$'\e['* ]]; then
    dt::pass "~/.pewrc color=yes doesn't break --follow (just no color)"
else
    dt::fail "pewrc color=yes + --follow: got: $(echo "$out" | cat -v)"
fi
rm -f "$FAKE_HOME/.pewrc"

# --- explicit -C with --follow or --quiet is an error ---
for flag in --follow --quiet; do
    out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -C "$flag" -- echo hi 2>&1)"
    if [ $? -ne 0 ] && echo "$out" | grep -q "can't be combined with $flag"; then
        dt::pass "-C $flag errors out"
    else
        dt::fail "-C $flag: expected error, got: $out"
    fi
done

# --- summary: rc in parentheses, no per-host "returned N" lines ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -- ls /nonexistent-pew-path 2>&1)"
if echo "$out" | grep -qF "pew: warning: 1/1 host(s) failed: $HOST (2)"; then
    dt::pass "failure summary shows the host's rc in parentheses"
else
    dt::fail "summary rc: got: $out"
fi
if ! echo "$out" | grep -q "returned"; then
    dt::pass "no per-host 'returned N' warning lines"
else
    dt::fail "per-host returned line still printed: $out"
fi
for flag in --follow --parallel; do
    out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" "$flag" -- false 2>&1)"
    if echo "$out" | grep -qF "1/1 host(s) failed: $HOST (1)" && ! echo "$out" | grep -q "returned"; then
        dt::pass "$flag uses the same rc-in-summary format"
    else
        dt::fail "$flag summary: got: $out"
    fi
done

# --- a timeout's note rides along with its rc ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" --timeout 1 -- sleep 10 2>&1)"
if echo "$out" | grep -qF "$HOST (124, timed out after 1"; then
    dt::pass "timeout shows as (124, timed out after ...) in the summary"
else
    dt::fail "timeout summary: got: $out"
fi

dt::summary
