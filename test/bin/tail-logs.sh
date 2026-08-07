#!/bin/bash
# Tail both containers' command logs together, live.
set -euo pipefail
cd "$(dirname "$0")/.."   # -> test/
exec tail -f logs/ubuntu/commands.log logs/rocky/commands.log
