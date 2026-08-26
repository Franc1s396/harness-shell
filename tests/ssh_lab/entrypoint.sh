#!/bin/sh
set -eu

: "${SSH_USER:?SSH_USER is required}"
: "${SSH_PASSWORD:?SSH_PASSWORD is required}"
: "${LAB_NODE:?LAB_NODE is required}"

if ! getent passwd "$SSH_USER" >/dev/null; then
  useradd --create-home --shell /bin/bash "$SSH_USER"
fi
printf '%s:%s\n' "$SSH_USER" "$SSH_PASSWORD" | chpasswd

install -d -m 0700 -o "$SSH_USER" -g "$SSH_USER" "/home/$SSH_USER/.ssh"
install -m 0600 -o "$SSH_USER" -g "$SSH_USER" /runtime/authorized_keys "/home/$SSH_USER/.ssh/authorized_keys"
install -m 0600 /runtime/host_ed25519_key /etc/ssh/ssh_host_ed25519_key
install -m 0644 /runtime/host_ed25519_key.pub /etc/ssh/ssh_host_ed25519_key.pub

printf 'harness-shell-%s-utf8-中文-🙂\n' "$LAB_NODE" > "/home/$SSH_USER/data.txt"
ln -sfn "/home/$SSH_USER/data.txt" "/home/$SSH_USER/data-link"
chown "$SSH_USER:$SSH_USER" "/home/$SSH_USER/data.txt" "/home/$SSH_USER/data-link"

exec /usr/sbin/sshd -D -e
