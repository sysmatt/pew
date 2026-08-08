#!/usr/bin/env bash
# Tests: pew run --timestamp / --strftime / --ts / --tss, their mutual
# exclusivity, and that --log-prefix files stay timestamp-free even when
# --timestamp is also set (per the CLI help: "Never written to --log-prefix
# files, which stay clean").
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
LOGPREFIX="/tmp/pew-ts-test-$$"
cleanup() {
    dt::cleanup "${CONTAINERS[@]}"
    rm -f "${LOGPREFIX}".*
}
trap cleanup EXIT

dt::log "bringing up one ubuntu ssh container..."
UBUNTU="$(dt::up ubuntu-latest-ssh.toml)" || exit 1
CONTAINERS+=("$UBUNTU")

INV="$(dt::generate_inventory)"
eval "$(dt::activate_cmd)"
IARGS=(-i "$INV" -U testuser --hosts ubuntu_latest_ssh --yes)

# --- --timestamp: default '%Y-%m-%d %H:%M:%S' prefix ---
out="$("$DT_PEW" run "${IARGS[@]}" --timestamp -- echo hi)"
if echo "$out" | grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2} pew-ubuntu-latest-ssh-[a-z]+: hi$'; then
    dt::pass "--timestamp prefixes lines with 'YYYY-MM-DD HH:MM:SS hostname: line'"
else
    dt::fail "--timestamp: unexpected format: $out"
fi

# --- --strftime with a custom format ---
out="$("$DT_PEW" run "${IARGS[@]}" --strftime '%H:%M' -- echo hi)"
if echo "$out" | grep -qE '^[0-9]{2}:[0-9]{2} pew-ubuntu-latest-ssh-[a-z]+: hi$'; then
    dt::pass "--strftime '%H:%M' uses the custom format"
else
    dt::fail "--strftime: unexpected format: $out"
fi

# --- --ts shorthand: %Y%m%d%H%M%S ---
out="$("$DT_PEW" run "${IARGS[@]}" --ts -- echo hi)"
if echo "$out" | grep -qE '^[0-9]{14} pew-ubuntu-latest-ssh-[a-z]+: hi$'; then
    dt::pass "--ts uses the 14-digit YYYYMMDDHHMMSS shorthand"
else
    dt::fail "--ts: unexpected format: $out"
fi

# --- --tss shorthand: %Y%m%d%H%M%S.%f (with subseconds) ---
out="$("$DT_PEW" run "${IARGS[@]}" --tss -- echo hi)"
if echo "$out" | grep -qE '^[0-9]{14}\.[0-9]+ pew-ubuntu-latest-ssh-[a-z]+: hi$'; then
    dt::pass "--tss uses the 14-digit + subsecond shorthand"
else
    dt::fail "--tss: unexpected format: $out"
fi

# --- --timestamp / --strftime / --ts / --tss are mutually exclusive ---
allpass=true
for combo in "--timestamp --ts" "--ts --tss" "--strftime %H --timestamp"; do
    if "$DT_PEW" run "${IARGS[@]}" $combo -- echo hi >/dev/null 2>&1; then
        allpass=false
        dt::log "combo '$combo' unexpectedly succeeded"
    fi
done
if $allpass; then
    dt::pass "--timestamp/--strftime/--ts/--tss are mutually exclusive"
else
    dt::fail "one or more timestamp-flag combos did not error as expected"
fi

# --- --log-prefix file stays clean even with --timestamp set ---
"$DT_PEW" run "${IARGS[@]}" --timestamp --log-prefix "$LOGPREFIX" -- echo hi >/dev/null
logfile="$(ls "${LOGPREFIX}".* 2>/dev/null | head -1)"
if [ -n "$logfile" ] && ! grep -qE '^[0-9]{4}-[0-9]{2}-[0-9]{2}' "$logfile" && grep -qx "hi" "$logfile"; then
    dt::pass "--log-prefix file has no timestamp even when --timestamp is set"
else
    dt::fail "--log-prefix file: expected clean 'hi' line, got: $(cat "$logfile" 2>&1)"
fi

dt::summary
