#!/bin/bash
# Runs on the EC2 instance at every boot (mydemoticket-boot.service): reads config from SSM, derives the
# hostnames from the current public IP and (re)runs deploy/install.sh. Data in /var/lib/mydemoticket is kept.
# The env file is rewritten on every boot, before install.sh restarts the service (install.sh never
# overwrites an existing env file).
# Env: NAME, REGION (set by the systemd unit). For tests: SRC, ENV_FILE, IMDS_URL.
set -euo pipefail
SRC=${SRC:-/opt/$NAME-src}
ENV_FILE=${ENV_FILE:-/etc/mydemoticket/env}
IMDS_URL=${IMDS_URL:-http://169.254.169.254}

param() { aws ssm get-parameter --region "$REGION" --with-decryption --name "/$NAME/$1" --query Parameter.Value --output text; }
# optional parameter: prints its value, or $2 if it is missing / unreadable
opt_param() { local v; v=$(param "$1" 2>/dev/null) && [ -n "$v" ] && [ "$v" != "None" ] && printf '%s' "$v" || printf '%s' "$2"; }

TOKEN=$(curl -sS -X PUT -H "X-aws-ec2-metadata-token-ttl-seconds: 60" "$IMDS_URL/latest/api/token")
IP=$(curl -sS -H "X-aws-ec2-metadata-token: $TOKEN" "$IMDS_URL/latest/meta-data/public-ipv4")
HOSTS="$(echo "$IP" | tr . -).sslip.io"
DOMAIN=$(param DOMAIN)
ALB_PORT=$(opt_param ALB_PORT none)
ALB_ARGS=()
if [ -n "$ALB_PORT" ] && [ "$ALB_PORT" != "none" ]; then
  # $DOMAIN is served by the load balancer (TLS there), Caddy only adds a plain-HTTP listener for it
  ALB_ARGS=(--alb-port "$ALB_PORT")
elif [ -n "$DOMAIN" ] && [ "$DOMAIN" != "none" ]; then
  HOSTS="$DOMAIN,$HOSTS"
fi

# Splunk HEC: on only if URL and token parameters exist (missing parameters = HEC off, no failure).
HEC_URL=$(opt_param HEC_URL none)
HEC_TOKEN=""
HEC_INDEX=""
if [ "$HEC_URL" != "none" ]; then
  HEC_TOKEN=$(opt_param HEC_TOKEN "")
  HEC_INDEX=$(opt_param HEC_INDEX mydemoticket)
fi
# values end up in a systemd EnvironmentFile: one line each, no whitespace or quotes
case "$HEC_URL$HEC_TOKEN$HEC_INDEX" in *[[:space:]\"\'\\]*) echo "HEC parameters contain invalid characters, HEC off" >&2; HEC_URL=none ;; esac

# Root-only env file, rewritten on every boot. Admin credentials are only read by the app when the
# database is empty (first start); HEC settings are read on every service start.
umask 077
mkdir -p "$(dirname "$ENV_FILE")"
{
  echo "# managed by mydemoticket"
  echo "MYDEMOTICKET_ADMIN_PASSWORD=$(param ADMIN_PASSWORD)"
  echo "MYDEMOTICKET_ADMIN_TOKEN=$(param ADMIN_TOKEN)"
  if [ "$HEC_URL" != "none" ] && [ -n "$HEC_TOKEN" ]; then
    echo "MYDEMOTICKET_HEC_URL=$HEC_URL"
    echo "MYDEMOTICKET_HEC_TOKEN=$HEC_TOKEN"
    echo "MYDEMOTICKET_HEC_INDEX=$HEC_INDEX"
    echo "MYDEMOTICKET_HEC_VERIFY_TLS=1"
  fi
} >"$ENV_FILE.tmp"
chmod 600 "$ENV_FILE.tmp"
mv -f "$ENV_FILE.tmp" "$ENV_FILE"
umask 022
if [ "$HEC_URL" != "none" ] && [ -n "$HEC_TOKEN" ]; then
  echo "Splunk HEC on: $HEC_URL index $HEC_INDEX"
else
  echo "Splunk HEC off (no /$NAME/HEC_URL or /$NAME/HEC_TOKEN parameter)"
fi

"$SRC/deploy/install.sh" --domain "$HOSTS" --python /usr/bin/python3 ${ALB_ARGS[@]+"${ALB_ARGS[@]}"}
echo "MyDemoTicket boot done: https://${HOSTS//,/ https://}"
