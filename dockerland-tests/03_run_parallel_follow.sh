#!/usr/bin/env bash
# Tests: pew run --parallel/--jobs concurrency, --follow streaming
# correctness, --follow/--multiline mutual exclusion, --continue-on-fail,
# and --timeout. Needs multiple hosts of the same type to make concurrency
# and continue-on-fail behavior observable, so brings up three ubuntu
# containers.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
cleanup() { dt::cleanup "${CONTAINERS[@]}"; }
trap cleanup EXIT

dt::log "bringing up three ubuntu ssh containers..."
for i in 1 2 3; do
    c="$(dt::up ubuntu-latest-ssh.toml)" || exit 1
    CONTAINERS+=("$c")
done

INV="$(dt::generate_inventory)"
eval "$(dt::activate_cmd)"
IARGS=(-i "$INV" -U testuser --hosts ubuntu_latest_ssh)

# --- --parallel runs across all matched hosts, implies --yes ---
out="$("$DT_PEW" run "${IARGS[@]}" --parallel -- hostname)"
distinct=$(echo "$out" | grep -oE 'ubuntu-latest-ssh-[a-z]+' | sort -u | wc -l)
if [ "$distinct" -eq 3 ]; then
    dt::pass "--parallel runs on all 3 hosts without prompting (--yes implied)"
else
    dt::fail "--parallel: expected 3 distinct hosts, got $distinct. Output: $out"
fi

# --- --jobs limits concurrency (timing-based) ---
start=$SECONDS
"$DT_PEW" run "${IARGS[@]}" --parallel --jobs 1 -- sleep 2 >/dev/null
serial_elapsed=$((SECONDS - start))

start=$SECONDS
"$DT_PEW" run "${IARGS[@]}" --parallel --jobs 3 -- sleep 2 >/dev/null
parallel_elapsed=$((SECONDS - start))

if [ "$serial_elapsed" -ge 5 ] && [ "$parallel_elapsed" -le 4 ]; then
    dt::pass "--jobs 1 serializes (${serial_elapsed}s) vs --jobs 3 concurrent (${parallel_elapsed}s)"
else
    dt::fail "--jobs timing: jobs=1 took ${serial_elapsed}s, jobs=3 took ${parallel_elapsed}s (expected jobs=1 >=5s, jobs=3 <=4s)"
fi

# --- --follow produces correct, complete multi-line output per host ---
out="$("$DT_PEW" run "${IARGS[@]}" --parallel --follow -- printf 'line1\\nline2\\nline3\\n')"
ok=true
for marker in line1 line2 line3; do
    count=$(echo "$out" | grep -c "$marker")
    [ "$count" -ge 3 ] || ok=false
done
if $ok; then
    dt::pass "--follow (parallel) delivers all 3 lines from all 3 hosts"
else
    dt::fail "--follow output incomplete: $out"
fi

out="$("$DT_PEW" run "${IARGS[@]}" --yes --follow -- printf 'a\\nb\\nc\\n')"
ok=true
for marker in a b c; do
    count=$(echo "$out" | grep -c "^[a-z0-9._-]*: $marker\$\|^$marker\$")
    [ "$count" -ge 1 ] || ok=false
done
if $ok; then
    dt::pass "--follow (serial) works too, not just under --parallel"
else
    dt::fail "--follow serial output incomplete: $out"
fi

# --- --follow and --multiline are mutually exclusive ---
if "$DT_PEW" run "${IARGS[@]}" --follow --multiline -- echo hi >/dev/null 2>&1; then
    dt::fail "--follow --multiline: expected argparse error, got success"
else
    dt::pass "--follow --multiline together correctly errors out"
fi

# --- --continue-on-fail: without it, serial run stops after first failure ---
# (count pew's own "<host> returned 1" warnings rather than command stdout -
# `false` produces none, and this sidesteps ssh's argv-joining mangling any
# quoted remote shell metacharacters we'd otherwise need)
out="$("$DT_PEW" run "${IARGS[@]}" --yes -- false 2>&1)"
ran_count=$(echo "$out" | grep -c "returned 1")
if [ "$ran_count" -eq 1 ]; then
    dt::pass "without --continue-on-fail, serial run stops after first failure (1 host ran)"
else
    dt::fail "expected exactly 1 host to run before stopping, got $ran_count. Output: $out"
fi

# --- --continue-on-fail: with it, all hosts are attempted despite failures ---
out="$("$DT_PEW" run "${IARGS[@]}" --yes --continue-on-fail -- false 2>&1)"
ran_count=$(echo "$out" | grep -c "returned 1")
if [ "$ran_count" -eq 3 ]; then
    dt::pass "--continue-on-fail runs all 3 hosts despite each one failing"
else
    dt::fail "expected 3 hosts to run with --continue-on-fail, got $ran_count. Output: $out"
fi

# --- --timeout kills a long-running command ---
start=$SECONDS
if "$DT_PEW" run "${IARGS[@]}" --yes --timeout 2 -- sleep 15 >/dev/null 2>&1; then
    ok=false
else
    ok=true
fi
elapsed=$((SECONDS - start))
if $ok && [ "$elapsed" -le 8 ]; then
    dt::pass "--timeout 2 kills a 15s sleep within ${elapsed}s and reports failure"
else
    dt::fail "--timeout 2: ok=$ok elapsed=${elapsed}s (expected failure within ~8s)"
fi

dt::summary
