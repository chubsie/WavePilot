#!/usr/bin/env bash
# 운용자 콘솔과 드론을 2프로세스로 띄우는 데모. 사용: scripts/run_demo.sh [--backend fake|llama_server] ...
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

BACKEND=fake
BACKEND_URL="${LLAMA_SERVER_URL:-http://127.0.0.1:8080}"
SCENARIO=scenarios/checkpoint.toml
SPEED=1.0
PORT=0
HOST=127.0.0.1
LISTEN_TIMEOUT_SEC=10
OPERATOR_EXIT_TIMEOUT_SEC=15

usage() {
  cat <<USAGE
usage: scripts/run_demo.sh [--backend fake|llama_server] [--backend-url URL]
                           [--scenario PATH] [--speed S] [--port P]

  --backend       report LLM backend (default: fake)
  --backend-url   llama-server base URL (default: \$LLAMA_SERVER_URL or http://127.0.0.1:8080)
  --scenario      scenario TOML file (default: scenarios/checkpoint.toml)
  --speed         sim seconds per wall second; 0 = as fast as possible (default: 1.0)
  --port          operator console port; 0 picks a free port (default: 0)
USAGE
}

# 인자 파싱 — 값은 모두 큰따옴표로만 전개하고 eval 하지 않는다 (T-01-09-02)
while [ $# -gt 0 ]; do
  case "$1" in
    --backend) BACKEND="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
    --backend-url) BACKEND_URL="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
    --scenario) SCENARIO="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
    --speed) SPEED="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
    --port) PORT="${2:-}"; shift 2 || { usage >&2; exit 2; } ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

case "$BACKEND" in
  fake|llama_server) ;;
  *) echo "invalid --backend: '$BACKEND'" >&2; usage >&2; exit 2 ;;
esac

# 사전 점검 — 자식 프로세스를 띄우기 전에 끝낸다
PYTHON="$ROOT/.venv/bin/python"
if [ ! -x "$PYTHON" ]; then
  echo "python venv not found at $PYTHON — run scripts/setup.sh first" >&2
  exit 1
fi
if [ ! -f "$SCENARIO" ]; then
  echo "scenario file not found: $SCENARIO" >&2
  exit 1
fi
if [ "$BACKEND" = "llama_server" ] && ! curl -sf -m 2 "$BACKEND_URL/health" > /dev/null; then
  echo "llama-server not ready at $BACKEND_URL" >&2
  echo "start it with: scripts/run_llama_server_mac.sh" >&2
  exit 1
fi

# 정리 — 어떤 종료 경로(정상·오류·Ctrl-C)에서도 자식 프로세스와 임시 로그를 남기지 않는다
WORK_DIR="$(mktemp -d)"
OPERATOR_PID=""
TAIL_PID=""
DRONE_PID=""

cleanup() {
  for pid in "$DRONE_PID" "$OPERATOR_PID" "$TAIL_PID"; do
    if [ -n "$pid" ] && kill -0 "$pid" 2> /dev/null; then
      kill "$pid" 2> /dev/null || true
      wait "$pid" 2> /dev/null || true
    fi
  done
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

# 운용자 콘솔 기동. 포트 열림을 TCP 접속으로 확인하면 운용자가 그 접속을 드론으로 받고
# --exit-on-disconnect 로 끝나 버리므로, 로그의 LISTEN 줄로만 판단한다
OPERATOR_LOG="$WORK_DIR/operator.log"
PYTHONUNBUFFERED=1 "$PYTHON" -m operator_console --host "$HOST" --port "$PORT" --exit-on-disconnect \
  < /dev/null > "$OPERATOR_LOG" 2>&1 &
OPERATOR_PID=$!
tail -n +1 -f "$OPERATOR_LOG" &
TAIL_PID=$!

# LISTEN 줄에서 실제 포트를 얻는다 (--port 0 이면 OS 가 고른 포트)
ACTUAL_PORT=""
LISTEN_POLL_COUNT=$((LISTEN_TIMEOUT_SEC * 5))
for _ in $(seq "$LISTEN_POLL_COUNT"); do
  ACTUAL_PORT="$(sed -n 's/.*LISTEN host=[^ ]* port=\([0-9][0-9]*\).*/\1/p' "$OPERATOR_LOG" | head -n 1)"
  if [ -n "$ACTUAL_PORT" ] || ! kill -0 "$OPERATOR_PID" 2> /dev/null; then
    break
  fi
  sleep 0.2
done
if [ -z "$ACTUAL_PORT" ]; then
  echo "operator console did not print LISTEN within ${LISTEN_TIMEOUT_SEC} s; log tail:" >&2
  tail -n 20 "$OPERATOR_LOG" >&2 || true
  exit 1
fi

# 드론은 백그라운드로 띄우고 wait 한다 — 전경 실행이면 bash 가 드론이 끝날 때까지 INT/TERM trap 을
# 미루기 때문이다. 종료 코드는 set -e 에 걸리지 않게 보관한다
DRONE_ARGS=(
  --scenario "$SCENARIO" --operator-host "$HOST" --operator-port "$ACTUAL_PORT"
  --backend "$BACKEND" --speed "$SPEED"
)
if [ "$BACKEND" = "llama_server" ]; then
  DRONE_ARGS+=(--llm-url "$BACKEND_URL")
fi
DRONE_STATUS=0
"$PYTHON" -m drone.run_scenario "${DRONE_ARGS[@]}" &
DRONE_PID=$!
wait "$DRONE_PID" || DRONE_STATUS=$?
DRONE_PID=""

# 운용자는 드론 연결이 끊기면 스스로 끝난다. 시간 안에 안 끝나면 cleanup 이 kill 한다
EXIT_POLL_COUNT=$((OPERATOR_EXIT_TIMEOUT_SEC * 5))
for _ in $(seq "$EXIT_POLL_COUNT"); do
  if ! kill -0 "$OPERATOR_PID" 2> /dev/null; then
    break
  fi
  sleep 0.2
done
sleep 0.3  # tail 이 운용자의 마지막 줄(LINK disconnected)까지 화면에 내보낼 여유

exit "$DRONE_STATUS"
