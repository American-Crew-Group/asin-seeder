#!/usr/bin/env bash
# Server side of deploy/prod.sh. Runs as root on the server, piped over ssh:
#   APP_DIR=... SERVICE=... SERVICE_USER=... LOGIN_USER=... BATCH_SIZE=... INTERVAL=... MIGRATE_FROM=... \
#     bash -s -- <command> [staging dir]
#
# The data (csv/formatted/asins.csv, csv/raw/processed.json and their companions) is the source of
# truth: no command here changes it. migrate-data and init-data only create it where it is missing.
set -euo pipefail

: "${APP_DIR:?}" "${SERVICE:?}" "${SERVICE_USER:?}" "${LOGIN_USER:?}" "${BATCH_SIZE:?}" "${INTERVAL:?}" "${MIGRATE_FROM:?}"
UNIT="/etc/systemd/system/$SERVICE.service"
CSV="$APP_DIR/csv/formatted/asins.csv"
MANIFEST="$APP_DIR/csv/raw/processed.json"
CODE_FILES=(send_asins.py convert_asins.py build_payload.py deduplicate_asins.py README.md .env.example REVISION)

step() { printf '==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
state() { systemctl show -p "$1" --value "$SERVICE" 2>/dev/null; }

# Inode, size and hash of each data file: proves a deploy left them untouched.
fingerprint() {
  local file
  for file in "$CSV" "$MANIFEST"; do
    if [[ -e $file ]]; then
      printf '%s %s %s\n' "$file" "$(stat -c '%i %s' "$file")" "$(sha256sum < "$file" | cut -d' ' -f1)"
    else
      printf '%s missing\n' "$file"
    fi
  done
}

# Users, folders and permissions. Only the service user can change or delete the data; the login
# user can read it and drop raw exports into csv/raw/ (sticky: nobody can remove another's files).
prepare() {
  command -v python3 >/dev/null || die 'python3 is not installed on the server'
  command -v flock >/dev/null || die 'flock (util-linux) is not installed on the server'
  if ! id "$SERVICE_USER" >/dev/null 2>&1; then
    step "Creating system user $SERVICE_USER"
    useradd --system --user-group --no-create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
  fi
  id -nG "$LOGIN_USER" | tr ' ' '\n' | grep -qx "$SERVICE_USER" || usermod -aG "$SERVICE_USER" "$LOGIN_USER"
  install -d -o root -g root -m 0755 "$APP_DIR" "$APP_DIR/deploy"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0755 \
    "$APP_DIR/csv" "$APP_DIR/csv/formatted" "$APP_DIR/csv/old" "$APP_DIR/json_payloads"
  install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 1775 "$APP_DIR/csv/raw"
  # Ownership and mode only; contents are never touched.
  chown -R "$SERVICE_USER:$SERVICE_USER" "$APP_DIR/csv/formatted"
  find "$APP_DIR/csv/formatted" -type f -exec chmod 0644 {} +
  if [[ -e $MANIFEST ]]; then
    chown "$SERVICE_USER:$SERVICE_USER" "$MANIFEST"
    chmod 0644 "$MANIFEST"
  fi
}

# Wait until the service has read the CSV and stays up; print its log either way.
verify() {
  local invocation pid restarts output='' _
  for _ in $(seq 60); do
    [[ $(state ActiveState) == active ]] && break
    sleep 1
  done
  invocation=$(state InvocationID)
  for _ in $(seq 90); do
    [[ $(state ActiveState) == active ]] || break
    output=$(journalctl "_SYSTEMD_INVOCATION_ID=$invocation" -o cat --no-pager 2>/dev/null || true)
    grep -q '^Pending:' <<<"$output" && break
    sleep 1
  done
  if ! grep -q '^Pending:' <<<"$output"; then
    journalctl -u "$SERVICE" -n 30 --no-pager || true
    die "$SERVICE did not start; see the log above"
  fi
  pid=$(state MainPID)
  restarts=$(state NRestarts)
  sleep 10
  if [[ $(state ActiveState) != active || $(state MainPID) != "$pid" || $(state NRestarts) != "$restarts" ]]; then
    journalctl -u "$SERVICE" -n 30 --no-pager || true
    die "$SERVICE stopped right after starting; see the log above"
  fi
  printf '%s\n' "$output" | tail -n 5
  step "$SERVICE is running (PID $pid)"
}

stop_service() {
  if [[ $(state ActiveState) =~ ^(active|activating|reloading|deactivating)$ ]]; then
    step "Stopping $SERVICE"
    systemctl stop "$SERVICE"
  fi
  [[ $(state ActiveState) =~ ^(inactive|failed|)$ ]] || die "$SERVICE did not stop"
  # A sender started by hand would keep writing the CSV behind systemd's back.
  if [[ -e $CSV.lock ]] && ! flock -n "$CSV.lock" true; then
    die "another process holds $CSV.lock (a send_asins.py started by hand?); stop it first"
  fi
}

cmd_deploy() {
  local staging=$1 before after
  [[ -d $staging ]] || die "missing upload folder $staging"
  prepare
  [[ -f $APP_DIR/.env ]] || die "$APP_DIR/.env with APIENDPOINT and AUTHENDPOINT is missing; run task migrate-data (takes it from the old setup) or task upload-env"
  chown root:root "$APP_DIR/.env"
  chmod 0600 "$APP_DIR/.env"
  stop_service
  before=$(fingerprint)
  step "Installing $(head -n 1 "$staging/REVISION") into $APP_DIR"
  for file in "${CODE_FILES[@]}"; do
    install -o root -g root -m 0644 "$staging/$file" "$APP_DIR/$file"
  done
  install -o root -g root -m 0644 "$staging/asin-seeder.service" "$APP_DIR/deploy/asin-seeder.service"
  sed -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@SERVICE_USER@|$SERVICE_USER|g" -e "s|@BATCH_SIZE@|$BATCH_SIZE|g" -e "s|@INTERVAL@|$INTERVAL|g" \
    "$staging/asin-seeder.service" > "$UNIT.tmp"
  chmod 0644 "$UNIT.tmp"
  mv "$UNIT.tmp" "$UNIT"
  systemctl daemon-reload
  systemctl enable --quiet "$SERVICE"
  rm -rf "$staging"
  after=$(fingerprint)
  [[ $before == "$after" ]] || die "data files changed during the deploy; $SERVICE left stopped"
  [[ -f $CSV ]] || die "$CSV does not exist yet; run task migrate-data (or task init-data on a new server), then deploy again"
  step "Data untouched:"
  sed 's/^/    /' <<<"$after"
  step "Starting $SERVICE"
  systemctl start "$SERVICE"
  verify
}

cmd_init_data() {
  local staging=$1
  prepare
  [[ ! -e $CSV ]] || die "$CSV already exists; it is the source of truth and is never replaced"
  [[ ! -e $MIGRATE_FROM/csv/formatted/asins.csv ]] \
    || die "the server already has data in $MIGRATE_FROM; use task migrate-data, not local data"
  stop_service
  install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0644 "$staging/asins.csv" "$CSV"
  step "Installed $CSV ($(($(wc -l < "$CSV") - 1)) rows)"
  if [[ -f $staging/processed.json && ! -e $MANIFEST ]]; then
    install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0644 "$staging/processed.json" "$MANIFEST"
    step "Installed $MANIFEST"
  fi
  rm -rf "$staging"
}

# One-time move of the data from the folder used before the service (migrate_from) into APP_DIR.
# Everything is copied on the server, checked by hash, and the old asins.csv and processed.json are
# renamed so nothing can keep sending from the stale copy. Credentials come from the old .env or,
# when its values are empty, from the export lines in the login user's ~/.bashrc.
cmd_migrate_data() {
  local from=$MIGRATE_FROM old_csv=$MIGRATE_FROM/csv/formatted/asins.csv source target name copied=()
  [[ -f $old_csv ]] || die "no $old_csv to migrate"
  [[ ! -e $CSV ]] || die "$CSV already exists; the data was already migrated"
  prepare
  stop_service
  # Hold the old lock while copying, so a sender started by hand cannot change the data meanwhile.
  exec 9>>"$old_csv.lock"
  flock -n 9 || die "a send_asins.py is running in $from; stop it first"
  step "Copying data from $from to $APP_DIR"
  for source in "$from"/csv/formatted/* "$from"/csv/raw/processed.json "$from"/csv/raw/*.csv "$from"/json_payloads/*.json; do
    [[ -f $source ]] || continue
    name=${source##*/}
    [[ $name == *.lock ]] && continue
    target=$APP_DIR/${source#"$from"/}
    [[ ! -e $target ]] || die "$target already exists"
    cp --preserve=timestamps "$source" "$target.migrating"
    [[ $(sha256sum < "$source") == "$(sha256sum < "$target.migrating")" ]] || die "copy of $source differs"
    chown "$SERVICE_USER:$SERVICE_USER" "$target.migrating"
    chmod 0644 "$target.migrating"
    copied+=("$target")
  done
  # Same order as the copies; asins.csv appears only when everything is in place.
  for target in "${copied[@]}"; do
    mv "$target.migrating" "$target"
  done
  printf '    %s\n' "${copied[@]#"$APP_DIR"/}"
  mv "$old_csv" "$old_csv.migrated"
  [[ -e $from/csv/raw/processed.json ]] && mv "$from/csv/raw/processed.json" "$from/csv/raw/processed.json.migrated"
  step "Renamed the old asins.csv and processed.json to *.migrated in $from"
  if [[ ! -e $APP_DIR/.env ]]; then
    local env_file=$APP_DIR/.env.migrating rc
    rc=$(getent passwd "$LOGIN_USER" | cut -d: -f6)/.bashrc
    if grep -qE '^APIENDPOINT=.+' "$from/.env" 2>/dev/null && grep -qE '^AUTHENDPOINT=.+' "$from/.env"; then
      grep -E '^(APIENDPOINT|AUTHENDPOINT)=' "$from/.env" > "$env_file"
    else
      sed -nE 's/^[[:space:]]*(export[[:space:]]+)?((APIENDPOINT|AUTHENDPOINT)=.+)$/\2/p' "$rc" > "$env_file"
    fi
    if grep -qE '^APIENDPOINT=.+' "$env_file" && grep -qE '^AUTHENDPOINT=.+' "$env_file"; then
      chown root:root "$env_file"
      chmod 0600 "$env_file"
      mv "$env_file" "$APP_DIR/.env"
      step "Wrote $APP_DIR/.env from the previous setup (values not shown)"
    else
      rm -f "$env_file"
      echo "WARNING: no credentials found in $from/.env or $rc; run: task upload-env" >&2
    fi
  fi
  step "Migration done; now run: task deploy"
}

cmd_upload_env() {
  local staging=$1
  install -d -o root -g root -m 0755 "$APP_DIR"
  getent group "$SERVICE_USER" >/dev/null || prepare
  install -o root -g root -m 0600 "$staging/.env" "$APP_DIR/.env"
  rm -rf "$staging"
  step "Installed $APP_DIR/.env; it is read when $SERVICE starts (task stop && task start)"
}

cmd_status() {
  local active
  active=$(state ActiveState)
  printf 'Service:   %s (%s, %s)\n' "$SERVICE" "${active:-not installed}" "$(state UnitFileState)"
  printf 'Since:     %s\n' "$(state ActiveEnterTimestamp)"
  printf 'PID:       %s   restarts: %s\n' "$(state MainPID)" "$(state NRestarts)"
  printf 'Revision:  %s\n' "$(head -n 1 "$APP_DIR/REVISION" 2>/dev/null || echo unknown)"
  if [[ -f $CSV ]]; then
    python3 - "$CSV" <<'PY'
import csv, sys
total = sent = pending = unknown = failed = 0
with open(sys.argv[1], encoding='utf-8-sig', newline='') as file:
    for row in csv.DictReader(file):
        total += 1
        reason = row['reason'].strip()
        if row['sent'].strip().lower() == 'true':
            sent += 1
        elif not reason:
            pending += 1
        elif reason.startswith('UNKNOWN'):
            unknown += 1
        else:
            failed += 1
print(f'CSV rows:  {total:,} total, {sent:,} sent, {pending:,} pending, {failed:,} failed, {unknown:,} unknown')
PY
  else
    printf 'CSV:       %s missing\n' "$CSV"
  fi
  if [[ -f $APP_DIR/csv/formatted/asins.errors.csv ]]; then
    echo 'Top errors (count, last seen, reason):'
    python3 - "$APP_DIR/csv/formatted/asins.errors.csv" <<'PY'
import csv, sys
with open(sys.argv[1], encoding='utf-8-sig', newline='') as file:
    for row in list(csv.DictReader(file))[:5]:
        reason = ' '.join(row['error'].split())
        print(f"    {int(row['count']):>8,}  {row['last_seen'][:10]}  {reason[:110]}{'…' if len(reason) > 110 else ''}")
PY
  fi
  echo 'Last log lines:'
  journalctl -u "$SERVICE" -n 15 --no-pager -o short-iso 2>/dev/null | sed 's/^/    /' || true
  [[ $active == active ]]
}

cmd_stop() {
  stop_service
  step "$SERVICE stopped; it stays enabled and starts again on boot or with: task start"
}

cmd_start() {
  [[ -f $UNIT ]] || die "$SERVICE is not installed; run: task deploy"
  if [[ $(state ActiveState) == active ]]; then
    step "$SERVICE is already running (PID $(state MainPID))"
    return
  fi
  systemctl start "$SERVICE"
  verify
}

command=${1:-}
shift || true
case $command in
  deploy) cmd_deploy "$@" ;;
  init-data) cmd_init_data "$@" ;;
  migrate-data) cmd_migrate_data ;;
  upload-env) cmd_upload_env "$@" ;;
  status) cmd_status ;;
  stop) cmd_stop ;;
  start) cmd_start ;;
  *) die "unknown command '$command'" ;;
esac
