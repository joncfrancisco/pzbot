#!/usr/bin/env bash
# Install or upgrade pzbot on the bot host. Idempotent: safe to re-run for every deploy.
#
#   sudo ./deploy/install.sh [--connect-host pz.joncfrancis.co]
#
# There is no SSH to this box. The way in is:
#   aws ssm start-session --target "$(terraform -chdir=infra output -raw bot_instance_id)"
#
# What this does NOT do is put any secret on disk. The Discord token and the RCON
# password stay in Parameter Store and are read at startup through the instance role --
# so rotating either one is a put-parameter and a restart, with nothing to clean up.
set -euo pipefail

APP_DIR=/opt/pzbot
VENV="${APP_DIR}/venv"
ENV_FILE=/etc/pzbot/env
UNIT=/etc/systemd/system/pzbot.service
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMDS=http://169.254.169.254

CONNECT_HOST=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --connect-host) CONNECT_HOST="$2"; shift 2 ;;
    *) echo "unknown argument: $1" >&2; exit 1 ;;
  esac
done

log() { echo "pzbot-install: $*" >&2; }
die() { log "FATAL: $*"; exit 1; }

[[ $EUID -eq 0 ]] || die "run me as root (sudo $0)"

# --- Where am I? ----------------------------------------------------------------------

token="$(curl -fsS -X PUT "$IMDS/latest/api/token" \
  -H "X-aws-ec2-metadata-token-ttl-seconds: 300")" || die "no IMDS -- is this the bot host?"
imds() { curl -fsS -H "X-aws-ec2-metadata-token: $token" "$IMDS/latest/$1"; }

REGION="$(imds meta-data/placement/region)"
INSTANCE_ID="$(imds meta-data/instance-id)"
# The stack tag, from the instance's own metadata. Same trick as pz-config-refresh.sh on
# the game server: nothing needs to be baked in, and a second stack works unmodified.
STACK="$(imds meta-data/tags/instance/pz:stack 2>/dev/null || true)"
if [[ -z "$STACK" ]]; then
  STACK="$(AWS_DEFAULT_REGION=$REGION aws ec2 describe-tags \
    --filters "Name=resource-id,Values=$INSTANCE_ID" "Name=key,Values=pz:stack" \
    --query 'Tags[0].Value' --output text 2>/dev/null || true)"
fi
[[ -n "$STACK" && "$STACK" != "None" ]] || die "cannot determine the pz:stack tag; refusing to guess"
log "instance=$INSTANCE_ID region=$REGION stack=$STACK"

# --- Packages and user ------------------------------------------------------------------

if ! command -v python3.12 >/dev/null; then
  log "installing python3.12"
  dnf install -y python3.12 python3.12-pip >/dev/null
fi

id pzbot >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin pzbot

# --- Code ---------------------------------------------------------------------------------

install -d -o pzbot -g pzbot "$APP_DIR"

# The venv is rebuilt from scratch on a Python minor upgrade, and reused otherwise: a
# venv whose interpreter has been replaced underneath it fails in ways that read like
# application bugs.
if [[ -x "${VENV}/bin/python" ]] && ! "${VENV}/bin/python" -c 'import sys; sys.exit(0)' 2>/dev/null; then
  log "existing venv is broken; rebuilding"
  rm -rf "$VENV"
fi
[[ -d "$VENV" ]] || python3.12 -m venv "$VENV"

log "installing dependencies"
"${VENV}/bin/pip" install --quiet --upgrade pip

# --require-hashes. requirements.txt is a pip-compile lockfile: every package pinned to an
# exact version and every artifact pinned to a hash, transitive dependencies included.
#
# This host holds ec2:StartInstances, ec2:StopInstances, root shell on the game server and
# the decrypted Discord token. It is the highest-value box in the stack, and until now it
# had the loosest supply chain: the old ranges (discord.py>=2.4,<3, boto3>=1.34) meant
# every redeploy resolved whatever minor and patch PyPI happened to be serving that
# afternoon, unverified. The file's own comment claimed it existed so "a redeploy cannot
# silently pull a new major of discord.py onto a box nobody is watching" -- true of majors
# only.
#
# --require-hashes also fails closed on an incomplete lockfile: if requirements.txt is
# edited by hand and a transitive dependency loses its hash, pip refuses the whole install
# rather than quietly falling back. Regenerate with pip-compile, never by hand.
"${VENV}/bin/pip" install --quiet --require-hashes -r "${REPO}/requirements.txt"

# --no-deps, so the application itself cannot pull anything past the lockfile.
"${VENV}/bin/pip" install --quiet --no-deps "${REPO}"

# --- Configuration --------------------------------------------------------------------------

install -d -m 0755 /etc/pzbot
if [[ -f "$ENV_FILE" ]]; then
  log "keeping the existing $ENV_FILE"
  # Only fill in the value that has no sensible default, and only if it is missing.
  if [[ -n "$CONNECT_HOST" ]]; then
    sed -i -E "s|^PZBOT_CONNECT_HOST=.*|PZBOT_CONNECT_HOST=${CONNECT_HOST}|" "$ENV_FILE"
  fi
else
  [[ -n "$CONNECT_HOST" ]] || CONNECT_HOST="pz.$(echo "$STACK" | tr -d '\n').invalid"
  log "writing $ENV_FILE"
  sed \
    -e "s|^PZBOT_STACK=.*|PZBOT_STACK=${STACK}|" \
    -e "s|^PZBOT_REGION=.*|PZBOT_REGION=${REGION}|" \
    -e "s|^AWS_DEFAULT_REGION=.*|AWS_DEFAULT_REGION=${REGION}|" \
    -e "s|^PZBOT_CONNECT_HOST=.*|PZBOT_CONNECT_HOST=${CONNECT_HOST}|" \
    "${REPO}/deploy/pzbot.env.example" >"$ENV_FILE"
fi
# 0644: there is nothing secret in here. Every secret is in Parameter Store, read at
# startup through the instance role.
chmod 0644 "$ENV_FILE"

# --- systemd ----------------------------------------------------------------------------------

install -m 0644 "${REPO}/deploy/pzbot.service" "$UNIT"
systemctl daemon-reload
systemctl enable pzbot.service >/dev/null
systemctl restart pzbot.service

sleep 3
if systemctl is-active --quiet pzbot.service; then
  log "pzbot is running. Follow it with: journalctl -u pzbot -f"
else
  log "pzbot did not stay up. The reason is almost always missing configuration:"
  journalctl -u pzbot -n 30 --no-pager >&2
  exit 1
fi
