#!/usr/bin/env bash
# 개발 환경 셋업: uv sync + llama-server 설치 여부 확인
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

uv sync

# 설치는 사용자 결정이라 안내만 한다 — 이 스크립트는 brew 를 실행하지 않는다
if command -v llama-server > /dev/null; then
  llama-server --version
else
  echo "llama-server not found. Install on macOS: brew install llama.cpp"
fi

echo "setup done. next steps:"
echo "  scripts/run_demo.sh --backend fake           # demo without an LLM server"
echo "  modules/llm_report/scripts/run_llama_server_mac.sh              # start local llama-server (first run downloads the model)"
echo "  scripts/run_demo.sh --backend llama_server   # demo with the real LLM"
