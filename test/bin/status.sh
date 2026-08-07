#!/bin/bash
# Quick status check: is the lab up, and how do I reach it.
set -euo pipefail
cd "$(dirname "$0")/.."   # -> test/

docker compose ps

echo
if [ -f inventory.ini ]; then
    echo "Inventory: test/inventory.ini"
else
    echo "No inventory.ini yet — run test/bin/up.sh first."
fi

if grep -q "BEGIN pew-test-harness" ~/.ssh/config 2>/dev/null; then
    echo "~/.ssh/config: pew-test-harness block is installed"
else
    echo "~/.ssh/config: pew-test-harness block NOT installed"
    echo "  (run test/bin/install-ssh-config.sh if you want bare 'ssh test-ubuntu' / pew to work directly)"
fi

echo
echo "Command logs (what actually ran inside each container):"
for name in ubuntu rocky; do
    log="logs/$name/commands.log"
    if [ -f "$log" ]; then
        echo "  $log ($(wc -l < "$log") lines)"
    else
        echo "  $log (none yet)"
    fi
done
