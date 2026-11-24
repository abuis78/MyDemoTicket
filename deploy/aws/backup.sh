#!/usr/bin/env bash
# Back up the MyDemoTicket data directory on the EC2 instance before an update, via SSM Run Command
# (no SSH, no sqlite3 needed): stop the service, copy /var/lib/mydemoticket to
# /var/lib/mydemoticket.bak-<UTC timestamp>, start the service again (also if the copy failed).
#
#   deploy/aws/backup.sh --dry-run   # print what would run, no AWS calls
#   deploy/aws/backup.sh             # run on the instance tagged Name=$NAME and wait for the result
#
# Env: AWS_PROFILE, AWS_REGION (default eu-central-1), NAME (default mydemoticket).
# Restore (on the instance): systemctl stop mydemoticket; mv /var/lib/mydemoticket /var/lib/mydemoticket.broken;
#   cp -a /var/lib/mydemoticket.bak-<ts> /var/lib/mydemoticket; systemctl start mydemoticket
set -euo pipefail

MODE=${1:-apply}
case "$MODE" in apply | --dry-run) ;; *) echo "usage: $0 [--dry-run]" >&2; exit 2 ;; esac

export AWS_REGION=${AWS_REGION:-eu-central-1}
export AWS_PAGER=""
NAME=${NAME:-mydemoticket}
TS=$(date -u +%Y%m%dT%H%M%SZ)
DATA=/var/lib/mydemoticket
BACKUP="$DATA.bak-$TS"
SCRIPT="set -u; systemctl stop mydemoticket; cp -a $DATA $BACKUP; rc=\$?; systemctl start mydemoticket; \
[ \$rc -eq 0 ] && du -sh $BACKUP && echo 'backup ok: $BACKUP'; exit \$rc"

say() { printf '%s\n' "$*"; }

if [ "$MODE" = "--dry-run" ]; then
  say "would find the running instance tagged Name=$NAME in $AWS_REGION"
  say "would run via SSM (AWS-RunShellScript) and wait for the result:"
  say "  $SCRIPT"
  say "Dry run done, nothing executed."
  exit 0
fi

IID=$(aws ec2 describe-instances --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=running" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
case "$IID" in
  "" | None) say "no running instance tagged Name=$NAME in $AWS_REGION"; exit 1 ;;
  *[[:space:]]*) say "more than one running instance tagged Name=$NAME: $IID"; exit 1 ;;
esac

PARAMS=$(python3 -c 'import json,sys; print(json.dumps({"commands":[sys.argv[1]]}))' "$SCRIPT")
CMD=$(aws ssm send-command --instance-ids "$IID" --document-name AWS-RunShellScript \
  --comment "mydemoticket backup $TS" --parameters "$PARAMS" --query Command.CommandId --output text)
say "backup started on $IID (command $CMD): $BACKUP"

aws ssm wait command-executed --command-id "$CMD" --instance-id "$IID" 2>/dev/null || true
STATUS=$(aws ssm get-command-invocation --command-id "$CMD" --instance-id "$IID" --query Status --output text)
aws ssm get-command-invocation --command-id "$CMD" --instance-id "$IID" \
  --query '[StandardOutputContent,StandardErrorContent]' --output text
if [ "$STATUS" = "Success" ]; then
  say "OK: backup $BACKUP on $IID"
  exit 0
fi
say "FAILED: command status $STATUS (service was restarted). Check: aws ssm start-session --target $IID"
exit 1
