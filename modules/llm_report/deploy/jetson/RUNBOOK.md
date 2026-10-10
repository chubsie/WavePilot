# Jetson Orin Nano — 보고 LLM llama-server 런북 (D-08)

> **이 문서의 모든 명령은 사용자가 실행한다. 에이전트는 실행하지 않는다 (D-09).**
> 빌드·모델 배치·기동·중지·방화벽 설정 등 Orin 상태를 바꾸는 작업은 전부 사용자 몫이다.

| 구분 | 단계 | 성격 |
|------|------|------|
| 읽기 전용 | 0 (Mac 에서 파일 전달만), 1 | Orin 상태를 바꾸지 않는다 (0단계는 사용자 홈 아래 파일 복사뿐) |
| 상태 변경 | 2 ~ 9 | 모델 다운로드, 이미지 빌드, 컨테이너 기동·중지, 방화벽 |
| 보고 | 10 | 결과를 붙여 넣어 에이전트에게 전달 |

구성 요약:

- 이미지: `wavepilot/llama-server:v0.6.0-r36.4.0` — llama.cpp `v0.6.0` 을 `nvcr.io/nvidia/l4t-jetpack:r36.4.0` 위에서 `CMAKE_CUDA_ARCHITECTURES=87` 로 소스 빌드. 컨테이너에는 `llama-server` 바이너리만 있다.
- compose 서비스: `llama-report`, 포트 `8080`, 모델 `/models/NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf` (읽기 전용 볼륨).
- Mac 의 `modules.llm_report` 은 그대로 Mac 에 두고 URL 만 `http://<orin-ip>:8080` 으로 바꿔 붙는다.

아래에서 `<user>`, `<orin-ip>`, `<mac-ip>`, `<orin-lan-ip>` 는 실제 값으로 바꾼다.

---

## 0. Mac → Orin 파일 전달

**목적:** 리포 전체(코드·테스트·이 런북)를 Orin 의 `~/wavepilot/` 로 복사한다. git push 가 아니라 파일 복사다.

**명령 (Mac, WavePilot 상위 디렉터리에서):**

```bash
cd WavePilot
rsync -av --exclude .venv --exclude .git --exclude graft --exclude __pycache__ --exclude .pytest_cache ./ <user>@<orin-ip>:~/wavepilot/
ssh <user>@<orin-ip> 'mkdir -p ~/wavepilot/models'
```

**기대 결과:** Orin 에 리포 전체가 `~/wavepilot/` 로 복사되고, 이 런북 디렉터리는 `~/wavepilot/modules/llm_report/deploy/jetson/` 이다. 모델 디렉터리는 `~/wavepilot/models` (= compose 기본값 `../../../../models`).

**실패 시:** ssh 접속 불가면 Orin IP·계정·네트워크를 먼저 확인한다. 다른 경로에 두려면 기동 때 `MODELS_DIR=<절대경로>` 를 지정한다.

---

## 1. 읽기 전용 점검

**목적:** L4T/JetPack 버전, nvidia 런타임, 메모리·디스크를 확인해 `L4T_TAG` 를 정한다.

**명령 (Orin):**

```bash
cat /etc/nv_tegra_release
dpkg-query --show nvidia-jetpack
docker info | grep -i runtime
docker compose version
free -h
df -h ~
```

**기대 결과:**

- `R36` 계열 (JetPack 6.x). 예: `# R36 (release), REVISION: 4.x` 또는 `5.x`.
- `docker info` 출력의 Runtimes 에 `nvidia` 가 있다.
- `docker compose version` 이 v2 이상.
- 디스크 여유 ≥ 15 GB (베이스 이미지 + 빌드 캐시 + 모델 2.84 GB).

**`L4T_TAG` 선택 기준:**

- NGC 의 최신 r36 태그는 `r36.4.0` 이다. 호스트가 JetPack 6.2.x (= L4T r36.5) 여도 같은 r36 주버전이므로 기본값 `r36.4.0` 으로 먼저 시도한다.
- 호스트가 r36.2/r36.3 이고 r36.4.0 베이스에서 CUDA 오류가 나면 `L4T_TAG=r36.3.0` (또는 `r36.2.0`) 으로 맞춰 다시 빌드한다. 이 경우 compose 의 `image:` 이름은 그대로지만 내용은 다른 태그로 빌드된다는 점을 보고에 적는다.

**실패 시:** nvidia 런타임이 없으면 `nvidia-container-toolkit` 설치 상태를 확인하고 여기서 멈춰 결과를 보고한다 (설치는 사용자 판단).

---

## 2. 모델 배치 (상태 변경)

**목적:** Nemotron-3-Nano-4B Q4_K_M GGUF 를 `~/wavepilot/models` 에 둔다.

