#!/usr/bin/env bash
set -Eeuo pipefail

failed=0

check_service() {
  local unit="$1"
  if systemctl is-active --quiet "$unit"; then
    printf 'OK    systemd %s\n' "$unit"
  else
    printf 'FAIL  systemd %s\n' "$unit" >&2
    failed=1
  fi
}

check_service nginx
check_service speedy-vpn-backend
check_service speedy-vpn-admin
check_service docker

if docker inspect -f '{{.State.Running}}' marzban-marzban-1 2>/dev/null | grep -qx true; then
  printf 'OK    docker marzban-marzban-1\n'
else
  printf 'FAIL  docker marzban-marzban-1\n' >&2
  failed=1
fi

if curl --fail --silent --show-error --max-time 10 https://mevl.me-fast.pro/healthz >/dev/null; then
  printf 'OK    public healthcheck\n'
else
  printf 'FAIL  public healthcheck\n' >&2
  failed=1
fi

if [[ "$(df --output=pcent / | tail -1 | tr -dc '0-9')" -ge 90 ]]; then
  printf 'FAIL  root filesystem is at or above 90%%\n' >&2
  failed=1
else
  printf 'OK    root filesystem capacity\n'
fi

exit "$failed"
