FROM ubuntu:24.04

RUN apt-get update && apt-get install -y --no-install-recommends \
        openssh-server python3 sudo rsync diffutils \
    && rm -rf /var/lib/apt/lists/*

RUN useradd -m -s /bin/bash testuser \
    && echo "testuser ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/testuser \
    && chmod 440 /etc/sudoers.d/testuser

COPY common/sshd-test-harness.conf /tmp/sshd-test-harness.conf
RUN cat /tmp/sshd-test-harness.conf >> /etc/ssh/sshd_config && rm /tmp/sshd-test-harness.conf

COPY common/log-and-run.sh /usr/local/bin/log-and-run.sh
COPY common/log-sftp.sh /usr/local/bin/log-sftp.sh
COPY common/entrypoint.sh /entrypoint.sh
RUN chmod +x /usr/local/bin/log-and-run.sh /usr/local/bin/log-sftp.sh /entrypoint.sh

EXPOSE 22
ENTRYPOINT ["/entrypoint.sh"]
