#!/bin/bash
# Quick interactive SSH into a lab host (requires install-ssh-config.sh
# to have been run). Usage:
#   test/bin/shell.sh ubuntu         # as testuser
#   test/bin/shell.sh ubuntu root    # as root
set -euo pipefail
if [ $# -lt 1 ]; then
    echo "Usage: $0 <ubuntu|rocky> [root]" >&2
    exit 1
fi
host="test-$1"
if [ -n "${2:-}" ]; then
    exec ssh "$2@$host"
else
    exec ssh "$host"
fi
