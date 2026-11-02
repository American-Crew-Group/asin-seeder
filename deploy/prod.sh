#!/usr/bin/env bash
# Deploy and operate the seeder service on the server described in deploy.prod.yml.
# Usually run through Taskfile.yml (task deploy, task status ...).
#
#   deploy/prod.sh deploy | status | stop | start | logs | migrate-data | init-data | upload-env
#
# CONFIG=other.yml selects another config file.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
CONFIG=${CONFIG:-$ROOT/deploy.prod.yml}
CODE_FILES=(send_asins.py convert_asins.py build_payload.py deduplicate_asins.py README.md .env.example)

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

# Value of a top-level "key: value" line, without quotes or trailing comment.
cfg() {
  local value
  value=$(awk -v key="$1" '
    $0 ~ "^" key ":" {
      sub("^" key ":[ \t]*", ""); sub(/[ \t]+#.*$/, ""); sub(/[ \t]+$/, "")
      gsub(/^["\047]|["\047]$/, ""); print; exit
    }' "$CONFIG")
  [[ -n $value ]] || die "$CONFIG: missing '$1'"
  printf '%s' "$value"
}

[[ -f $CONFIG ]] || die "config file $CONFIG not found"
HOST=$(cfg host)
SSH_USER=$(cfg ssh_user)
SSH_PORT=$(cfg ssh_port)
SSH_KEY=$(cfg ssh_key)
SSH_KEY=${SSH_KEY/#\~/$HOME}
APP_DIR=$(cfg remote_dir)
SERVICE=$(cfg service_name)
SERVICE_USER=$(cfg service_user)
BATCH_SIZE=$(cfg batch_size)
INTERVAL=$(cfg interval)
MIGRATE_FROM=$(cfg migrate_from)
[[ -f $SSH_KEY ]] || die "SSH key $SSH_KEY not found"
[[ $APP_DIR == /* ]] || die "$CONFIG: remote_dir must be an absolute path"
[[ $BATCH_SIZE =~ ^[1-9][0-9]*$ ]] || die "$CONFIG: batch_size must be a positive integer"
[[ $INTERVAL =~ ^[0-9]+(\.[0-9]+)?$ ]] || die "$CONFIG: interval must be a number of seconds"
[[ $MIGRATE_FROM == /* ]] || die "$CONFIG: migrate_from must be an absolute path"

COMMON=(-i "$SSH_KEY" -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new
        -o ControlMaster=auto -o "ControlPath=$HOME/.ssh/cm-asin-seeder-%r@%h:%p" -o ControlPersist=60)
TARGET="$SSH_USER@$HOST"

remote_ssh() { ssh "${COMMON[@]}" -p "$SSH_PORT" "$TARGET" "$@"; }

# Run deploy/server.sh on the server as root with the config values.
server() {
  local env
  env=$(printf '%q ' "APP_DIR=$APP_DIR" "SERVICE=$SERVICE" "SERVICE_USER=$SERVICE_USER" \
                     "LOGIN_USER=$SSH_USER" "BATCH_SIZE=$BATCH_SIZE" "INTERVAL=$INTERVAL" \
                     "MIGRATE_FROM=$MIGRATE_FROM")
  remote_ssh "sudo env $env bash -s -- $(printf '%q ' "$@")" < "$ROOT/deploy/server.sh"
}

# Copy files with scp into a fresh private folder on the server; print its path.
upload() {
  local staging
  staging=$(remote_ssh 'mktemp -d /tmp/asin-seeder-upload.XXXXXX')
  scp -q "${COMMON[@]}" -P "$SSH_PORT" "$@" "$TARGET:$staging/" >&2
  printf '%s' "$staging"
}

local_stage() {
  STAGE=$(mktemp -d)
  trap 'rm -rf "$STAGE"' EXIT
}

deploy() {
  local revision staging file
  cd "$ROOT"
  for file in "${CODE_FILES[@]}" deploy/asin-seeder.service deploy/server.sh; do
    [[ -f $file ]] || die "missing $file"
  done
  if [[ -n $(git status --porcelain -- "${CODE_FILES[@]}" deploy 2>/dev/null) ]]; then
    echo 'WARNING: deploying uncommitted changes' >&2
  fi
  local_stage
  revision="$(git describe --always --dirty 2>/dev/null || echo unknown) deployed $(date -u +%FT%TZ) by $(whoami)"
  printf '%s\n' "$revision" > "$STAGE/REVISION"
  cp "${CODE_FILES[@]}" deploy/asin-seeder.service "$STAGE/"
  echo "==> Uploading $revision to $TARGET"
  staging=$(upload "$STAGE"/* "$STAGE/.env.example")
  server deploy "$staging"
}

init_data() {
  local staging files=("$ROOT/csv/formatted/asins.csv")
  [[ -f ${files[0]} ]] || die "no local ${files[0]} to upload"
  [[ -f $ROOT/csv/raw/processed.json ]] && files+=("$ROOT/csv/raw/processed.json")
  echo "==> Uploading ${files[*]##*/} (only installed where the server has none)"
  staging=$(upload "${files[@]}")
  server init-data "$staging"
}

upload_env() {
  local staging
  [[ -f $ROOT/.env ]] || die "no local .env; copy .env.example and fill it in"
  grep -qE '^APIENDPOINT=.+' "$ROOT/.env" && grep -qE '^AUTHENDPOINT=.+' "$ROOT/.env" \
    || die 'the local .env must give APIENDPOINT and AUTHENDPOINT a value'
  staging=$(upload "$ROOT/.env")
  server upload-env "$staging"
}

case ${1:-} in
  deploy) deploy ;;
  init-data) init_data ;;
  migrate-data) server migrate-data ;;
  upload-env) upload_env ;;
  status) server status ;;
  stop) server stop ;;
  start) server start ;;
  logs) ssh "${COMMON[@]}" -p "$SSH_PORT" -t "$TARGET" "sudo journalctl -u $(printf '%q' "$SERVICE") -n 100 -f" ;;
  *) die "usage: $0 deploy|status|stop|start|logs|migrate-data|init-data|upload-env" ;;
esac
