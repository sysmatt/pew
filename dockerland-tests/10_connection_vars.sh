#!/usr/bin/env bash
# Tests: pew dials each host by its *rendered* Ansible connection variables
# (ansible_host/ansible_port/ansible_user/ansible_ssh_private_key_file/
# ansible_ssh_common_args), not its bare inventory name, plus the
# --extra-vars/-e passthrough and ~/.pewrc's extra_vars= that steer them.
#
# Mirrors the sysmatt-deploy connect_via_tailnet pattern: a group_vars
# ansible_host template picks between two paths based on a boolean var.
# Here the "direct" path is deliberately unresolvable (<name>.invalid) and
# the "alternate" path is the container's real IP, so a host only connects
# when -e (or extra_vars=) flips the toggle - proving the template was
# rendered with the extra vars applied, not read raw.
#
# Every pewrc-dependent call runs with HOME overridden to a scratch dir, as
# in 09_pewrc_and_env.sh - never touches the real user's ~/.pewrc.
set -u  # NOT pipefail: echo "$x" | grep -q early-exits on a match, SIGPIPE-killing echo,
        # which pipefail then reports as pipeline failure even though grep matched.
cd "$(dirname "${BASH_SOURCE[0]}")"
source ./lib.sh

CONTAINERS=()
LOCAL_TMP="/tmp/pew-connvars-test-$$"
FAKE_HOME="$LOCAL_TMP/fakehome"
TPL_INV="$LOCAL_TMP/inventory"
cleanup() {
    dt::cleanup "${CONTAINERS[@]}"
    rm -rf "$LOCAL_TMP"
}
trap cleanup EXIT
mkdir -p "$FAKE_HOME" "$TPL_INV/group_vars"

dt::log "bringing up one ubuntu ssh container..."
UBUNTU="$(dt::up ubuntu-latest-ssh.toml)" || exit 1
CONTAINERS+=("$UBUNTU")

INV="$(dt::generate_inventory)"
eval "$(dt::activate_cmd)"

# Same as dockerland's inventory, except each host's literal ansible_host=IP
# becomes real_ip=IP (a host-level ansible_host would outrank group_vars),
# plus a host whose ansible_host template can't render at all.
sed 's/ ansible_host=/ real_ip=/' "$INV" > "$TPL_INV/hosts.ini"
cat >> "$TPL_INV/hosts.ini" <<'EOF'

[broken]
broken_host ansible_host="{{ no_such_variable }}"
EOF
cat > "$TPL_INV/group_vars/all.yml" <<'EOF'
connect_via_alt: false
ansible_host: >-
  {{ real_ip if (connect_via_alt | bool) else inventory_hostname ~ '.invalid' }}
EOF

# No -U anywhere below unless testing it: the user comes from inventory's
# ansible_user=testuser now.
BASE=(-i "$TPL_INV" --hosts ubuntu_latest_ssh --yes)
ALT=(-e connect_via_alt=true)
IP="$(awk '/ real_ip=/ { sub(/.*real_ip=/, ""); print $1; exit }' "$TPL_INV/hosts.ini")"

# --- default path renders from the template (the unresolvable one) and fails ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${BASE[@]}" --dry-run -- true 2>&1)"
if echo "$out" | grep -qE "testuser@${UBUNTU}\.invalid true$"; then
    dt::pass "templated ansible_host renders its default branch (<name>.invalid)"
else
    dt::fail "default ansible_host render: got: $out"
fi
if HOME="$FAKE_HOME" "$DT_PEW" run "${BASE[@]}" --timeout 20 -- true >/dev/null 2>&1; then
    dt::fail "default path: expected connection to <name>.invalid to fail, but it succeeded"
else
    dt::pass "default path dials <name>.invalid (connection fails as expected)"
fi

# --- -e flips the template to the real IP and the command actually runs ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${BASE[@]}" "${ALT[@]}" --dry-run -- true 2>&1)"
if echo "$out" | grep -qF "testuser@$IP true"; then
    dt::pass "-e connect_via_alt=true renders ansible_host to the container IP ($IP)"
else
    dt::fail "-e ansible_host render: got: $out"
fi
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${BASE[@]}" "${ALT[@]}" -- whoami 2>&1)"
if echo "$out" | grep -qx "${UBUNTU}: testuser"; then
    dt::pass "-e path connects, as inventory's ansible_user (testuser) with no -U"
