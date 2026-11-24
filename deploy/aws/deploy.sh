#!/usr/bin/env bash
# Deploy MyDemoTicket to AWS: one small EC2 instance (arm64, Amazon Linux 2023). The app runs under systemd
# (deploy/install.sh), Caddy runs as a Docker container and gets Let's Encrypt certificates.
# Hostnames: <ip-with-dashes>.sslip.io (always, TLS by Caddy) plus $DOMAIN (default ticket.soc-games.com).
# With ALB_NAME set (default alb-aws), $DOMAIN is routed through that existing load balancer: host rule on its
# HTTPS listener (443, ACM certificate there) -> target group -> instance port $ALB_PORT (plain HTTP, only from the
# ALB security group). DNS: CNAME $DOMAIN -> ALB DNS name. ALB_NAME=none: Caddy serves $DOMAIN itself (A record).
#
#   deploy/aws/deploy.sh --dry-run      # read-only: checks and plan, creates nothing
#   deploy/aws/deploy.sh                # create (idempotent, resources named/tagged $NAME)
#   deploy/aws/deploy.sh --uninstall    # remove everything this script created (data on the instance is lost)
#
# Env: AWS_PROFILE, AWS_REGION (default eu-central-1), NAME (default mydemoticket), INSTANCE_TYPE (default t4g.nano),
#      DOMAIN (default ticket.soc-games.com, "none" for sslip.io only),
#      ALB_NAME (default alb-aws, "none" = no load balancer), ALB_PORT (default 8081), ALB_RULE_PRIORITY (default 40),
#      EXTRA_TAGS (optional "k=v,k=v", e.g. tags required by an SCP).
# Secrets: admin password and admin API token are created in the macOS keychain (service "mydemoticket") and copied
# to SSM Parameter Store (SecureString). They never appear in user data, on the command line or in output.
set -euo pipefail

MODE=${1:-apply}
case "$MODE" in apply | --dry-run | --uninstall) ;; *) echo "usage: $0 [--dry-run|--uninstall]" >&2; exit 2 ;; esac

export AWS_REGION=${AWS_REGION:-eu-central-1}
export AWS_PAGER=""
NAME=${NAME:-mydemoticket}
INSTANCE_TYPE=${INSTANCE_TYPE:-t4g.nano}
DOMAIN=${DOMAIN:-ticket.soc-games.com}
ALB_NAME=${ALB_NAME:-alb-aws}
ALB_PORT=${ALB_PORT:-8081}
ALB_RULE_PRIORITY=${ALB_RULE_PRIORITY:-40}
[ "$ALB_NAME" = "none" ] && ALB_PORT=none
TG_NAME="$NAME-$ALB_PORT"
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
EXTRA=""
IFS=',' read -ra _pairs <<<"${EXTRA_TAGS:-}"
for kv in "${_pairs[@]}"; do [ -n "$kv" ] && EXTRA="$EXTRA,{Key=${kv%%=*},Value=${kv#*=}}"; done
TAGS="ResourceType=%s,Tags=[{Key=Name,Value=$NAME},{Key=app,Value=mydemoticket}$EXTRA]"

say() { printf '%s\n' "$*"; }
keychain() { security find-generic-password -s mydemoticket -a "$1" -w; }

instance_id() {
  aws ec2 describe-instances --filters "Name=tag:Name,Values=$NAME" \
    "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text
}
sg_id() {
  aws ec2 describe-security-groups --filters "Name=group-name,Values=$NAME" --query 'SecurityGroups[0].GroupId' --output text
}
alb_arn() { aws elbv2 describe-load-balancers --names "$ALB_NAME" --query 'LoadBalancers[0].LoadBalancerArn' --output text; }
https_listener() { aws elbv2 describe-listeners --load-balancer-arn "$1" --query 'Listeners[?Port==`443`].ListenerArn | [0]' --output text; }
tg_arn() { aws elbv2 describe-target-groups --names "$TG_NAME" --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || echo None; }
host_rule() { # listener -> ARN of the rule forwarding host $DOMAIN, or None
  aws elbv2 describe-rules --listener-arn "$1" \
    --query "Rules[?Conditions[?Field=='host-header' && contains(Values, '$DOMAIN')]].RuleArn | [0]" --output text
}

