#!/bin/bash
# Runs on the EC2 instance at every boot (mydemoticket-boot.service): reads config from SSM, derives the
# hostnames from the current public IP and (re)runs deploy/install.sh. Data in /var/lib/mydemoticket is kept.
# Env: NAME, REGION (set by the systemd unit).
set -euo pipefail
SRC=/opt/$NAME-src

param() { aws ssm get-parameter --region "$REGION" --with-decryption --name "/$NAME/$1" --query Parameter.Value --output text; }

TOKEN=$(curl -sS -X PUT -H "X-aws-ec2-metadata-token-ttl-seconds: 60" http://169.254.169.254/latest/api/token)
IP=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/public-ipv4)
HOSTS="$(echo "$IP" | tr . -).sslip.io"
DOMAIN=$(param DOMAIN)
ALB_PORT=$(param ALB_PORT 2>/dev/null || echo none)
ALB_ARGS=()
if [ -n "$ALB_PORT" ] && [ "$ALB_PORT" != "none" ]; then
  # $DOMAIN is served by the load balancer (TLS there), Caddy only adds a plain-HTTP listener for it
  ALB_ARGS=(--alb-port "$ALB_PORT")
elif [ -n "$DOMAIN" ] && [ "$DOMAIN" != "none" ]; then
  HOSTS="$DOMAIN,$HOSTS"
fi

# Admin credentials: only read by the app when the database is empty (first start).
umask 077
mkdir -p /etc/mydemoticket
{
  echo "# managed by mydemoticket"
  echo "MYDEMOTICKET_ADMIN_PASSWORD=$(param ADMIN_PASSWORD)"
  echo "MYDEMOTICKET_ADMIN_TOKEN=$(param ADMIN_TOKEN)"
} >/etc/mydemoticket/env
umask 022

"$SRC/deploy/install.sh" --domain "$HOSTS" --python /usr/bin/python3 "${ALB_ARGS[@]}"
echo "MyDemoTicket boot done: https://${HOSTS//,/ https://}"
