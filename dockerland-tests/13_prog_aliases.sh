#!/usr/bin/env bash
# Tests: invoked as `pes` or `per` (a symlink to ./pew, or a pipx entry
# point), pew acts as `pew shell` / `pew run` - `pes alpha` is
# `pew shell alpha`. Dispatch is on basename(argv[0]).
#
# No containers needed: every check is --dry-run against a static
# inventory, which resolves hosts and renders the ssh command locally
# without ever opening a connection.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

LOCAL_TMP="/tmp/pew-alias-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
BIN="$LOCAL_TMP/bin"
trap 'rm -rf "$LOCAL_TMP"' EXIT
mkdir -p "$FAKE_HOME" "$BIN"
ln -s "$DT_PEW" "$BIN/pes"
ln -s "$DT_PEW" "$BIN/per"

INV="$LOCAL_TMP/inventory.ini"
cat > "$INV" <<'EOF'
[web]
web1.example.test
web2.example.test
EOF

check() {
    local label="$1" pattern="$2" prog="$3"; shift 3
    local out
    out="$(HOME="$FAKE_HOME" "$BIN/$prog" "$@" 2>&1)"
    if echo "$out" | grep -qE -- "$pattern"; then
        dt::pass "$label"
    else
        dt::fail "$label: got: $out"
    fi
}
check "pes HOST -> pew shell HOST"      '^ssh web1\.example\.test$'           pes -i "$INV" -n web1
check "pes HOST -- cmd keeps the -- split" '^ssh -t web1\.example\.test htop$' pes -i "$INV" -n web1 -- htop
check "pes options after HOST"          '^ssh -t web1\.example\.test htop$'   pes web1 -i "$INV" -n -- htop
check "per -> pew run"                  ': ssh web1\.example\.test uptime$'   per -i "$INV" --hosts web1.example.test -n -- uptime
check "pes --help is shell's help"      'usage: pew shell'                    pes --help
check "pes with no HOST errors like shell" 'shell needs a HOST'               pes -i "$INV"

dt::summary
