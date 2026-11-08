#!/usr/bin/env bash
set -Eeuo pipefail

SWAPPINESS="${QQBOT_SWAPPINESS:-20}"
SYSCTL_CONF="/etc/sysctl.conf"
SYSCTL_TARGET="/etc/sysctl.d/zz-qqbot-memory.conf"
JOURNAL_DIR="/etc/systemd/journald.conf.d"
JOURNAL_TARGET="$JOURNAL_DIR/99-qqbot-limits.conf"

if [[ "${EUID}" -ne 0 ]]; then
  echo "Please run with sudo: sudo bash deploy/harden_server_memory.sh" >&2
  exit 1
fi
if ! [[ "$SWAPPINESS" =~ ^[0-9]+$ ]] || ((SWAPPINESS < 1 || SWAPPINESS > 100)); then
  echo "QQBOT_SWAPPINESS must be an integer from 1 to 100." >&2
  exit 2
fi

for command_name in awk cp install mktemp mv sysctl systemctl; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "Required command is missing: $command_name" >&2
    exit 2
  }
done

install -d -m 0755 /etc/sysctl.d "$JOURNAL_DIR" /var/backups/qqbot-memory
backup_dir="/var/backups/qqbot-memory/$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 0700 "$backup_dir"
old_swappiness="$(sysctl -n vm.swappiness)"

had_sysctl_conf=0
had_sysctl_target=0
had_journal_target=0
[[ -e "$SYSCTL_CONF" || -L "$SYSCTL_CONF" ]] && had_sysctl_conf=1
[[ -e "$SYSCTL_TARGET" || -L "$SYSCTL_TARGET" ]] && had_sysctl_target=1
[[ -e "$JOURNAL_TARGET" || -L "$JOURNAL_TARGET" ]] && had_journal_target=1

for target in "$SYSCTL_CONF" "$SYSCTL_TARGET" "$JOURNAL_TARGET"; do
  if [[ -L "$target" ]]; then
    echo "Refusing to replace symbolic link without an explicit migration: $target" >&2
    exit 2
  fi
done

((had_sysctl_conf == 0)) || cp -a -- "$SYSCTL_CONF" "$backup_dir/sysctl.conf"
((had_sysctl_target == 0)) || cp -a -- "$SYSCTL_TARGET" "$backup_dir/zz-qqbot-memory.conf"
((had_journal_target == 0)) || cp -a -- "$JOURNAL_TARGET" "$backup_dir/99-qqbot-limits.conf"

sysctl_conf_temp="$(mktemp /etc/.sysctl.conf.qqbot.XXXXXX)"
sysctl_target_temp="$(mktemp /etc/sysctl.d/.zz-qqbot-memory.XXXXXX)"
journal_temp="$(mktemp "$JOURNAL_DIR/.99-qqbot-limits.XXXXXX")"

rollback_file() {
  local existed="$1" backup="$2" target="$3"
  rm -f -- "$target"
  if ((existed)); then
    cp -a -- "$backup" "$target"
  fi
}

cleanup() {
  local status="$?"
  trap - EXIT
  set +e
  rm -f -- "$sysctl_conf_temp" "$sysctl_target_temp" "$journal_temp"
  if ((status != 0)); then
    echo "Memory hardening failed; restoring this run's configuration backups." >&2
    rollback_file "$had_sysctl_conf" "$backup_dir/sysctl.conf" "$SYSCTL_CONF"
    rollback_file "$had_sysctl_target" "$backup_dir/zz-qqbot-memory.conf" "$SYSCTL_TARGET"
    rollback_file "$had_journal_target" "$backup_dir/99-qqbot-limits.conf" "$JOURNAL_TARGET"
    sysctl -w "vm.swappiness=$old_swappiness" >/dev/null || true
    systemctl try-restart systemd-journald.service >/dev/null 2>&1 || true
  fi
  exit "$status"
}
trap cleanup EXIT

if ((had_sysctl_conf)); then
  awk '!/^[[:space:]]*vm\.swappiness[[:space:]]*=/' "$SYSCTL_CONF" >"$sysctl_conf_temp"
fi
cat >>"$sysctl_conf_temp" <<EOF

# Managed by pwcs2-qq-bot/deploy/harden_server_memory.sh.
vm.swappiness = $SWAPPINESS
EOF

cat >"$sysctl_target_temp" <<EOF
# Keep a small amount of anonymous memory swappable so a configured swap file
# can absorb cold pages instead of repeatedly evicting and rereading file cache.
vm.swappiness = $SWAPPINESS
EOF

cat >"$journal_temp" <<'EOF'
[Journal]
SystemMaxUse=256M
RuntimeMaxUse=64M
SystemKeepFree=1G
EOF

chmod 0644 "$sysctl_conf_temp" "$sysctl_target_temp" "$journal_temp"
mv -fT "$sysctl_conf_temp" "$SYSCTL_CONF"
mv -fT "$sysctl_target_temp" "$SYSCTL_TARGET"
mv -fT "$journal_temp" "$JOURNAL_TARGET"

sysctl -w "vm.swappiness=$SWAPPINESS"
systemctl try-restart systemd-journald.service

actual="$(sysctl -n vm.swappiness)"
if [[ "$actual" != "$SWAPPINESS" ]]; then
  echo "Failed to apply vm.swappiness=$SWAPPINESS (current: $actual)." >&2
  exit 1
fi

echo "OK vm.swappiness=$actual; journal persistent/runtime caps are 256M/64M"
echo "Backups for this run: $backup_dir"