say "Account: $(aws sts get-caller-identity --query Arn --output text)  Region: $AWS_REGION  Name: $NAME"
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
BUCKET="$NAME-deploy-$ACCOUNT-$AWS_REGION"

# ---------------------------------------------------------------- uninstall
if [ "$MODE" = "--uninstall" ]; then
  if [ "$ALB_NAME" != "none" ]; then
    lst=$(https_listener "$(alb_arn)")
    rule=$(host_rule "$lst")
    [ "$rule" != "None" ] && say "delete ALB rule for $DOMAIN" && aws elbv2 delete-rule --rule-arn "$rule"
    tg=$(tg_arn)
    [ "$tg" != "None" ] && say "delete target group $TG_NAME" && aws elbv2 delete-target-group --target-group-arn "$tg"
  fi
  iid=$(instance_id)
  if [ -n "$iid" ]; then
    say "terminate instance $iid"
    aws ec2 terminate-instances --instance-ids $iid >/dev/null
    aws ec2 wait instance-terminated --instance-ids $iid
  fi
  sg=$(sg_id)
  [ "$sg" != "None" ] && say "delete security group $sg" && aws ec2 delete-security-group --group-id "$sg"
  if aws iam get-instance-profile --instance-profile-name "$NAME" >/dev/null 2>&1; then
    say "delete IAM instance profile and role $NAME"
    aws iam remove-role-from-instance-profile --instance-profile-name "$NAME" --role-name "$NAME" 2>/dev/null || true
    aws iam delete-instance-profile --instance-profile-name "$NAME"
  fi
  if aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
    aws iam detach-role-policy --role-name "$NAME" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
    aws iam delete-role-policy --role-name "$NAME" --policy-name read-config
    aws iam delete-role --role-name "$NAME"
  fi
  for p in ADMIN_PASSWORD ADMIN_TOKEN DOMAIN ALB_PORT; do
    aws ssm delete-parameter --name "/$NAME/$p" 2>/dev/null && say "delete parameter /$NAME/$p" || true
  done
  if aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
    say "delete bucket $BUCKET"
    aws s3 rm "s3://$BUCKET" --recursive >/dev/null
    aws s3api delete-bucket --bucket "$BUCKET"
  fi
  say "Uninstall done. Keychain items (service mydemoticket) are kept."
  exit 0
fi

# ---------------------------------------------------------------- checks (read-only)
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
[ "$VPC" = "None" ] && { say "no default VPC in $AWS_REGION"; exit 1; }
AMI=$(aws ssm get-parameter --name /aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64 \
  --query Parameter.Value --output text)
say "Default VPC: $VPC  AMI: $AMI  Type: $INSTANCE_TYPE  Domain: $DOMAIN"
IID=$(instance_id)

# ---------------------------------------------------------------- dry run: plan only
if [ "$MODE" = "--dry-run" ]; then
  keychain admin-password >/dev/null 2>&1 && say "keychain mydemoticket/admin-password exists" \
    || say "would create keychain item mydemoticket/admin-password"
  aws iam get-role --role-name "$NAME" >/dev/null 2>&1 && say "IAM role $NAME exists" || say "would create IAM role + instance profile $NAME"
  [ "$(sg_id)" != "None" ] && say "security group exists" || say "would create security group $NAME (80, 443; no SSH)"
  aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1 && say "bucket $BUCKET exists" || say "would create private bucket $BUCKET"
  say "would write SSM parameters /$NAME/ADMIN_PASSWORD, /$NAME/ADMIN_TOKEN (SecureString), /$NAME/DOMAIN"
  [ -n "$IID" ] && say "instance $IID exists" || say "would launch $INSTANCE_TYPE"
  if [ "$ALB_NAME" != "none" ]; then
    lst=$(https_listener "$(alb_arn)")
    say "ALB $ALB_NAME HTTPS listener: $lst"
    [ "$(tg_arn)" != "None" ] && say "target group $TG_NAME exists" || say "would create target group $TG_NAME (HTTP $ALB_PORT, health /api/v1/health)"
    [ "$(host_rule "$lst")" != "None" ] && say "ALB rule for $DOMAIN exists" \
      || say "would add ALB rule host=$DOMAIN priority $ALB_RULE_PRIORITY -> $TG_NAME (other rules untouched)"
  fi
  say "Dry run done, nothing created."
  exit 0
