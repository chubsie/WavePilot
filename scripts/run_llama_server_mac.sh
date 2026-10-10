#!/usr/bin/env bash
# Mac 로컬 llama-server 기동 (AI-SPEC §3 플래그 그대로). 첫 실행 때 GGUF 2.84 GB를 Hugging Face에서 받는다
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 환경변수로 덮어쓸 수 있다. 포트는 llama-server 기본값(master 9931)에 기대지 않고 항상 명시한다 (AI-SPEC Pitfall 6)
LLAMA_HOST="${LLAMA_HOST:-127.0.0.1}"
LLAMA_PORT="${LLAMA_PORT:-8080}"
LLAMA_HF_MODEL="${LLAMA_HF_MODEL:-nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF:Q4_K_M}"
LLAMA_ALIAS="${LLAMA_ALIAS:-nemotron-3-nano-4b}"
LLAMA_CTX_SIZE="${LLAMA_CTX_SIZE:-4096}"

SERVER_CMD=(
  llama-server
  -hf "$LLAMA_HF_MODEL"
  --alias "$LLAMA_ALIAS"
  --host "$LLAMA_HOST"
  --port "$LLAMA_PORT"
  -c "$LLAMA_CTX_SIZE"
  -np 1
  -ngl 999
  --jinja
  --chat-template-kwargs '{"enable_thinking": false}'
)

# --print-command 는 점검 없이 실행될 명령만 한 줄로 보여 준다 (테스트·문서용). 그 밖의 인자는 그대로 덧붙인다
if [ "${1:-}" = "--print-command" ]; then
  printf '%q ' "${SERVER_CMD[@]}"
  echo
  exit 0
fi
if [ $# -gt 0 ]; then
  SERVER_CMD+=("$@")
fi

if [ "$(uname -s)" != "Darwin" ]; then
  echo "this launcher is for macOS; on Jetson use the runbook in deploy/jetson/" >&2
  exit 1
fi
if ! command -v llama-server > /dev/null; then
  echo "llama-server not found. Install on macOS: brew install llama.cpp" >&2
  exit 1
fi
if [ "$LLAMA_HOST" != "127.0.0.1" ]; then
  echo "WARNING: binding to $LLAMA_HOST — unauthenticated port exposed on LAN" >&2
fi

llama-server --version >&2 || true

echo "health: http://$LLAMA_HOST:$LLAMA_PORT/health (503 while loading, 200 when ready)" >&2
echo "smoke test (another terminal): uv run python scripts/smoke_llama_server.py" >&2

# exec 로 이 셸을 서버 프로세스로 바꾸므로 남는 자식 프로세스가 없다 — 그래서 trap 이 필요 없다
exec "${SERVER_CMD[@]}"
