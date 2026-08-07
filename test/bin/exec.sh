#!/bin/bash
# Run a command directly inside a lab container via `docker exec` — for
# debugging the container itself (permissions, installed packages, etc.),
# bypassing SSH/logging entirely. Not for testing pew — use test/bin/pew
# or plain ssh/scp/rsync against the hosts for that.
# Usage: test/bin/exec.sh ubuntu whoami
#        test/bin/exec.sh rocky          # interactive root shell
set -euo pipefail
if [ $# -lt 1 ]; then
    echo "Usage: $0 <ubuntu|rocky> [cmd...]" >&2
    exit 1
fi
container="pew-test-$1"; shift
if [ $# -eq 0 ]; then
    exec docker exec -it "$container" bash
else
    exec docker exec "$container" "$@"
fi
