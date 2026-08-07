#!/bin/bash
# Dump each running container's own stdout/stderr (sshd's connection-level
# log, since sshd runs with -e) to test/logs/<name>-docker.log, without
# stopping anything. Safe to run any time the lab is up; also called
# automatically by down.sh before teardown.
set -euo pipefail
cd "$(dirname "$0")/.."   # -> test/

for name in ubuntu rocky; do
    container="pew-test-$name"
    if docker inspect "$container" >/dev/null 2>&1; then
        docker compose logs --no-color "$name" > "logs/$name-docker.log" 2>&1
        echo "Saved logs/$name-docker.log"
    fi
done