fi

# ---------------------------------------------------------------- secrets in keychain
for item in admin-password admin-token; do
  if ! keychain "$item" >/dev/null 2>&1; then
    say "create keychain item mydemoticket/$item (random)"
    security add-generic-password -s mydemoticket -a "$item" \
      -w "$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  fi
done

# ---------------------------------------------------------------- IAM role: SSM access, read /$NAME/*, read bundle
if ! aws iam get-role --role-name "$NAME" >/dev/null 2>&1; then
  say "create IAM role + instance profile $NAME"
  aws iam create-role --role-name "$NAME" --assume-role-policy-document \
    '{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ec2.amazonaws.com"},"Action":"sts:AssumeRole"}]}' >/dev/null
  aws iam attach-role-policy --role-name "$NAME" --policy-arn arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore
  aws iam create-instance-profile --instance-profile-name "$NAME" >/dev/null
  aws iam add-role-to-instance-profile --instance-profile-name "$NAME" --role-name "$NAME"
  NEW_ROLE=1
else
  say "IAM role $NAME exists"
fi
aws iam put-role-policy --role-name "$NAME" --policy-name read-config --policy-document \
  "{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":\"ssm:GetParameter\",\"Resource\":\"arn:aws:ssm:$AWS_REGION:$ACCOUNT:parameter/$NAME/*\"},{\"Effect\":\"Allow\",\"Action\":\"s3:GetObject\",\"Resource\":\"arn:aws:s3:::$BUCKET/*\"}]}"
[ "${NEW_ROLE:-0}" = 1 ] && sleep 10 # IAM propagation before run-instances

# ---------------------------------------------------------------- security group: 80/443 only, no SSH
SG=$(sg_id)
if [ "$SG" = "None" ]; then
  say "create security group $NAME (80, 443 from anywhere; no SSH)"
  SG=$(aws ec2 create-security-group --group-name "$NAME" --description "mydemoticket: HTTP/HTTPS only" \
    --vpc-id "$VPC" --tag-specifications "$(printf "$TAGS" security-group)" --query GroupId --output text)
  for port in 80 443; do
    aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port $port --cidr 0.0.0.0/0 >/dev/null
  done
else
  say "security group $SG exists"
fi

# ---------------------------------------------------------------- parameters (secrets as SecureString)
put_param() { # name type value  (value via temp JSON file, never on the command line)
  local f; f=$(mktemp); chmod 600 "$f"
  python3 -c 'import json,sys; print(json.dumps({"Name":sys.argv[1],"Type":sys.argv[2],"Value":sys.stdin.read().rstrip("\n"),"Overwrite":True}))' \
    "/$NAME/$1" "$2" <<<"$3" >"$f"
  aws ssm put-parameter --cli-input-json "file://$f" >/dev/null
  rm -f "$f"
}
say "write SSM parameters /$NAME/*"
put_param ADMIN_PASSWORD SecureString "$(keychain admin-password)"
put_param ADMIN_TOKEN SecureString "$(keychain admin-token)"
put_param DOMAIN String "$DOMAIN"
put_param ALB_PORT String "$ALB_PORT"

# ---------------------------------------------------------------- code bundle in a private bucket
if ! aws s3api head-bucket --bucket "$BUCKET" >/dev/null 2>&1; then
  say "create private bucket $BUCKET"
  aws s3api create-bucket --bucket "$BUCKET" --create-bucket-configuration LocationConstraint="$AWS_REGION" >/dev/null
  aws s3api put-public-access-block --bucket "$BUCKET" --public-access-block-configuration \
    BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi
BUNDLE=$(mktemp)
COPYFILE_DISABLE=1 tar --exclude=__pycache__ -czf "$BUNDLE" -C "$ROOT" ticket-system/mydemoticket deploy
aws s3 cp "$BUNDLE" "s3://$BUCKET/bundle.tgz" >/dev/null
say "uploaded code bundle ($(wc -c <"$BUNDLE" | tr -d ' ') bytes) at $(git -C "$ROOT" rev-parse --short HEAD)"
rm -f "$BUNDLE"

