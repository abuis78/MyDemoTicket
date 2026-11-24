# Deploying MyDemoTicket to AWS EC2 (ticket.soc-games.com)

Target: one small EC2 instance, Caddy terminates TLS (automatic Let's Encrypt)
and proxies to the app on `127.0.0.1:8080`, run by systemd as user
`mydemoticket`. Data lives in `/var/lib/mydemoticket`.

> Creating AWS resources costs money (t4g.nano + 8 GB gp3 + public IPv4 ≈ 8 USD/month,
> estimate). Get an explicit go before step 1.

## Quick path: one command (deploy/aws/deploy.sh)

```bash
aws sso login --profile janus-env2
export AWS_PROFILE=janus-env2
EXTRA_TAGS="splunkit_environment_type=non-prd,splunkit_data_classification=private" deploy/aws/deploy.sh --dry-run
EXTRA_TAGS="splunkit_environment_type=non-prd,splunkit_data_classification=private" deploy/aws/deploy.sh
```

Creates (all named `mydemoticket`): IAM role + instance profile (SSM access, read `/mydemoticket/*` and the
bundle), security group (80/443, no SSH), private S3 bucket with the code bundle, SSM parameters
(admin password + token as SecureString, from the macOS keychain service `mydemoticket`), one `t4g.nano`.
On every boot `deploy/aws/boot.sh` fetches the bundle, derives `<ip>.sslip.io` from the current public IP and
runs `install.sh --domain ticket.soc-games.com,<ip>.sslip.io`. Caddy runs as Docker container `caddy:2.8.4`.
Login: user `admin`, password via `security find-generic-password -s mydemoticket -a admin-password -w`.
`ticket.soc-games.com` goes through the existing load balancer `alb-aws` (default `ALB_NAME`): TLS with the ACM
wildcard certificate on its 443 listener, host rule (priority 40) → target group `mydemoticket-8081` → instance
port 8081 (plain HTTP, only from the ALB security group). DNS at one.com:
`CNAME ticket → alb-aws-962976536.eu-central-1.elb.amazonaws.com` (same as soar/splunk/demo).
Test before DNS exists:
`curl --connect-to ticket.soc-games.com:443:alb-aws-962976536.eu-central-1.elb.amazonaws.com:443 https://ticket.soc-games.com/api/v1/health`.
New code: run `deploy.sh` again (uploads the bundle), then reboot the instance (order: see
[Backup before an update](#backup-before-an-update)).
`deploy/aws/deploy.sh --uninstall` removes everything (data on the instance is lost), including the HEC parameters.

## Splunk HEC

The audit log is always written locally to `/var/lib/mydemoticket/logs/audit.log` (0600, inside the data dir,
which is the only writable path of the systemd unit). In addition it is shipped to Splunk HEC when configured
(details: [`docs/logging.md`](../docs/logging.md)).

1. Store the HEC token once in the macOS keychain (prompts for the value, nothing in the shell history):
   ```bash
   security add-generic-password -s mydemoticket -a splunk-hec-token -w
   ```
2. Run `deploy/aws/deploy.sh`. Env: `HEC_URL` (default `https://splunk.soc-games.com:8088`, `none` = HEC off),
   `HEC_INDEX` (default `mydemoticket`). It writes these SSM parameters:

   | Parameter | Type | Content |
   |---|---|---|
   | `/mydemoticket/HEC_URL` | String | HEC base URL, or `none` |
   | `/mydemoticket/HEC_INDEX` | String | target index, `mydemoticket` |
   | `/mydemoticket/HEC_TOKEN` | SecureString | token from the keychain (via temp JSON file, never on the command line or in output) |

   If the keychain item is missing, `deploy.sh` prints a warning, sets `HEC_URL=none`, deletes `HEC_TOKEN` and
   continues. `--dry-run` lists the planned parameters without the token value.
3. Reboot the instance. `boot.sh` reads the parameters on **every** boot and rewrites the root-only env file
   `/etc/mydemoticket/env` (0600) with `MYDEMOTICKET_HEC_URL`, `MYDEMOTICKET_HEC_TOKEN`, `MYDEMOTICKET_HEC_INDEX`
   and `MYDEMOTICKET_HEC_VERIFY_TLS=1`. Missing parameters mean HEC off, the boot does not fail. `install.sh`
   never overwrites an existing env file; the service reads it via `EnvironmentFile`.
4. Check in Splunk: `index=mydemoticket sourcetype=mydemoticket:audit | stats count by tenant`.

All tenants share the one index `mydemoticket`; they are separated by the indexed field `tenant` and one Splunk
role per tenant with `srchFilter = tenant::<slug>` (see [`docs/logging.md`](../docs/logging.md#tenant-separation-in-splunk)).

## Backup before an update

`deploy/aws/backup.sh` copies the data directory on the instance via SSM Run Command (no SSH, no `sqlite3`):
`systemctl stop mydemoticket; cp -a /var/lib/mydemoticket /var/lib/mydemoticket.bak-<UTC timestamp>;
systemctl start mydemoticket` (the service is started again even if the copy fails). It finds the running
instance by tag `Name=$NAME` and waits for the command status. `--dry-run` only prints the command, no AWS call.

Rollout order:

```bash
export AWS_PROFILE=janus-env2
deploy/aws/backup.sh                       # 1. backup on the instance, wait for "OK: backup ..."
EXTRA_TAGS="splunkit_environment_type=non-prd,splunkit_data_classification=private" deploy/aws/deploy.sh   # 2. bundle + parameters
aws ec2 reboot-instances --instance-ids <instance id printed by deploy.sh>                               # 3. reboot: boot.sh installs
curl -fsS https://ticket.soc-games.com/api/v1/health                                                     # 4. smoke test
```

Restore on the instance (`aws ssm start-session --target <id>`):
`sudo systemctl stop mydemoticket && sudo mv /var/lib/mydemoticket /var/lib/mydemoticket.broken &&
sudo cp -a /var/lib/mydemoticket.bak-<ts> /var/lib/mydemoticket && sudo systemctl start mydemoticket`.
Backups stay on the instance volume (8 GB); delete old `.bak-*` directories by hand.

## Files

| File | Purpose |
|---|---|
| `install.sh` | Copies the code, renders the systemd unit and Caddy site, enables the service. `--dry-run`, `--uninstall`, `--purge-data` |
| `mydemoticket.service.template` | Hardened systemd unit (`ProtectSystem=strict`, only the data dir writable) |
| `Caddyfile.template` | Caddy site for `@DOMAIN@` → `127.0.0.1:@PORT@` |
| `aws/deploy.sh` | Creates / updates / removes the AWS resources and SSM parameters (`--dry-run`, `--uninstall`) |
| `aws/boot.sh` | Runs on every boot of the instance: SSM parameters → `/etc/mydemoticket/env`, then `install.sh` |
| `aws/backup.sh` | Copies the data dir on the instance via SSM before an update (`--dry-run`) |
| `tests/test_deploy_scripts.sh` | Offline test of the scripts with stubbed `aws`/`curl`/`security` (no AWS, no network) |

Try it locally without changing anything:

```bash
deploy/install.sh --dry-run            # renders into a new temp dir and prints the path
deploy/install.sh --dry-run --uninstall --root "$TMPDIR/mdt-dry"
```

## 1. Instance (soc-games account, eu-central-1)

1. Log in: `aws sso login --profile janus-env2`, then
   `export AWS_PROFILE=janus-env2 AWS_REGION=eu-central-1`.
2. Launch Amazon Linux 2023 **arm64** (`/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64`),
   type `t4g.nano` (or `t4g.micro`), default VPC, auto-assigned public IP.
   - **Required tags (SCP)** on instance *and* volume:
     `splunkit_environment_type=non-prd`, `splunkit_data_classification=private`.
   - IMDSv2 required, root volume gp3 8 GB encrypted.
   - Security group: inbound 80 and 443 only, no SSH. Use SSM Session Manager
     (instance role with `AmazonSSMManagedInstanceCore`).
3. On `t4g.nano` add 1 GB swap before installing packages (otherwise `dnf` can be OOM-killed):
   ```bash
   sudo dd if=/dev/zero of=/swapfile bs=1M count=1024 && sudo chmod 600 /swapfile
   sudo mkswap /swapfile && sudo swapon /swapfile
   echo '/swapfile swap swap defaults 0 0' | sudo tee -a /etc/fstab
   ```

Amazon Linux 2023 ships Python 3.9, which is enough – no other packages are needed for the app.

## 2. DNS

`soc-games.com` is hosted at one.com, not Route 53. Create an `A` record
`ticket` → public IP of the instance manually in the one.com panel. The account is
at its Elastic IP limit, so the auto-assigned IP changes on stop/start: update
the record afterwards (and the SOAR asset base URL).

Without a DNS change you can use `<ip-with-dashes>.sslip.io` as domain, e.g.
`--domain 3-75-130-60.sslip.io` – Caddy gets a valid certificate for it too.

## 3. Caddy

Caddy is not in the AL2023 repositories. Install the static binary from the
official release page (https://caddyserver.com/docs/install#static-binaries), as
`/usr/bin/caddy`, with the systemd unit from
https://github.com/caddyserver/dist/blob/master/init/caddy.service and a `caddy`
system user. Ports 80/443 must be reachable for the ACME challenge.

## 4. Install the app

```bash
sudo dnf install -y git
git clone <repo-url> /tmp/MyDemoTicket && cd /tmp/MyDemoTicket
sudo deploy/install.sh --domain ticket.soc-games.com
curl -s https://ticket.soc-games.com/api/v1/health
sudo cat /var/lib/mydemoticket/initial-credentials.txt   # once; store in a password manager, then delete
```

Optional fixed admin credentials: put `MYDEMOTICKET_ADMIN_PASSWORD=` /
`MYDEMOTICKET_ADMIN_TOKEN=` into `/etc/mydemoticket/env` (root only, 0600)
**before the first start**. They are only read when the database is empty.

Update: `git pull` and run `sudo deploy/install.sh` again (data is kept).

## 5. Connect Splunk SOAR

1. In the web UI (admin → Users) create a token for user `soar` (or use the one
   from `initial-credentials.txt`) and store it in the SOAR asset.
2. Admin → Webhooks: add type `soar` with the SOAR base URL and a SOAR
   `ph-auth-token` (automation user) as secret.

## Uninstall

```bash
sudo deploy/install.sh --uninstall               # keeps /var/lib/mydemoticket
sudo deploy/install.sh --uninstall --purge-data  # also deletes data and the system user
```

Backup: `sudo systemctl stop mydemoticket && sudo tar czf mdt-backup.tgz -C /var/lib mydemoticket && sudo systemctl start mydemoticket`.
