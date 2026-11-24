#!/bin/bash
# First-boot script for the EC2 instance (placeholders filled in by deploy.sh). Contains no secrets.
set -euxo pipefail
NAME=@NAME@
REGION=@REGION@
BUCKET=@BUCKET@

# t4g.nano has 0.5 GB RAM: dnf gets OOM-killed without swap
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=1024
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile swap swap defaults 0 0' >>/etc/fstab
fi
dnf install -y docker
systemctl enable --now docker
docker pull caddy:2.8.4

mkdir -p /etc/caddy
cat >/etc/systemd/system/caddy.service <<'EOF'
[Unit]
Description=Caddy (Docker) - TLS reverse proxy for MyDemoTicket
After=docker.service network-online.target
Requires=docker.service
[Service]
ExecStartPre=-/usr/bin/docker rm -f caddy
ExecStart=/usr/bin/docker run --rm --name caddy --network host -v /etc/caddy:/etc/caddy:ro -v caddy-data:/data caddy:2.8.4
ExecReload=/usr/bin/docker exec caddy caddy reload --config /etc/caddy/Caddyfile
ExecStop=/usr/bin/docker stop caddy
Restart=always
RestartSec=5
[Install]
WantedBy=multi-user.target
EOF

cat >/etc/systemd/system/mydemoticket-boot.service <<EOF
[Unit]
Description=MyDemoTicket: fetch code + config, (re)install, set hostnames
After=docker.service network-online.target
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
Environment=NAME=$NAME REGION=$REGION BUCKET=$BUCKET
ExecStart=/bin/bash -c 'mkdir -p /opt/$NAME-src && aws s3 cp --region $REGION s3://$BUCKET/bundle.tgz /opt/$NAME-src/bundle.tgz && tar -xzf /opt/$NAME-src/bundle.tgz -C /opt/$NAME-src && exec /opt/$NAME-src/deploy/aws/boot.sh'
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable caddy.service mydemoticket-boot.service
systemctl start mydemoticket-boot.service