**명령 (Orin):**

```bash
mkdir -p ~/wavepilot/models
hf download nvidia/NVIDIA-Nemotron-3-Nano-4B-GGUF NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf --local-dir ~/wavepilot/models
sha256sum ~/wavepilot/models/NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf
```

Orin 에 `hf` CLI 가 없으면 Mac 에서 받은 파일을 복사한다 (Mac):

```bash
rsync -av --progress <mac-path>/NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf <user>@<orin-ip>:~/wavepilot/models/
```

**기대 결과:** 약 2.84 GB 파일. `sha256sum` 값을 기록해 둔다 (Hugging Face 파일 페이지의 SHA256 과 비교).

**실패 시:** 디스크 부족이면 1단계 `df -h` 결과와 함께 보고. GGUF 는 git 에 넣지 않는다 (`.gitignore` 의 `*.gguf`, `models/`).

---

## 3. 베이스 이미지 확인 (상태 변경: 이미지 pull)

**목적:** 베이스 이미지에 nvcc 가 들어 있는지 확인한다 (RESEARCH A7 가정 검증).

**명령 (Orin):**

```bash
docker run --rm --runtime nvidia nvcr.io/nvidia/l4t-jetpack:r36.4.0 nvcc --version
```

**기대 결과:** `Cuda compilation tools, release 12.x` 출력.

**실패 시:** `nvcc: not found` 이면 **여기서 중단**하고 결과를 보고한다. 대체 베이스(devel 계열 CUDA 이미지)는 다음 계획에서 결정한다.

---

## 4. 빌드 (상태 변경)

**목적:** llama.cpp `v0.6.0` 의 `llama-server` 를 SM87 로 빌드해 `wavepilot/llama-server:v0.6.0-r36.4.0` 이미지를 만든다.

**명령 (Orin):**

```bash
cd ~/wavepilot/modules/llm_report/deploy/jetson
time docker compose build llama-report
```

메모리 부족(OOM)으로 죽거나 멈추면 병렬도를 낮춰 재시도:

```bash
BUILD_JOBS=2 docker compose build llama-report
```

빌드 결과 확인:

```bash
docker compose run --rm --no-deps llama-report --version
docker compose run --rm --no-deps llama-report --help | grep -E -- '--chat-template-kwargs|--jinja|--no-webui'
```

**기대 결과:** 빌드는 수십 분 걸릴 수 있다. `--version` 이 `v0.6.0` 계열 버전과 CUDA 디바이스(Orin)를 출력. `--help` grep 에 `--chat-template-kwargs`, `--jinja` 가 반드시 나온다. `--no-webui` 는 있으면 8단계에서 선택적으로 쓴다.

**실패 시:**

- cmake 구성 오류(예: curl 관련 옵션), 링크 오류(정적 빌드 `BUILD_SHARED_LIBS=OFF` 가정 실패, RESEARCH A8) → 빌드 로그 마지막 50줄을 첨부해 보고.
- `--version` 실행 시 공유 라이브러리(`.so`)를 못 찾는다는 오류 → 정적 빌드 가정 실패. 로그 첨부.

---

## 5. 기동 (상태 변경)

**목적:** `llama-report` 컨테이너를 띄우고 `/health` 200 을 확인한다.

**명령 (Orin, `~/wavepilot/modules/llm_report/deploy/jetson` 에서):**

```bash
docker compose up -d llama-report
docker compose ps
docker compose logs --tail 50 llama-report
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/health
```

**기대 결과:** 모델 로딩 중에는 `/health` 가 `503`, 준비되면 `200` (`{"status":"ok"}`). `docker compose ps` 의 상태가 `healthy` 가 될 때까지 기다린다 (start_period 120s).

**실패 시:**

- 로그에 `unknown model architecture` → llama.cpp 버전 문제(RESEARCH Pitfall 9). `LLAMA_CPP_REF` 를 더 새 태그로 바꿔 재빌드(9단계)하거나 대안 A 시도.
- `CUDA error: out of memory` → 컨텍스트/메모리 문제(Pitfall 8). `free -h` 와 로그를 보고.
- 모델 파일을 못 찾음 → `~/wavepilot/models/NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf` 경로 또는 `MODELS_DIR` 확인.

---

## 6. 스모크 요청

**목적:** 스키마 강제 JSON 출력, thinking 비활성, `timings` 를 확인한다.

**명령 (Orin):**