# ---------------------------------------------------------------- instance
USERDATA=$(mktemp)
sed -e "s|@NAME@|$NAME|g" -e "s|@REGION@|$AWS_REGION|g" -e "s|@BUCKET@|$BUCKET|g" "$ROOT/deploy/aws/user-data.sh" \
  | gzip -9 >"$USERDATA"
if [ -n "$IID" ]; then
  say "instance $IID exists. New code is picked up on the next reboot:"
  say "  aws ec2 reboot-instances --instance-ids $IID"
else
  say "launch $INSTANCE_TYPE"
  IID=$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" --security-group-ids "$SG" \
    --iam-instance-profile Name="$NAME" --user-data "fileb://$USERDATA" \
    --metadata-options HttpTokens=required,HttpEndpoint=enabled \
    --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=8,VolumeType=gp3,Encrypted=true}' \
    --tag-specifications "$(printf "$TAGS" instance)" "$(printf "$TAGS" volume)" \
    --query 'Instances[0].InstanceId' --output text)
  aws ec2 wait instance-running --instance-ids "$IID"
  say "instance $IID running"
fi
rm -f "$USERDATA"
IP=$(aws ec2 describe-instances --instance-ids "$IID" --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)
[ "$IP" = "None" ] && { say "instance has no public IP (subnet without auto-assign?)"; exit 1; }
HOST=${IP//./-}.sslip.io
say "public IP: $IP"

# ---------------------------------------------------------------- load balancer routing for $DOMAIN
if [ "$ALB_NAME" != "none" ] && [ "$DOMAIN" != "none" ]; then
  ALB=$(alb_arn)
  ALB_SG=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB" --query 'LoadBalancers[0].SecurityGroups[0]' --output text)
  ALB_DNS=$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB" --query 'LoadBalancers[0].DNSName' --output text)
  LST=$(https_listener "$ALB")
  if ! aws ec2 describe-security-groups --group-ids "$SG" --query "SecurityGroups[0].IpPermissions[?FromPort==\`$ALB_PORT\`]" --output text | grep -q .; then
    say "allow port $ALB_PORT from ALB security group $ALB_SG"
    aws ec2 authorize-security-group-ingress --group-id "$SG" --protocol tcp --port "$ALB_PORT" --source-group "$ALB_SG" >/dev/null
  fi
  TG=$(tg_arn)
  if [ "$TG" = "None" ]; then
    say "create target group $TG_NAME"
    TG=$(aws elbv2 create-target-group --name "$TG_NAME" --protocol HTTP --port "$ALB_PORT" --vpc-id "$VPC" \
      --target-type instance --health-check-path /api/v1/health --matcher HttpCode=200 \
      --tags "Key=Name,Value=$NAME" "Key=app,Value=mydemoticket" --query 'TargetGroups[0].TargetGroupArn' --output text)
  fi
  aws elbv2 register-targets --target-group-arn "$TG" --targets "Id=$IID,Port=$ALB_PORT"
  if [ "$(host_rule "$LST")" = "None" ]; then
    say "add ALB rule host=$DOMAIN priority $ALB_RULE_PRIORITY -> $TG_NAME"
    aws elbv2 create-rule --listener-arn "$LST" --priority "$ALB_RULE_PRIORITY" \
      --conditions "Field=host-header,Values=$DOMAIN" --actions "Type=forward,TargetGroupArn=$TG" >/dev/null
  fi
  say "DNS: CNAME '${DOMAIN%%.*}' -> $ALB_DNS at one.com for https://$DOMAIN"
elif [ "$DOMAIN" != "none" ]; then
  say "DNS: set A record '${DOMAIN%%.*}' -> $IP at one.com for https://$DOMAIN"
fi

say "Waiting for https://$HOST/api/v1/health (first boot + certificate take a few minutes) ..."
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 "https://$HOST/api/v1/health" 2>/dev/null; then
    say ""; say "OK: https://$HOST  (login: admin, password in keychain: security find-generic-password -s mydemoticket -a admin-password -w)"
    exit 0
  fi
  sleep 10
done
say "Not healthy after 10 minutes. Logs: aws ssm start-session --target $IID, then: sudo journalctl -u mydemoticket-boot -u mydemoticket -u caddy"
exit 1
