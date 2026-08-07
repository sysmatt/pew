#!/bin/bash
# Tear down the pew test lab. Saves each container's own sshd/docker log
# before removing containers (the commands.log under test/logs/ is on a
# bind mount and survives regardless).
set -euo pipefail
cd "$(dirname "$0")/.."   # -> test/

"$(dirname "$0")/save-logs.sh" || true

docker compose down

echo
echo "Test lab is down. test/logs/ and test/inventory.ini are left in place."
echo "If you added the ~/.ssh/config block, test/bin/uninstall-ssh-config.sh"
echo "removes it (optional — harmless to leave while the lab is stopped,"
echo "connections will just fail until you run up.sh again)."
