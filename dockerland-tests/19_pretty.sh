#!/usr/bin/env bash
# Tests: --pretty's live status footer for run and copy - drawn on a
# terminal, never when piped or for --dry-run, on via ~/.pewrc's
# pretty=yes or as pez, off via --no-pretty - and the final status line
# it leaves behind.
#
# No containers needed: a fake ssh on PATH runs each "remote" command
# locally (bad.example.test exits 255), and script(1) gives pew a
# pseudo-terminal. That captures the raw byte stream, footer redraws and
# all, so these check for the footer's escapes and text, not for what the
# screen ends up showing.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

LOCAL_TMP="/tmp/pew-pretty-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
BIN="$LOCAL_TMP/bin"
trap 'rm -rf "$LOCAL_TMP"' EXIT
mkdir -p "$FAKE_HOME" "$BIN"
ln -s "$DT_PEW" "$BIN/pez"

cat > "$BIN/ssh" <<'SSH'
#!/bin/bash
# Skip ssh options, take the first non-option as the host, run the rest.
while [ $# -gt 0 ]; do
    case "$1" in -o|-p|-l|-i|-F) shift 2;; -*) shift;; *) host="${1#*@}"; shift; break;; esac
done
sleep 0.5
[ "$host" = bad.example.test ] && { echo "bad broke"; exit 255; }
eval "$*"
SSH
chmod +x "$BIN/ssh"
export PATH="$BIN:$PATH"

INV="$LOCAL_TMP/inventory.ini"
cat > "$INV" <<'INI'
[lab]
good1.example.test
good2.example.test
bad.example.test
INI
IARGS=(-i "$INV" lab -Z)

on_tty() {
    script -qec "$(printf '%q ' "$@")" /dev/null | tr -d '\r'
}
HIDE=$'\e[?25l'
SHOW=$'\e[?25h'

check() {
    local label="$1" ok="$2" out="$3"
    if [ "$ok" = yes ]; then dt::pass "$label"; else dt::fail "$label: got (tail): $(echo "$out" | tail -c 600 | cat -v)"; fi
}
has() { [[ "$1" == *"$2"* ]] && echo yes || echo no; }
lacks() { [[ "$1" != *"$2"* ]] && echo yes || echo no; }
plain() { sed 's/\x1b\[[0-9;?]*[A-Za-z]//g' <<< "$1"; }

# --- parallel run on a tty: footer drawn, then the final status line ---
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -P -j 1 --pretty -- echo hi)"
check "--pretty hides the cursor and draws the footer" "$(has "$out" "$HIDE")" "$out"
check "-j 1 shows the other hosts as waiting" "$(has "$out" "waiting: ")" "$out"
check "footer shows a running host with its elapsed time" \
    "$( [[ "$(plain "$out")" =~ good1\.example\.test\ +[0-9.]+s ]] && echo yes || echo no)" "$out"
check "results are still printed" "$(has "$(plain "$out")" "good2.example.test: hi")" "$out"
check "failure summary keeps its rc" "$(has "$(plain "$out")" "1/3 host(s) failed: bad.example.test (255)")" "$out"
check "final status line counts ok/failed" "$(has "$out" "pew: done in")" "$out"
check "final line: 2 ok, 1 failed" "$( [[ "$out" == *"2 ok"*"1 failed"* ]] && echo yes || echo no)" "$out"
check "cursor restored at the end" "$(has "$out" "$SHOW")" "$out"
check "--pretty implies --color" "$(has "$out" $'\e[32mgood1.example.test\e[0m')" "$out"

# --- serial, --follow and copy get the footer too ---
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -X --pretty -- echo hi)"
check "serial --pretty draws the footer" "$(has "$out" "waiting: ")" "$out"
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -P -f --pretty -- echo hi)"
check "--follow --pretty works (no --color clash)" "$( [[ "$out" == *"$HIDE"* && "$out" == *"good1.example.test: hi"* ]] && echo yes || echo no)" "$out"
echo data > "$LOCAL_TMP/src.txt"
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" copy "${IARGS[@]}" -P --pretty "$LOCAL_TMP/src.txt" "$LOCAL_TMP/dst.txt")"
check "copy --pretty draws the footer and final line" "$( [[ "$out" == *"$HIDE"* && "$out" == *"pew: done in"* ]] && echo yes || echo no)" "$out"

# --- off when not a terminal, or for --dry-run ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${IARGS[@]}" -P --pretty -- echo hi 2>&1)"
check "--pretty is a no-op when piped" "$( [[ "$out" != *$'\e['* && "$out" != *"done in"* ]] && echo yes || echo no)" "$out"
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -n --pretty -- echo hi)"
check "--pretty does nothing for --dry-run" "$(lacks "$out" "$HIDE")" "$out"

# --- ~/.pewrc pretty=yes, --no-pretty, pez ---
echo "pretty=yes" > "$FAKE_HOME/.pewrc"
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -P -- echo hi)"
check "~/.pewrc pretty=yes turns the footer on" "$(has "$out" "$HIDE")" "$out"
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -P --no-pretty -- echo hi)"
check "--no-pretty overrides ~/.pewrc pretty=yes" "$(lacks "$out" "$HIDE")" "$out"
printf 'pretty=yes\npretty_style=emoji\n' > "$FAKE_HOME/.pewrc"
out="$(HOME="$FAKE_HOME" on_tty "$DT_PEW" run "${IARGS[@]}" -P -- echo hi)"
check "pretty_style=emoji uses emoji" "$(has "$out" "✅ 2 ok")" "$out"
rm -f "$FAKE_HOME/.pewrc"
out="$(HOME="$FAKE_HOME" on_tty "$BIN/pez" -i "$INV" lab echo hi)"
check "pez turns --pretty on" "$( [[ "$out" == *"$HIDE"* && "$out" == *"pew: done in"* ]] && echo yes || echo no)" "$out"

dt::summary
