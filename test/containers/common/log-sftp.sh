#!/bin/bash
# sshd Subsystem wrapper for sftp. ForceCommand does NOT apply to subsystem
# requests in OpenSSH, so scp's default (SFTP) protocol would otherwise be
# completely invisible to commands.log. This can't log individual file
# operations (those travel inside the binary SFTP protocol, not as argv),
# but it at least records that an SFTP session happened, closing the
# "silently invisible" gap. pew's `diff` uses scp/SFTP for its fetch.
LOGDIR="/var/log/test-harness"
mkdir -p "$LOGDIR"
echo "$(date -Iseconds) [$(whoami)@$(hostname)] SFTP session started" >> "$LOGDIR/commands.log"

for candidate in /usr/lib/openssh/sftp-server /usr/libexec/openssh/sftp-server /usr/lib/ssh/sftp-server; do
    if [ -x "$candidate" ]; then
        exec "$candidate" "$@"
    fi
done

echo "log-sftp.sh: could not find the real sftp-server binary" >&2
exit 1
