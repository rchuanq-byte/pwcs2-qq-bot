#!/usr/bin/env bash
set -Eeuo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
BIN_DIR="$TMP_DIR/bin"
STATE_DIR="$TMP_DIR/state"
MODE_FILE="$TMP_DIR/mode"
RESTARTS_FILE="$TMP_DIR/restarts"
mkdir -p "$BIN_DIR"
printf 'failed\n' >"$MODE_FILE"
printf '0\n' >"$RESTARTS_FILE"
printf 'recover\n' >"$TMP_DIR/restart-behavior"

cat >"$BIN_DIR/getent" <<'EOF'
#!/usr/bin/env bash
printf '203.0.113.10 STREAM test\n'
EOF
cat >"$BIN_DIR/ip" <<'EOF'
#!/usr/bin/env bash
if [[ "$1 $2" == "route get" ]]; then
  printf '%s via 192.0.2.1 dev iwan0 src 192.0.2.2\n' "$3"
  exit 0
fi
exit 1
EOF
cat >"$BIN_DIR/curl" <<'EOF'
#!/usr/bin/env bash
if [[ "$(<"$TEST_MODE_FILE")" == "healthy" ]]; then
  printf '200'
  exit 0
fi
exit 28
EOF
cat >"$BIN_DIR/systemctl" <<'EOF'
#!/usr/bin/env bash
if [[ "$1 $2" == "restart qqbot-iwan.service" ]]; then
  count="$(<"$TEST_RESTARTS_FILE")"
  printf '%s\n' "$((count + 1))" >"$TEST_RESTARTS_FILE"
  case "$(<"$TEST_RESTART_BEHAVIOR_FILE")" in
    recover)
      printf 'healthy\n' >"$TEST_MODE_FILE"
      exit 0
      ;;
    unhealthy)
      exit 0
      ;;
    fail)
      exit 1
      ;;
  esac
fi
exit 1
EOF
cat >"$BIN_DIR/sleep" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$BIN_DIR/flock" <<'EOF'
#!/usr/bin/env bash
# The production host uses util-linux flock. Sequential behavior is enough for
# this test; the systemd oneshot unit also prevents overlapping timer jobs.
exit 0
EOF
chmod 0755 "$BIN_DIR"/*

export PATH="$BIN_DIR:$PATH"
export TEST_MODE_FILE="$MODE_FILE"
export TEST_RESTARTS_FILE="$RESTARTS_FILE"
export TEST_RESTART_BEHAVIOR_FILE="$TMP_DIR/restart-behavior"
export IWAN_WATCHDOG_STATE_DIR="$STATE_DIR"
export IWAN_WATCHDOG_FAILURE_THRESHOLD=3
export IWAN_WATCHDOG_COOLDOWN_SECONDS=1800
export IWAN_WATCHDOG_RESTART_TIMEOUT_SECONDS=5
export IWAN_PROXY_DOMAINS=api.llm.ustc.edu.cn
export IWAN_HEALTH_URLS=https://api.llm.ustc.edu.cn/v1/models

run_watchdog() {
  bash "$ROOT_DIR/deploy/qqbot-iwan-watchdog"
}

run_watchdog
run_watchdog
[[ "$(<"$RESTARTS_FILE")" == "0" ]]
run_watchdog
[[ "$(<"$RESTARTS_FILE")" == "1" ]]
[[ "$(<"$STATE_DIR/consecutive_failures")" == "0" ]]

# A healthy cycle resets the failure counter.
run_watchdog
[[ "$(<"$STATE_DIR/consecutive_failures")" == "0" ]]

# Three more failures occur inside the 30-minute cooldown and must not trigger
# another restart.
printf 'failed\n' >"$MODE_FILE"
run_watchdog
run_watchdog
run_watchdog
[[ "$(<"$RESTARTS_FILE")" == "1" ]]

# A failed systemd restart retains the threshold. Once the attempt cooldown has
# expired, the very next failed cycle can retry instead of waiting 3 more runs.
export IWAN_WATCHDOG_STATE_DIR="$TMP_DIR/state-restart-failed"
printf 'failed\n' >"$MODE_FILE"
printf 'fail\n' >"$TEST_RESTART_BEHAVIOR_FILE"
printf '0\n' >"$RESTARTS_FILE"
run_watchdog
run_watchdog
if run_watchdog; then
  echo "Expected a failed systemctl restart to fail the watchdog run." >&2
  exit 1
fi
[[ "$(<"$IWAN_WATCHDOG_STATE_DIR/consecutive_failures")" == "3" ]]
if run_watchdog; then
  echo "Expected the next-cycle retry to preserve the systemctl failure." >&2
  exit 1
fi
[[ "$(<"$RESTARTS_FILE")" == "2" ]]

# A restart that returns success but remains unhealthy has the same protected
# retry semantics.
export IWAN_WATCHDOG_STATE_DIR="$TMP_DIR/state-still-unhealthy"
printf 'failed\n' >"$MODE_FILE"
printf 'unhealthy\n' >"$TEST_RESTART_BEHAVIOR_FILE"
printf '0\n' >"$RESTARTS_FILE"
run_watchdog
run_watchdog
if run_watchdog; then
  echo "Expected a failed post-restart health check to fail the watchdog run." >&2
  exit 1
fi
[[ "$(<"$IWAN_WATCHDOG_STATE_DIR/consecutive_failures")" == "3" ]]
[[ -s "$IWAN_WATCHDOG_STATE_DIR/last_attempt" ]]
run_watchdog
[[ "$(<"$RESTARTS_FILE")" == "1" ]]

echo "OK iWAN watchdog threshold, recovery, cooldown, and failed-restart behavior"
