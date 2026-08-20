#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

backup_dir="${MEFAST_BACKUP_DIR:-/var/backups/mefast}"
retention_days="${MEFAST_BACKUP_RETENTION_DAYS:-14}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
stage_dir="$(mktemp -d)"
archive_path="${backup_dir}/mefast-${timestamp}.tar.gz"

cleanup() {
  rm -rf -- "$stage_dir"
}
trap cleanup EXIT

install -d -m 700 "$backup_dir"

copy_if_exists() {
  local source="$1"
  local destination="$2"
  if [[ -e "$source" ]]; then
    install -d -m 700 "$(dirname "$destination")"
    cp -a -- "$source" "$destination"
  fi
}

# SQLite's backup API produces a consistent copy while the API service is live.
if [[ -f /opt/speedy-vpn-backend/speedy_vpn_data.sqlite3 ]]; then
  install -d -m 700 "$stage_dir/backend"
  python3 - <<'PY' "$stage_dir/backend/speedy_vpn_data.sqlite3"
import sqlite3
import sys

source = "/opt/speedy-vpn-backend/speedy_vpn_data.sqlite3"
destination = sys.argv[1]
with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
    src.backup(dst)
PY
fi

copy_if_exists /etc/speedy-vpn-backend.env "$stage_dir/etc/speedy-vpn-backend.env"
copy_if_exists /etc/nginx "$stage_dir/etc/nginx"
copy_if_exists /etc/systemd/system/speedy-vpn-backend.service "$stage_dir/etc/systemd/speedy-vpn-backend.service"
copy_if_exists /etc/systemd/system/speedy-vpn-admin.service "$stage_dir/etc/systemd/speedy-vpn-admin.service"
copy_if_exists /etc/systemd/system/telegram-vpn-bot.service "$stage_dir/etc/systemd/telegram-vpn-bot.service"
copy_if_exists /opt/marzban/.env "$stage_dir/marzban/.env"
copy_if_exists /var/lib/marzban "$stage_dir/marzban/data"
copy_if_exists /root/telegram-vpn-bot/.env "$stage_dir/telegram-bot/.env"
copy_if_exists /root/telegram-vpn-bot/.env.marzban "$stage_dir/telegram-bot/.env.marzban"
copy_if_exists /root/awg "$stage_dir/amneziawg"

{
  printf 'created_at_utc=%s\n' "$timestamp"
  printf 'hostname=%s\n' "$(hostname -f 2>/dev/null || hostname)"
  printf 'backend_service=%s\n' "$(systemctl is-active speedy-vpn-backend 2>/dev/null || true)"
  printf 'admin_service=%s\n' "$(systemctl is-active speedy-vpn-admin 2>/dev/null || true)"
  printf 'nginx_service=%s\n' "$(systemctl is-active nginx 2>/dev/null || true)"
  printf 'marzban_container=%s\n' "$(docker inspect -f '{{.State.Status}}' marzban-marzban-1 2>/dev/null || true)"
} > "$stage_dir/MANIFEST.txt"

tar -C "$stage_dir" -czf "$archive_path" .
sha256sum "$archive_path" > "${archive_path}.sha256"
find "$backup_dir" -maxdepth 1 -type f -name 'mefast-*.tar.gz' -mtime "+${retention_days}" -delete
find "$backup_dir" -maxdepth 1 -type f -name 'mefast-*.tar.gz.sha256' -mtime "+${retention_days}" -delete
printf 'Backup created: %s\n' "$archive_path"
