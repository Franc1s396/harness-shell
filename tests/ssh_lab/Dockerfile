FROM debian:bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends openssh-server ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /run/sshd

COPY sshd_config /etc/ssh/sshd_config
COPY entrypoint.sh /usr/local/bin/harness-sshd
RUN chmod 0755 /usr/local/bin/harness-sshd

EXPOSE 22
HEALTHCHECK --interval=2s --timeout=2s --retries=30 CMD ssh-keyscan -T 1 -p 22 127.0.0.1 >/dev/null 2>&1 || exit 1
ENTRYPOINT ["/usr/local/bin/harness-sshd"]
