#!/usr/bin/env bash
# Offline tests for deploy/: runs boot.sh, deploy.sh and backup.sh against PATH stubs for aws, curl and
# security (no network, no AWS, no keychain access), plus install.sh --dry-run.
#
#   deploy/tests/test_deploy_scripts.sh      # prints PASS/FAIL per check, last line "ALL PASSED" or "FAILED: n"
set -uo pipefail

REPO=$(cd "$(dirname "$0")/../.." && pwd)
WORK=$(mktemp -d "${TMPDIR:-/tmp}/mdt-deploy-test.XXXXXX")
[ -n "${KEEP:-}" ] && echo "work dir kept: $WORK" || trap 'rm -rf "$WORK"' EXIT
STUBS="$WORK/stubs"
mkdir -p "$STUBS"
FAILS=0
FAKE_TOKEN="hec-test-token-7f3a9c"   # fake value; must never show up in script output

pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; FAILS=$((FAILS + 1)); }
check() { local msg=$1; shift; if "$@"; then pass "$msg"; else fail "$msg"; fi; }
perms() { ls -l "$1" | cut -c1-10; }

# ------------------------------------------------------------------ stubs
# aws: SSM parameters are files in $STUB_SSM (name = parameter name without /<NAME>/ prefix); every call is
# logged to $STUB_LOG; put-parameter stores the value from the JSON file (never logged).
cat >"$STUBS/aws" <<'EOF'
#!/usr/bin/env bash
echo "aws $*" >>"$STUB_LOG"
args=" $* "
name=""; prev=""
for a in "$@"; do [ "$prev" = "--name" ] && name=$a; prev=$a; done
case "$args" in
  *" ssm get-parameter "*)
    case "$name" in
      /aws/service/*) echo ami-123; exit 0 ;;
    esac
    f="$STUB_SSM/${name##*/}"
    [ -f "$f" ] && { cat "$f"; exit 0; }
    echo "An error occurred (ParameterNotFound)" >&2; exit 254 ;;
  *" ssm put-parameter "*)
    file=""; prev=""
    for a in "$@"; do [ "$prev" = "--cli-input-json" ] && file=${a#file://}; prev=$a; done
    python3 - "$file" "$STUB_SSM" <<'PY'
import json, sys, os
d = json.load(open(sys.argv[1]))
n = d["Name"].rsplit("/", 1)[-1]
open(os.path.join(sys.argv[2], n), "w").write(d["Value"])
open(os.path.join(sys.argv[2], n + ".type"), "w").write(d["Type"])
PY
    exit 0 ;;
  *" ssm delete-parameter "*) f="$STUB_SSM/${name##*/}"; [ -f "$f" ] || exit 254; rm -f "$f" "$f.type"; exit 0 ;;
  *" sts get-caller-identity "*"Account"*) echo 123456789012 ;;
  *" sts get-caller-identity "*) echo arn:aws:sts::123456789012:assumed-role/test ;;
  *" describe-vpcs "*) echo vpc-test ;;
  *" describe-instances "*"PublicIpAddress"*) echo 3.75.130.60 ;;
  *" describe-instances "*) echo i-0test ;;
  *" describe-security-groups "*"IpPermissions"*) echo 8081 ;;
  *" describe-security-groups "*) echo sg-test ;;
  *" iam get-"*|*" s3api head-bucket "*) exit 0 ;;
  *" s3 cp "*) [ -n "${STUB_BUNDLE:-}" ] && cp "$3" "$STUB_BUNDLE"; exit 0 ;;
  *) echo None ;;
esac
exit 0
EOF
# curl: IMDS token / public IP, health check OK
cat >"$STUBS/curl" <<'EOF'
#!/usr/bin/env bash
echo "curl $*" >>"$STUB_LOG"
case " $* " in
  *"/latest/api/token"*) echo imds-token ;;
  *"/public-ipv4"*) echo 3.75.130.60 ;;
  *) echo '{"status":"ok"}' ;;
esac
EOF
# security: keychain; splunk-hec-token only if $STUB_HEC_TOKEN is set; add-generic-password is refused
cat >"$STUBS/security" <<'EOF'
#!/usr/bin/env bash
echo "security $1" >>"$STUB_LOG"
[ "$1" = "find-generic-password" ] || exit 1
acct=""; prev=""
for a in "$@"; do [ "$prev" = "-a" ] && acct=$a; prev=$a; done
case "$acct" in
  splunk-hec-token) [ -n "${STUB_HEC_TOKEN:-}" ] && { echo "$STUB_HEC_TOKEN"; exit 0; }; exit 44 ;;
  admin-password) echo stub-admin-password ;;
  admin-token) echo stub-admin-token ;;
  *) exit 44 ;;
