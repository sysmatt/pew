#!/usr/bin/env bash
# Shared helpers for dockerland-tests/*.sh. Source, don't execute:
#   source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/lib.sh"
#
# Every dockerland call below is pinned to --project "$DT_PROJECT" so this
# harness only ever touches its own containers, never any other project's.

set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.

DT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DT_TEMPLATES="$DT_DIR/templates"
DT_REPO_ROOT="$(git -C "$DT_DIR" rev-parse --show-toplevel)"
DT_PEW="$DT_REPO_ROOT/pew"
DT_PROJECT="$(basename "$DT_REPO_ROOT")"
DT_INVENTORY="$DT_DIR/inventory.ini"

DT_PASS=0
DT_FAIL=0

dt::log()  { echo "[dockerland-tests] $*" >&2; }
dt::pass() { DT_PASS=$((DT_PASS + 1)); echo "PASS: $*"; }
dt::fail() { DT_FAIL=$((DT_FAIL + 1)); echo "FAIL: $*"; }

# assert that a command's output/exit satisfies a check - convenience wrapper
# most feature scripts drive their own dt::pass/dt::fail directly instead.
dt::summary() {
    echo
    echo "== $(basename "$0"): $DT_PASS passed, $DT_FAIL failed =="
    [ "$DT_FAIL" -eq 0 ]
}

# Bring up one container from a template (bare filename, e.g.
# "ubuntu-latest-ssh.toml"), print its container name on stdout.
#
# `up` now polls for ssh readiness itself (ssh=true templates) and fails
# loudly on timeout before returning, so no manual wait-then-teardown-on-
# failure dance is needed here - a failed `up` never leaves a container
# behind to leak. --format json replaces scraping the "Up: <name>" line
# out of human-readable text.
dt::up() {
    local template="$1"
    local json
    if ! json="$(dockerland up "$DT_TEMPLATES/$template" --project "$DT_PROJECT" --format json 2>&1)"; then
        echo "$json" >&2
        return 1
    fi
    local name
    name="$(jq -r '.name' <<< "$json")"
    if [ -z "$name" ] || [ "$name" = "null" ]; then
        echo "dt::up: couldn't parse container name from dockerland JSON output: $json" >&2
        return 1
    fi
    echo "$name"
}

dt::down() {
    local name="$1"
    dockerland down "$name" --project "$DT_PROJECT" >&2 || true
}

# Tear down every dockerland container belonging to this project.
dt::teardown_all() {
    dockerland down --all --project "$DT_PROJECT" >&2 || true
}

# Regenerate the project's ssh inventory, return its path on stdout.
dt::generate_inventory() {
    dockerland list --project "$DT_PROJECT" --format ansible-inventory > "$DT_INVENTORY"
    echo "$DT_INVENTORY"
}

# eval this in the CALLING shell to get ssh/scp on PATH pointed at dockerland's
# isolated ssh_config, e.g.: eval "$(dt::activate_cmd)"
dt::activate_cmd() {
    # dockerland activate now decorates $PS1 (export DOCKERLAND_OLD_PS1="$PS1"),
    # which is normally unset in a non-interactive script - give it a default
    # first so that doesn't trip `set -u`.
    echo 'export PS1="${PS1:-}"'
    dockerland activate
}

# Run a command inside a container as root (dockerland exec has no -u flag,
# but none of the starter Dockerfiles set USER, so this is the default).
dt::exec() {
    local name="$1"; shift
    dockerland exec "$name" --project "$DT_PROJECT" -- "$@"
}

# Host-side path to a container's mailslot dir (see workspace.py:
# MAILSLOT_SUBDIR = .dockerland/mailslot/<container-name>/, always rooted
# at the repo toplevel regardless of where this script lives).
dt::mailslot_dir() {
    local name="$1"
    echo "$DT_REPO_ROOT/.dockerland/mailslot/$name"
}

# Write a script into a container's mailslot and run it via dockerland exec.
# Usage: dt::run_mailslot_script <name> <local-script-path> [args...]
dt::run_mailslot_script() {
    local name="$1" script="$2"; shift 2
    local slot
    slot="$(dt::mailslot_dir "$name")"
    cp "$script" "$slot/run.sh"
    chmod +x "$slot/run.sh"
    dt::exec "$name" bash /mailslot/run.sh "$@"
}

# Trap-friendly cleanup: pass container names to tear down on EXIT.
# Usage: trap "dt::cleanup name1 name2 ..." EXIT
dt::cleanup() {
    for name in "$@"; do
        dt::down "$name"
    done
}
