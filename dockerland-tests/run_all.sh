#!/usr/bin/env bash
# Master runner: runs every feature-test script in sequence, then the
# 10-container stress test, and prints a combined summary. Each script
# tears down its own containers via its own EXIT trap, so this just needs
# to run them one after another and tally results - but does a
# belt-and-suspenders teardown_all first (in case a previous run left
# something behind) and after the last script too.
#
# Usage: ./run_all.sh [--skip-stress]
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

SKIP_STRESS=false
[ "${1:-}" = "--skip-stress" ] && SKIP_STRESS=true

SCRIPTS=(
    01_list_and_where.sh
    02_run_basic.sh
    03_run_parallel_follow.sh
    04_run_timestamps.sh
    05_copy.sh
    06_diff.sh
    07_facts.sh
    08_remote_user_root.sh
    09_pewrc_and_env.sh
)
$SKIP_STRESS || SCRIPTS+=(stress_10.sh)

dt::log "clearing any leftover containers from a previous run..."
dt::teardown_all

TOTAL_PASS=0
TOTAL_FAIL=0
FAILED_SCRIPTS=()

for script in "${SCRIPTS[@]}"; do
    echo
    echo "########################################################################"
    echo "# $script"
    echo "########################################################################"
    ./"$script"
    rc=$?
    # Each script's own dt::summary already printed its "N passed, M failed"
    # line; re-derive the numbers from its exit code plus a rerun of the
    # same script would double the work, so just track pass/fail at the
    # script level here and let each script's own output carry the detail.
    if [ "$rc" -eq 0 ]; then
        TOTAL_PASS=$((TOTAL_PASS + 1))
    else
        TOTAL_FAIL=$((TOTAL_FAIL + 1))
        FAILED_SCRIPTS+=("$script")
    fi
done

dt::log "final cleanup..."
dt::teardown_all

echo
echo "########################################################################"
echo "# SUMMARY: $TOTAL_PASS/${#SCRIPTS[@]} scripts passed"
echo "########################################################################"
if [ "$TOTAL_FAIL" -gt 0 ]; then
    echo "Failed: ${FAILED_SCRIPTS[*]}"
    exit 1
fi
exit 0