esac
EOF
chmod +x "$STUBS"/*
export PATH="$STUBS:$PATH" STUB_LOG="$WORK/calls.log"
unset AWS_PROFILE
for t in aws curl security; do
  [ "$(command -v $t)" = "$STUBS/$t" ] || { echo "FAIL: $t stub not first on PATH, abort"; exit 1; }
done

# ------------------------------------------------------------------ bash -n
for s in deploy/aws/deploy.sh deploy/aws/boot.sh deploy/aws/backup.sh deploy/aws/user-data.sh deploy/install.sh; do
  check "bash -n $s" bash -n "$REPO/$s"
done

# ------------------------------------------------------------------ boot.sh
# fake source tree: install.sh stub records its arguments
SRC="$WORK/src"
mkdir -p "$SRC/deploy"
printf '#!/usr/bin/env bash\necho "$*" >"%s/install.args"\n' "$WORK" >"$SRC/deploy/install.sh"
chmod +x "$SRC/deploy/install.sh"

run_boot() { # case-name ; params in $STUB_SSM
  local out="$WORK/boot-$1.out"
  NAME=mydemoticket REGION=eu-central-1 SRC="$SRC" ENV_FILE="$WORK/etc-$1/env" \
    bash "$REPO/deploy/aws/boot.sh" >"$out" 2>&1
  echo $? >"$WORK/boot-$1.rc"
}

export STUB_SSM="$WORK/ssm-boot"
mkdir -p "$STUB_SSM"
printf '%s' ticket.soc-games.com >"$STUB_SSM/DOMAIN"
printf '%s' 8081 >"$STUB_SSM/ALB_PORT"
printf '%s' pw-secret >"$STUB_SSM/ADMIN_PASSWORD"
printf '%s' tok-secret >"$STUB_SSM/ADMIN_TOKEN"
printf '%s' https://splunk.soc-games.com:8088 >"$STUB_SSM/HEC_URL"
printf '%s' mydemoticket >"$STUB_SSM/HEC_INDEX"
printf '%s' "$FAKE_TOKEN" >"$STUB_SSM/HEC_TOKEN"

run_boot hec-on
ENV="$WORK/etc-hec-on/env"
check "boot.sh (HEC on) exit 0" [ "$(cat "$WORK/boot-hec-on.rc")" = 0 ]
check "env file mode 0600" [ "$(perms "$ENV")" = "-rw-------" ]
check "env has MYDEMOTICKET_HEC_URL" grep -qx 'MYDEMOTICKET_HEC_URL=https://splunk.soc-games.com:8088' "$ENV"
check "env has MYDEMOTICKET_HEC_TOKEN (value matches SSM)" grep -qx "MYDEMOTICKET_HEC_TOKEN=$FAKE_TOKEN" "$ENV"
check "env has MYDEMOTICKET_HEC_INDEX" grep -qx 'MYDEMOTICKET_HEC_INDEX=mydemoticket' "$ENV"
check "env has MYDEMOTICKET_HEC_VERIFY_TLS=1" grep -qx 'MYDEMOTICKET_HEC_VERIFY_TLS=1' "$ENV"
check "env keeps admin credentials" grep -qx 'MYDEMOTICKET_ADMIN_PASSWORD=pw-secret' "$ENV"
check "env has managed marker" grep -qx '# managed by mydemoticket' "$ENV"
check "no temp env file left" [ ! -e "$ENV.tmp" ]
check "boot.sh output does not contain the HEC token" bash -c "! grep -qF '$FAKE_TOKEN' '$WORK/boot-hec-on.out'"
check "install.sh called with domain + alb port" grep -q -- '--domain 3-75-130-60.sslip.io --python /usr/bin/python3 --alb-port 8081' "$WORK/install.args"

# second boot with changed index: the file is rewritten, not kept
printf '%s' other-index >"$STUB_SSM/HEC_INDEX"
run_boot hec-on
check "env file rewritten on every boot" grep -qx 'MYDEMOTICKET_HEC_INDEX=other-index' "$ENV"

# HEC parameters missing -> HEC off, boot still succeeds
rm -f "$STUB_SSM/HEC_URL" "$STUB_SSM/HEC_INDEX" "$STUB_SSM/HEC_TOKEN" "$STUB_SSM/ALB_PORT"
run_boot hec-missing
ENV="$WORK/etc-hec-missing/env"
check "boot.sh (HEC params missing) exit 0" [ "$(cat "$WORK/boot-hec-missing.rc")" = 0 ]
check "env without HEC vars when params missing" bash -c "! grep -q MYDEMOTICKET_HEC_ '$ENV'"
check "output says HEC off" grep -q 'Splunk HEC off' "$WORK/boot-hec-missing.out"
check "no ALB port -> domain + sslip" grep -q -- '--domain ticket.soc-games.com,3-75-130-60.sslip.io --python /usr/bin/python3$' "$WORK/install.args"

# HEC_URL=none with a stale token parameter -> off
printf '%s' none >"$STUB_SSM/HEC_URL"
printf '%s' "$FAKE_TOKEN" >"$STUB_SSM/HEC_TOKEN"
run_boot hec-none
check "HEC_URL=none -> no HEC vars" bash -c "! grep -q MYDEMOTICKET_HEC_ '$WORK/etc-hec-none/env'"

# URL set but token missing -> off
printf '%s' https://splunk.soc-games.com:8088 >"$STUB_SSM/HEC_URL"
rm -f "$STUB_SSM/HEC_TOKEN"
run_boot hec-notoken
check "token missing -> exit 0 and no HEC vars" bash -c "[ \$(cat '$WORK/boot-hec-notoken.rc') = 0 ] && ! grep -q MYDEMOTICKET_HEC_ '$WORK/etc-hec-notoken/env'"

# ------------------------------------------------------------------ deploy.sh against stubs
export STUB_SSM="$WORK/ssm-deploy"
mkdir -p "$STUB_SSM"
run_deploy() { # case-name mode
  NAME=mydemoticket bash "$REPO/deploy/aws/deploy.sh" $2 >"$WORK/deploy-$1.out" 2>&1
  echo $? >"$WORK/deploy-$1.rc"
}

STUB_HEC_TOKEN="$FAKE_TOKEN" run_deploy dry --dry-run
check "deploy.sh --dry-run exit 0" [ "$(cat "$WORK/deploy-dry.rc")" = 0 ]
check "dry run names HEC parameters" grep -q '/mydemoticket/HEC_URL=https://splunk.soc-games.com:8088, /mydemoticket/HEC_INDEX=mydemoticket, /mydemoticket/HEC_TOKEN (SecureString' "$WORK/deploy-dry.out"
check "dry run writes no parameter" [ -z "$(ls "$STUB_SSM")" ]
check "dry run output does not contain the token" bash -c "! grep -qF '$FAKE_TOKEN' '$WORK/deploy-dry.out'"

STUB_BUNDLE="$WORK/bundle.tgz" STUB_HEC_TOKEN="$FAKE_TOKEN" run_deploy apply ""
check "deploy.sh apply (stubbed) exit 0" [ "$(cat "$WORK/deploy-apply.rc")" = 0 ]
tar -tzf "$WORK/bundle.tgz" >"$WORK/bundle.list" 2>/dev/null
check "bundle has the package" grep -qx 'ticket-system/mydemoticket/pairing.py' "$WORK/bundle.list"
check "bundle has the SOAR app (pairing wizard)" grep -qx 'soar-app/mydemoticket/mydemoticket.json' "$WORK/bundle.list"
check "bundle has both playbooks" bash -c "grep -qx 'soar-playbooks/mydemoticket_es_mirror.py' '$WORK/bundle.list' && grep -qx 'soar-playbooks/mydemoticket_ticket_from_es.json' '$WORK/bundle.list'"
check "bundle has no app tests, dist or caches" bash -c "! grep -qE '^soar-app/(tests|dist)|__pycache__' '$WORK/bundle.list'"
check "HEC_TOKEN written as SecureString with keychain value" \
  bash -c "[ \"\$(cat '$STUB_SSM/HEC_TOKEN.type')\" = SecureString ] && [ \"\$(cat '$STUB_SSM/HEC_TOKEN')\" = '$FAKE_TOKEN' ]"
check "HEC_URL written as String (default URL)" \
  bash -c "[ \"\$(cat '$STUB_SSM/HEC_URL.type')\" = String ] && [ \"\$(cat '$STUB_SSM/HEC_URL')\" = https://splunk.soc-games.com:8088 ]"
check "HEC_INDEX written as String mydemoticket" \
  bash -c "[ \"\$(cat '$STUB_SSM/HEC_INDEX.type')\" = String ] && [ \"\$(cat '$STUB_SSM/HEC_INDEX')\" = mydemoticket ]"
check "apply output does not contain the token" bash -c "! grep -qF '$FAKE_TOKEN' '$WORK/deploy-apply.out'"
check "token never on an aws command line" bash -c "! grep -qF '$FAKE_TOKEN' '$STUB_LOG'"
check "keychain never written" bash -c "! grep -q 'security add-generic-password' '$STUB_LOG'"

# keychain item missing -> warning, HEC off, no abort, stale token parameter removed
STUB_HEC_TOKEN="" run_deploy nokey ""
check "deploy.sh without HEC keychain item exit 0" [ "$(cat "$WORK/deploy-nokey.rc")" = 0 ]
check "warning about missing keychain item" grep -q 'WARNING: keychain item mydemoticket/splunk-hec-token missing' "$WORK/deploy-nokey.out"
check "HEC_URL=none and HEC_TOKEN removed" bash -c "[ \"\$(cat '$STUB_SSM/HEC_URL')\" = none ] && [ ! -e '$STUB_SSM/HEC_TOKEN' ]"

# HEC_URL=none -> off
printf '%s' x >"$STUB_SSM/HEC_TOKEN"
STUB_HEC_TOKEN="$FAKE_TOKEN" HEC_URL=none run_deploy hecnone ""
check "HEC_URL=none -> /HEC_URL=none, token removed" bash -c "[ \"\$(cat '$STUB_SSM/HEC_URL')\" = none ] && [ ! -e '$STUB_SSM/HEC_TOKEN' ]"

STUB_HEC_TOKEN="$FAKE_TOKEN" run_deploy apply2 ""
run_deploy uninstall --uninstall
check "deploy.sh --uninstall exit 0" [ "$(cat "$WORK/deploy-uninstall.rc")" = 0 ]
check "uninstall deletes HEC parameters" bash -c "for p in HEC_URL HEC_INDEX HEC_TOKEN; do grep -q \"delete parameter /mydemoticket/\$p\" '$WORK/deploy-uninstall.out' || exit 1; done"

# ------------------------------------------------------------------ backup.sh
: >"$STUB_LOG"
bash "$REPO/deploy/aws/backup.sh" --dry-run >"$WORK/backup-dry.out" 2>&1
check "backup.sh --dry-run exit 0" [ $? = 0 ]
check "backup dry run makes no aws call" [ ! -s "$STUB_LOG" ]
check "backup dry run shows stop/cp/start" \
  grep -qE 'systemctl stop mydemoticket; cp -a /var/lib/mydemoticket /var/lib/mydemoticket\.bak-[0-9]{8}T[0-9]{6}Z; .*systemctl start mydemoticket' "$WORK/backup-dry.out"
bash "$REPO/deploy/aws/backup.sh" --bogus >/dev/null 2>&1
check "backup.sh rejects unknown option" [ $? = 2 ]

# ------------------------------------------------------------------ install.sh
IROOT="$WORK/iroot"
bash "$REPO/deploy/install.sh" --dry-run --root "$IROOT" >"$WORK/install-dry.out" 2>&1
check "install.sh --dry-run --root exit 0" [ $? = 0 ]
check "rendered unit: ReadWritePaths covers data dir (logs inside)" grep -qx 'ReadWritePaths=/var/lib/mydemoticket' "$IROOT/etc/systemd/system/mydemoticket.service"
check "rendered unit reads env file" grep -qx 'EnvironmentFile=-/etc/mydemoticket/env' "$IROOT/etc/systemd/system/mydemoticket.service"
check "install copies the SOAR app next to the package" [ -f "$IROOT/opt/mydemoticket/soar-app/mydemoticket/mydemoticket.json" ]
check "install copies the playbooks next to the package" [ -f "$IROOT/opt/mydemoticket/soar-playbooks/mydemoticket_es_mirror.py" ]
check "installed package finds the pairing bundle" bash -c "cd '$IROOT/opt/mydemoticket' && env -u MYDEMOTICKET_SOAR_BUNDLE_DIR python3 -c 'import os, sys, mydemoticket.pairing as p; sys.exit(0 if os.path.realpath(p.bundle_dir() or \"\") == os.path.realpath(sys.argv[1]) else 1)' '$IROOT/opt/mydemoticket'"
printf '# managed by mydemoticket\nMYDEMOTICKET_HEC_URL=https://keep.example:8088\n' >"$IROOT/etc/mydemoticket/env"
bash "$REPO/deploy/install.sh" --dry-run --root "$IROOT" >/dev/null 2>&1
check "install.sh keeps an existing env file" grep -qx 'MYDEMOTICKET_HEC_URL=https://keep.example:8088' "$IROOT/etc/mydemoticket/env"

echo
if [ "$FAILS" -eq 0 ]; then echo "ALL PASSED"; else echo "FAILED: $FAILS"; exit 1; fi
