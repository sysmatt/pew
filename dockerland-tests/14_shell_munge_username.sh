#!/usr/bin/env bash
# Tests: pew shell drops a +suffix from the username (MikroTik console
# options like admin+ct, unsuited to an interactive session) unless
# --no-munge-username, and only shell does - run keeps the full user.
#
# No containers needed: every check is --dry-run against a static
# inventory, which renders the ssh command locally without ever opening
# a connection.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

LOCAL_TMP="/tmp/pew-munge-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
trap 'rm -rf "$LOCAL_TMP"' EXIT
mkdir -p "$FAKE_HOME"

INV="$LOCAL_TMP/inventory.ini"
cat > "$INV" <<'EOF'
[routers]
rtr1.example.test ansible_user=admin+ct
[web]
web1.example.test ansible_user=deploy
EOF

PEW() { HOME="$FAKE_HOME" "$DT_PEW" "$@"; }

check() {
    local label="$1" pattern="$2"; shift 2
    local out
    out="$(PEW "$@" 2>&1)"
    if echo "$out" | grep -qE -- "$pattern"; then
        dt::pass "$label"
    else
        dt::fail "$label: got: $out"
    fi
}
check "shell strips +suffix by default"        '^ssh admin@rtr1\.example\.test$'             shell -i "$INV" -n rtr1
check "shell -- cmd strips it too"             '^ssh -t admin@rtr1\.example\.test htop$'      shell -i "$INV" -n rtr1 -- htop
check "--no-munge-username keeps it"           '^ssh admin\+ct@rtr1\.example\.test$'          shell -i "$INV" -n --no-munge-username rtr1
check "--remote-user is munged as well"        '^ssh ops@rtr1\.example\.test$'                shell -i "$INV" -n -U ops+t500w rtr1
check "a user without + is untouched"          '^ssh deploy@web1\.example\.test$'             shell -i "$INV" -n web1
check "run keeps the full user"                ': ssh admin\+ct@rtr1\.example\.test uptime$'  run -i "$INV" --hosts rtr1.example.test -n -- uptime
check "--debug reports the munge"              "username 'admin\+ct' munged to 'admin'"       shell -i "$INV" -n --debug rtr1

dt::summary
