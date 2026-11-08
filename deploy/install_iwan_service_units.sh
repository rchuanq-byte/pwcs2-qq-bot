#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE_PATH="/etc/systemd/system/qqbot-iwan.service"
START_PATH="/usr/local/sbin/qqbot-iwan-start"
WATCHDOG_PATH="/usr/local/sbin/qqbot-iwan-watchdog"
WATCHDOG_SERVICE_PATH="/etc/systemd/system/qqbot-iwan-watchdog.service"
WATCHDOG_TIMER_PATH="/etc/systemd/system/qqbot-iwan-watchdog.timer"
LEGACY_SERVICE_DROP_IN="/etc/systemd/system/qqbot-iwan.service.d/mtu.conf"
LEGACY_WATCHDOG_DROP_IN="/etc/systemd/system/qqbot-iwan-watchdog.service.d/health.conf"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run with sudo: sudo bash deploy/install_iwan_service_units.sh" >&2
  exit 1
fi

for command_name in cp install mktemp mv systemctl systemd-analyze; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 2
  }
done
for source_file in \
  "$ROOT_DIR/deploy/qqbot-iwan-start" \
  "$ROOT_DIR/deploy/qqbot-iwan-watchdog"; do
  test -f "$source_file" || { echo "Missing deployment file: $source_file" >&2; exit 1; }
done

WORK_DIR="$(mktemp -d /run/qqbot-iwan-units.XXXXXX)"
BACKUP_DIR="/var/backups/qqbot-iwan-units/$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 0700 "$BACKUP_DIR"
changed=0

backup_file() {
  local source="$1" key="$2"
  if [[ -e "$source" || -L "$source" ]]; then
    cp -a -- "$source" "$BACKUP_DIR/$key"
    printf '1\n'
  else
    printf '0\n'
  fi
}

restore_file() {
  local existed="$1" key="$2" target="$3"
  rm -f -- "$target"
  if ((existed)); then
    cp -a -- "$BACKUP_DIR/$key" "$target"
  fi
}

atomic_install() {
  local mode="$1" source="$2" target="$3" temp
  temp="$(mktemp "$(dirname "$target")/.$(basename "$target").XXXXXX")"
  install -m "$mode" "$source" "$temp"
  mv -fT "$temp" "$target"
}

had_start="$(backup_file "$START_PATH" start)"
had_watchdog="$(backup_file "$WATCHDOG_PATH" watchdog)"
had_service="$(backup_file "$SERVICE_PATH" service)"
had_watchdog_service="$(backup_file "$WATCHDOG_SERVICE_PATH" watchdog-service)"
had_watchdog_timer="$(backup_file "$WATCHDOG_TIMER_PATH" watchdog-timer)"
had_legacy_service="$(backup_file "$LEGACY_SERVICE_DROP_IN" legacy-service)"
had_legacy_watchdog="$(backup_file "$LEGACY_WATCHDOG_DROP_IN" legacy-watchdog)"

rollback() {
  set +e
  echo "iWAN unit installation failed; restoring this run's backups." >&2
  restore_file "$had_start" start "$START_PATH"
  restore_file "$had_watchdog" watchdog "$WATCHDOG_PATH"
  restore_file "$had_service" service "$SERVICE_PATH"
  restore_file "$had_watchdog_service" watchdog-service "$WATCHDOG_SERVICE_PATH"
  restore_file "$had_watchdog_timer" watchdog-timer "$WATCHDOG_TIMER_PATH"
  restore_file "$had_legacy_service" legacy-service "$LEGACY_SERVICE_DROP_IN"
  restore_file "$had_legacy_watchdog" legacy-watchdog "$LEGACY_WATCHDOG_DROP_IN"
  systemctl daemon-reload || true
}

cleanup() {
  local status="$?"
  trap - EXIT
  if ((status != 0 && changed)); then
    rollback
  fi
  case "$WORK_DIR" in
    /run/qqbot-iwan-units.*) rm -rf -- "$WORK_DIR" ;;
  esac
  exit "$status"
}
trap cleanup EXIT

cat >"$WORK_DIR/qqbot-iwan.service" <<'EOF'
[Unit]
Description=QQ Bot USTC iWAN tunnel
Wants=network-online.target
After=network-online.target
StartLimitIntervalSec=1800
StartLimitBurst=5

[Service]
Type=simple
EnvironmentFile=-/etc/qqbot-iwan.env
ExecStartPre=/bin/sh -c 'test -r "${IWAN_CONFIG_DIR:-/home/admin/.config/iwan}/servers.json"'
ExecStart=/usr/local/sbin/qqbot-iwan-start
ExecStartPost=/bin/sh -c 'count=0; until ip link show "${IWAN_TUN:-iwan0}" >/dev/null 2>&1; do count=$((count + 1)); test "$count" -lt 50 || exit 1; sleep 0.1; done; sleep 1; ip link set dev "${IWAN_TUN:-iwan0}" mtu "${IWAN_MTU:-1280}"'
ExecStopPost=-/bin/sh -c 'ip link delete "${IWAN_TUN:-iwan0}" 2>/dev/null || true'
Restart=always
RestartSec=60
TimeoutStartSec=30
TimeoutStopSec=30
KillMode=control-group

[Install]
WantedBy=multi-user.target
EOF

cat >"$WORK_DIR/qqbot-iwan-watchdog.service" <<'EOF'
[Unit]
Description=Check and recover QQ Bot USTC iWAN tunnel
After=qqbot-iwan.service

[Service]
Type=oneshot
EnvironmentFile=-/etc/qqbot-iwan.env
ExecStart=/usr/local/sbin/qqbot-iwan-watchdog
TimeoutStartSec=180
EOF

cat >"$WORK_DIR/qqbot-iwan-watchdog.timer" <<'EOF'
[Unit]
Description=Run QQ Bot USTC iWAN watchdog with restart protection

[Timer]
OnBootSec=5min
OnUnitInactiveSec=5min
AccuracySec=30s
RandomizedDelaySec=30s
Unit=qqbot-iwan-watchdog.service

[Install]
WantedBy=timers.target
EOF

changed=1
atomic_install 0755 "$ROOT_DIR/deploy/qqbot-iwan-start" "$START_PATH"
atomic_install 0755 "$ROOT_DIR/deploy/qqbot-iwan-watchdog" "$WATCHDOG_PATH"
systemd-analyze verify \
  "$WORK_DIR/qqbot-iwan.service" \
  "$WORK_DIR/qqbot-iwan-watchdog.service" \
  "$WORK_DIR/qqbot-iwan-watchdog.timer"
atomic_install 0644 "$WORK_DIR/qqbot-iwan.service" "$SERVICE_PATH"
atomic_install 0644 "$WORK_DIR/qqbot-iwan-watchdog.service" "$WATCHDOG_SERVICE_PATH"
atomic_install 0644 "$WORK_DIR/qqbot-iwan-watchdog.timer" "$WATCHDOG_TIMER_PATH"

# The generated base units contain the current settings. Remove old overrides
# only after the replacement units have passed systemd's verifier.
rm -f "$LEGACY_SERVICE_DROP_IN" "$LEGACY_WATCHDOG_DROP_IN"
systemctl daemon-reload
systemctl reset-failed qqbot-iwan.service || true
systemctl enable qqbot-iwan.service
systemctl enable --now qqbot-iwan-watchdog.timer
systemctl restart qqbot-iwan-watchdog.timer

changed=0
echo "OK atomically installed rate-limited iWAN service and watchdog units"
echo "Backups for this run: $BACKUP_DIR"