```bash
curl -s http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "nemotron-3-nano-4b",
    "messages": [
      {"role": "system", "content": "Answer in JSON only."},
      {"role": "user", "content": "Track T1 is a vehicle moving north. Summarize in one sentence citing T1."}
    ],
    "response_format": {
      "type": "json_schema",
      "json_schema": {
        "name": "smoke",
        "schema": {
          "type": "object",
          "properties": {"sentence": {"type": "string"}, "track_ids": {"type": "array", "items": {"type": "string"}}},
          "required": ["sentence", "track_ids"]
        }
      }
    },
    "chat_template_kwargs": {"enable_thinking": false},
    "temperature": 0.0,
    "max_tokens": 64
  }'
```

메모리 확인:

```bash
free -h
timeout 5 tegrastats
```

**기대 결과:** `choices[0].message.content` 가 스키마에 맞는 JSON 문자열, `<think>` 없음, 응답에 `timings` 객체가 있다.

**실패 시:** `content` 가 비었거나 `<think>` 가 섞이면 RESEARCH Pitfall 5 신호. 전체 응답을 보고.

---

## 7. Mac 에서 접속

**목적:** Mac 의 `modules.llm_report` 이 같은 클라이언트로 Orin 의 llama-server 에 붙을 수 있는지 확인한다.

**명령 (Mac):**

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://<orin-ip>:8080/health
```

**기대 결과:** `200`. Mac 의 `modules.llm_report` 은 `backend_url="http://<orin-ip>:8080"` 로 바꾸기만 하면 된다 (기본값 `http://127.0.0.1:8080` 과 포트 동일, 호스트만 다름). 설정 하나로 전환하는 기능 자체는 Phase 3 RPT-04 범위다.

live 테스트 파일이 있으면 선택적으로:

```bash
cd WavePilot && LLAMA_SERVER_URL=http://<orin-ip>:8080 uv run pytest -m llm
```

**실패 시:** Orin 내부 `/health` 는 200 인데 Mac 에서 실패하면 방화벽(ufw)·바인딩 주소(`LLAMA_BIND_ADDR`)·네트워크를 확인한다.

---

## 8. 보안 (상태 변경: 방화벽·설정)

**목적:** 인증 없는 포트를 LAN 에 여는 위험을 줄인다.

- 기본 바인딩은 `0.0.0.0:8080` 이고 **인증이 없다**. 신뢰할 수 있는 LAN 에서만 기동한다.
- 특정 인터페이스로 좁히기:

  ```bash
  LLAMA_BIND_ADDR=<orin-lan-ip> docker compose up -d llama-report
  ```

- 방화벽으로 Mac 만 허용 (예시):

  ```bash
  sudo ufw allow from <mac-ip> to any port 8080 proto tcp
  ```

---

## 9. 중지·롤백 (상태 변경)

**명령 (Orin, `~/wavepilot/modules/llm_report/deploy/jetson` 에서):**

```bash
docker compose down
docker image rm wavepilot/llama-server:v0.6.0-r36.4.0
docker builder prune
```

다른 llama.cpp 태그로 재빌드:

```bash
LLAMA_CPP_REF=<tag> docker compose build llama-report
```

**대안 A — NVIDIA 사전 빌드 이미지로 스모크:** 소스 빌드가 막히면 `ghcr.io/nvidia-ai-iot/llama_cpp:<tag>-jetson-orin` 을 compose 와 같은 플래그로 띄워 본다. Nemotron 지원 여부는 미확인이며, 태그는 사용자가 확인한다.

```bash
docker run --rm --runtime nvidia -p 8080:8080 -v ~/wavepilot/models:/models:ro \
  ghcr.io/nvidia-ai-iot/llama_cpp:<tag>-jetson-orin \
  llama-server -m /models/NVIDIA-Nemotron3-Nano-4B-Q4_K_M.gguf --alias nemotron-3-nano-4b \
  --host 0.0.0.0 --port 8080 -c 4096 -np 1 -ngl 999 --jinja \
  --chat-template-kwargs '{"enable_thinking": false}'
```

---

## 10. 결과 보고 양식

아래를 채워 붙여 넣는다 (성공이면 맨 앞에 `orin ok`):

```text
L4T / JetPack 버전 (cat /etc/nv_tegra_release, dpkg-query):
nvcc --version (3단계):
빌드 성공 여부 / 소요 시간 / BUILD_JOBS / L4T_TAG:
llama-server --version 출력:
--help grep 결과 (--chat-template-kwargs / --jinja / --no-webui):
/health 코드 — Orin 내부:
/health 코드 — Mac 에서:
스모크 응답 content 앞부분:
스모크 응답 timings:
free -h (기동 후):
실패 시 해당 단계 + 로그 마지막 50줄:
```

---

참고: 전력 모드·클럭 고정(`nvpmodel`, `jetson_clocks`)과 지연 측정은 Phase 5 범위다.