else
    dt::fail "-e path run as ansible_user: got: $out"
fi

# --- identity stays the inventory name: label and {} substitution ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${BASE[@]}" "${ALT[@]}" -- echo '{}' 2>&1)"
if echo "$out" | grep -qx "${UBUNTU}: ${UBUNTU}"; then
    dt::pass "label and {} stay the inventory name, not the dialed address"
else
    dt::fail "label/{} substitution: got: $out"
fi

# --- --root still overrides ansible_user ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${BASE[@]}" "${ALT[@]}" --root -- whoami 2>&1)"
if echo "$out" | grep -qx "${UBUNTU}: root"; then
    dt::pass "--root overrides inventory's ansible_user"
else
    dt::fail "--root over ansible_user: got: $out"
fi

# --- ansible_port is honored (a wrong port via -e refuses the connection) ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${BASE[@]}" "${ALT[@]}" -e ansible_port=1 --dry-run -- true 2>&1)"
if echo "$out" | grep -qF -- "-o Port=1 "; then
    dt::pass "ansible_port renders into the ssh command as -o Port="
else
    dt::fail "ansible_port in dry-run: got: $out"
fi
if HOME="$FAKE_HOME" "$DT_PEW" run "${BASE[@]}" "${ALT[@]}" -e ansible_port=1 --timeout 20 -- true >/dev/null 2>&1; then
    dt::fail "ansible_port=1: expected connection refused, but it succeeded"
else
    dt::pass "ansible_port=1 is actually dialed (connection fails)"
fi

# --- copy (rsync -e ssh ...) and diff (scp) both use the rendered path ---
# diff compares a local file against the *same* path remotely, so the
# payload lives at a /tmp path that exists on both sides.
PAYLOAD="/tmp/pew-connvars-payload-$$.txt"
echo "connvars-copy-payload" > "$PAYLOAD"
if HOME="$FAKE_HOME" "$DT_PEW" copy "${BASE[@]}" "${ALT[@]}" --same "$PAYLOAD" >/dev/null 2>&1 \
   && [ "$(dt::exec "$UBUNTU" cat "$PAYLOAD" 2>&1 | grep -v 'Output logged')" = "connvars-copy-payload" ]; then
    dt::pass "copy works over the rendered connection (rsync -e with key/options)"
else
    dt::fail "copy over rendered connection failed"
fi
out="$(HOME="$FAKE_HOME" "$DT_PEW" diff "${BASE[@]}" "${ALT[@]}" "$PAYLOAD" 2>&1)"
rm -f "$PAYLOAD"
if echo "$out" | grep -q "(no differences)"; then
    dt::pass "diff works over the rendered connection (scp with key/options)"
else
    dt::fail "diff over rendered connection: got: $out"
fi

# --- ~/.pewrc's extra_vars= applies, and a command-line -e overrides it ---
cat > "$FAKE_HOME/.pewrc" <<EOF
extra_vars=connect_via_alt=true
EOF
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${BASE[@]}" -- whoami 2>&1)"
if echo "$out" | grep -qx "${UBUNTU}: testuser"; then
    dt::pass "~/.pewrc's extra_vars= steers the connection path"
else
    dt::fail "~/.pewrc extra_vars=: got: $out"
fi
out="$(HOME="$FAKE_HOME" "$DT_PEW" run "${BASE[@]}" -e connect_via_alt=false --dry-run -- true 2>&1)"
if echo "$out" | grep -qF "testuser@${UBUNTU}.invalid"; then
    dt::pass "command-line -e overrides ~/.pewrc's extra_vars="
else
    dt::fail "-e over extra_vars= precedence: got: $out"
fi
rm -f "$FAKE_HOME/.pewrc"

# --- a host whose ansible_host can't render warns and falls back ---
out="$(HOME="$FAKE_HOME" "$DT_PEW" run -i "$TPL_INV" --hosts broken --yes --dry-run -- true 2>&1)"
if echo "$out" | grep -q "broken_host: could not render connection variables" \
   && echo "$out" | grep -qE "ssh broken_host true$"; then
    dt::pass "unrenderable ansible_host warns and falls back to the inventory name"
else
    dt::fail "unrenderable ansible_host fallback: got: $out"
fi

dt::summary
