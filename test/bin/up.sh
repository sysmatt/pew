#!/bin/bash
# Bring the pew test lab up: generate a throwaway SSH keypair if needed,
# build/start the containers, wait for sshd, and (re)generate the Ansible
# inventory that points at them.
set -euo pipefail
cd "$(dirname "$0")/.."   # -> test/

if [ ! -f ssh/id_test ]; then
    echo "Generating throwaway test-harness SSH keypair (test/ssh/id_test)..."
    mkdir -p ssh
    ssh-keygen -t ed25519 -f ssh/id_test -N "" -C "pew-test-harness" -q
fi

mkdir -p logs/ubuntu logs/rocky

echo "Building and starting test containers..."
docker compose up -d --build

echo "Waiting for sshd to accept connections..."
for port in 2201 2203; do
    for _ in $(seq 1 30); do
        if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
            exec 3>&- 3<&-
            break
        fi
        sleep 1
    done
done

KEY_PATH="$(pwd)/ssh/id_test"
cat > inventory.ini <<EOF
[ubuntu]
test-ubuntu ansible_host=127.0.0.1 ansible_port=2201

[rocky]
test-rocky ansible_host=127.0.0.1 ansible_port=2203

[testlab:children]
ubuntu
rocky

[testlab:vars]
ansible_user=testuser
ansible_ssh_private_key_file=$KEY_PATH
ansible_ssh_common_args='-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null'
EOF

echo
echo "Test lab is up."
echo "  Ansible inventory: test/inventory.ini"
echo "  Try: ansible testlab -i test/inventory.ini -m ping"
echo
echo "pew itself execs plain 'ssh <host> ...' directly (it doesn't read"
echo "inventory vars), so bare hostnames like 'test-ubuntu' need to resolve"
echo "via your OWN SSH config, not just the Ansible inventory above. Run:"
echo "  test/bin/install-ssh-config.sh"
echo "to add (or refresh) a clearly-marked block in ~/.ssh/config for this,"
echo "or pass connection details manually. See test/TESTING.md for details."
