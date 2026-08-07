#!/bin/bash
# Removes the pew-test-harness block from ~/.ssh/config that
# install-ssh-config.sh added. Only touches lines between the BEGIN/END
# markers — leaves everything else in the file untouched.
set -euo pipefail

SSH_CONFIG="$HOME/.ssh/config"
if [ ! -f "$SSH_CONFIG" ] || ! grep -q "^# BEGIN pew-test-harness" "$SSH_CONFIG"; then
    echo "No pew-test-harness block found in $SSH_CONFIG — nothing to do."
    exit 0
fi

TMP="$(mktemp)"
awk '
    $0 ~ /^# BEGIN pew-test-harness/ { skipping=1 }
    skipping && $0 ~ /^# END pew-test-harness/ { skipping=0; next }
    !skipping { print }
' "$SSH_CONFIG" > "$TMP"
mv "$TMP" "$SSH_CONFIG"
chmod 600 "$SSH_CONFIG"

echo "Removed pew-test-harness block from $SSH_CONFIG"
