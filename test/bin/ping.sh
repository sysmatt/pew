#!/bin/bash
# Quick Ansible connectivity check against the whole lab.
set -euo pipefail
cd "$(dirname "$0")/.."   # -> test/
ansible testlab -i inventory.ini -m ping
