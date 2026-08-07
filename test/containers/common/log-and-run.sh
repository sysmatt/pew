#!/bin/bash
# sshd ForceCommand wrapper: logs every command an SSH session asks to run,
# then actually runs it. Covers plain `ssh host cmd` (pew's run) and
# rsync-over-ssh (pew's copy, which execs `rsync --server ...` as a normal
# command) — SFTP-based transfers (scp's default protocol, used by pew's
# diff) don't go through here at all; see log-sftp.sh for those.
LOGDIR="/var/log/test-harness"
mkdir -p "$LOGDIR"

{
    printf '%s [%s@%s] ' "$(date -Iseconds)" "$(whoami)" "$(hostname)"
    if [ -n "$SSH_ORIGINAL_COMMAND" ]; then
        printf '%s\n' "$SSH_ORIGINAL_COMMAND"
    else
        printf '(interactive shell)\n'
    fi
} >> "$LOGDIR/commands.log"

if [ -n "$SSH_ORIGINAL_COMMAND" ]; then
    exec /bin/bash -c "$SSH_ORIGINAL_COMMAND"
else
    exec /bin/bash -l
fi
