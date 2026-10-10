# Jetson Orin Nano — 판단 모델(d1-3B) llama-server 런북

> **이 문서의 모든 명령은 사용자가 실행한다. 에이전트는 실행하지 않는다 (D-10, CLAUDE.md 서버 작업 원칙).**
> 모델 다운로드·빌드·기동·중지·방화벽 설정 등 Orin 상태를 바꾸는 작업은 전부 사용자 몫이다.

| 구분 | 단계 | 성격 |
|------|------|------|
| 읽기 전용 | 0 (Mac 에서 파일 전달만), 1 | Orin 상태를 바꾸지 않는다 (0단계는 사용자 홈 아래 파일 복사뿐) |
| 상태 변경 | 2 ~ 5, 9, 10 | 모델 다운로드, 이미지 pull·빌드, 컨테이너 기동·중지, 방화벽 |
| 요청·측정 | 6 ~ 8 | 실행 중인 서버에 요청만 보낸다 (스모크, 지연 교차 검증, Mac 접속) |
| 보고 | 11 | 결과를 붙여 넣어 에이전트에게 전달 |

구성 요약:

- 이미지: `wavepilot/llama-server:b11541-r36.4.0` — llama.cpp `b11541` 을 `nvcr.io/nvidia/l4t-jetpack:r36.4.0` 위에서 `CMAKE_CUDA_ARCHITECTURES=87` 로 소스 빌드. 컨테이너에는 `llama-server` 바이너리만 있다.
- 왜 `b11541` 인가: 예전 보고 서버 이미지(llama.cpp `v0.6.0`)는 d1-3B 의 `lfm2-d1` 아키텍처를 읽지 못한다. 지원은 PR #30110 으로 들어왔고 `b11541` 이 확인된 첫 태그다.
- compose 프로젝트: `wavepilot-decision` (llm_report 의 `jetson` 프로젝트와 분리 — 여기서 `down` 해도 그쪽 컨테이너를 건드리지 않는다).
- compose 서비스: `llama-decision`, 포트 `8081`, 모델 `/models/d1-3B-Q4_K_M.gguf` (읽기 전용 볼륨).
- Mac 의 판단 셀렉터는 `D1_SERVER_URL=http://<orin-ip>:8081` 로 이 서버의 `/v1/systemone` 에 붙는다.

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

**기대 결과:** Orin 에 리포 전체가 `~/wavepilot/` 로 복사되고, 이 런북 디렉터리는 `~/wavepilot/modules/decision/deploy/jetson/` 이다. 모델 디렉터리는 `~/wavepilot/models` (= compose 기본값 `../../../../models`).

**실패 시:** ssh 접속 불가면 Orin IP·계정·네트워크를 먼저 확인한다. 다른 경로에 두려면 기동 때 `MODELS_DIR=<절대경로>` 를 지정한다.

---

## 1. 읽기 전용 점검

**목적:** L4T/JetPack 버전, nvidia 런타임, 메모리·디스크를 확인해 `L4T_TAG` 를 정하고, 8081/8080 포트를 아무도 잡고 있지 않은지 본다.

**명령 (Orin):**

```bash
cat /etc/nv_tegra_release
dpkg-query --show nvidia-jetpack
docker info | grep -i runtime
docker compose version
free -h
df -h ~
docker ps
ss -ltn | grep -E ':8081|:8080'
```

**기대 결과:**

- `R36` 계열 (JetPack 6.x). 예: `# R36 (release), REVISION: 4.x` 또는 `5.x`.
- `docker info` 출력의 Runtimes 에 `nvidia` 가 있다.
- `docker compose version` 이 v2 이상.
- 디스크 여유 ≥ 15 GB (베이스 이미지 + 빌드 캐시 + 모델 약 1.7 GB).
- `docker ps` 에 예전 `llama-report` 컨테이너가 없고, `ss` grep 결과가 비어 있다.

**`L4T_TAG` 선택 기준:**

- NGC 의 최신 r36 태그는 `r36.4.0` 이다. 호스트가 JetPack 6.2.x (= L4T r36.5) 여도 같은 r36 주버전이므로 기본값 `r36.4.0` 으로 먼저 시도한다.
- 호스트가 r36.2/r36.3 이고 r36.4.0 베이스에서 CUDA 오류가 나면 `L4T_TAG=r36.3.0` (또는 `r36.2.0`) 으로 맞춰 다시 빌드한다. 이 경우 compose 의 `image:` 이름은 그대로지만 내용은 다른 태그로 빌드된다는 점을 보고에 적는다.

**참고 — 예전 보고 서버:** Nemotron 모델은 사용자가 이미 Orin 에서 지웠다. 예전 `llama-report` 컨테이너가 아직 떠 있으면 중지할지는 사용자 판단이다. 중지한다면 그 컨테이너의 디렉터리(`~/wavepilot/modules/llm_report/deploy/jetson`)에서 `docker compose down` 한다. 디스크가 모자라면 선택적으로 `docker image rm wavepilot/llama-server:v0.6.0-r36.4.0` 으로 예전 이미지를 지운다.

**실패 시:** nvidia 런타임이 없으면 `nvidia-container-toolkit` 설치 상태를 확인하고 여기서 멈춰 결과를 보고한다 (설치는 사용자 판단).

---

## 2. 모델 배치 (상태 변경)

**목적:** d1-3B Q4_K_M GGUF 를 `~/wavepilot/models` 에 둔다.

**명령 (Orin):**

```bash
mkdir -p ~/wavepilot/models
hf download LiquidAI/d1-3B-GGUF d1-3B-Q4_K_M.gguf --local-dir ~/wavepilot/models
ls -l ~/wavepilot/models/d1-3B-Q4_K_M.gguf
sha256sum ~/wavepilot/models/d1-3B-Q4_K_M.gguf
```

Orin 에 `hf` CLI 가 없으면 Mac 에서 받은 파일을 복사한다 (Mac):

```bash
rsync -av --progress <mac-path>/d1-3B-Q4_K_M.gguf <user>@<orin-ip>:~/wavepilot/models/
```

**기대 결과:** `ls -l` 크기가 정확히 `1674456672` 바이트. `sha256sum` 값을 Hugging Face 파일 페이지(`LiquidAI/d1-3B-GGUF` → `d1-3B-Q4_K_M.gguf`)의 SHA256 과 비교하고 기록한다. mmproj 파일은 필요 없다 (텍스트만 보낸다).

**실패 시:** 크기·해시가 다르면 파일을 지우고 다시 받는다. 디스크 부족이면 1단계 `df -h` 결과와 함께 보고. GGUF 는 git 에 넣지 않는다 (`.gitignore` 의 `*.gguf`, `models/`).

---

## 3. 베이스 이미지 확인 (상태 변경: 이미지 pull)

**목적:** 베이스 이미지에 nvcc 가 들어 있는지 확인한다. 예전 보고 서버 빌드 때 `r36.4.0` 베이스에서 이미 통과했다면 건너뛴다.

**명령 (Orin):**

```bash
docker run --rm --runtime nvidia nvcr.io/nvidia/l4t-jetpack:r36.4.0 nvcc --version
```

**기대 결과:** `Cuda compilation tools, release 12.x` 출력.

**실패 시:** `nvcc: not found` 이면 **여기서 중단**하고 결과를 보고한다.

---

## 4. 빌드 (상태 변경)

**목적:** llama.cpp `b11541` 의 `llama-server` 를 SM87 로 빌드해 `wavepilot/llama-server:b11541-r36.4.0` 이미지를 만든다.

**명령 (Orin):**

```bash
cd ~/wavepilot/modules/decision/deploy/jetson
time docker compose build llama-decision
```

메모리 부족(OOM)으로 죽거나 멈추면 병렬도를 낮춰 재시도:

```bash
BUILD_JOBS=2 docker compose build llama-decision
```

빌드 결과 확인:

```bash
docker compose run --rm --no-deps llama-decision --version
```

**기대 결과:** 빌드는 수십 분 걸릴 수 있다. `--version` 이 b11541 무렵의 빌드 번호와 CUDA 디바이스(Orin)를 출력한다.

**실패 시:**

- 빌드 로그 마지막 50줄을 첨부해 보고한다.
- `b11541` 자체가 빌드되지 않으면 더 새 b-태그로 재시도한다:

  ```bash
  LLAMA_CPP_REF=<newer-b-tag> docker compose build llama-decision
  ```

  이때 이미지 이름은 `b11541-r36.4.0` 그대로지만 내용은 다른 태그로 빌드된다. 사용한 태그를 보고에 반드시 적는다.
- `--version` 실행 시 공유 라이브러리(`.so`)를 못 찾는다는 오류 → 정적 빌드(`BUILD_SHARED_LIBS=OFF`) 가정 실패. 로그 첨부.

---

## 5. 기동 (상태 변경)

**목적:** `llama-decision` 컨테이너를 띄우고 `/health` 200, `lfm2-d1` 로딩 로그, `/v1/models` 의 decisions 출력을 확인한다.

**명령 (Orin, `~/wavepilot/modules/decision/deploy/jetson` 에서):**

```bash
docker compose up -d llama-decision
docker compose ps
docker compose logs --tail 80 llama-decision | grep -E 'decision model type|error'
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8081/health
curl -s http://localhost:8081/v1/models
```

**기대 결과:**

- 로그에 `decision model type: lfm2-d1` 줄이 있다.
- 모델 로딩 중에는 `/health` 가 `503`, 준비되면 `200` (`{"status":"ok"}`). `docker compose ps` 상태가 `healthy` 가 될 때까지 기다린다 (start_period 120s).
- `/v1/models` 응답의 output_modalities 에 `"decisions"` 가 있다.

**실패 시:**

- `decision model type` 줄이 없거나 6단계에서 `/v1/systemone` 이 501 → llama.cpp 버전이 판단 모델을 모른다 (RESEARCH Pitfall 1). 4단계의 `LLAMA_CPP_REF` 재빌드로 간다.
- `CUDA error: out of memory` → `free -h` 와 로그를 보고.
- 모델 파일을 못 찾음 → `~/wavepilot/models/d1-3B-Q4_K_M.gguf` 경로 또는 `MODELS_DIR` 확인.

---

## 6. systemone 스모크 요청

**목적:** `/v1/systemone` 이 choice 질문에 choice / probabilities / confidence / usage 로 답하는지 확인한다.

요청 본문을 임시 파일로 만든다 (Orin). `state` 는 압축 JSON **문자열**이고 합성 값이다. criteria 문구는 예시일 뿐이며, 실제 정의의 원본은 코드 상수 `RULE_MODE_DEFINITIONS` (01-01) 다.

```bash
cat > /tmp/d1_smoke.json <<'EOF'
{
  "state": "{\"schema_version\":2,\"drone\":{\"battery_pct\":78.0,\"altitude_m\":40.0},\"tracks\":[{\"track_id\":\"T1\",\"class_name\":\"person\",\"confidence\":0.82,\"distance_m\":65.0}],\"mission\":{\"target_class\":\"person\"}}",
  "questions": {
    "rule_mode": {
      "type": "choice",
      "instructions": "Choose the next rule_mode for the drone.",
      "criteria": {
        "search": "No confirmed target; sweep the area to find one.",
        "approach": "A target is detected but far; move closer to confirm it.",
        "observe": "A target is confirmed and close enough; keep it in view.",
        "hold": "Stay in place because the situation is unclear or unsafe to move.",
        "return_home": "Battery is low or the mission is over; fly back to home."
      }
    }
  }
}
EOF
```

요청 (두 번 보낸다 — 첫 번째는 워밍업):

```bash
curl -s http://localhost:8081/v1/systemone -H "Content-Type: application/json" -d @/tmp/d1_smoke.json
echo
curl -s http://localhost:8081/v1/systemone -H "Content-Type: application/json" -d @/tmp/d1_smoke.json
echo
```

메모리 확인:

```bash
free -h
timeout 5 tegrastats
```

**기대 결과:**

- `answers.rule_mode.choice` 가 보낸 키(`search`, `approach`, `observe`, `hold`, `return_home`) 중 하나다.
- `answers.rule_mode.probabilities` 의 합이 약 1, `confidence` 가 0 ~ 1 사이.
- `usage.input_tokens` 가 있다.
- `timings` 는 **없다** — 정상이다 (RESEARCH 2.3). 그래서 7단계에서 지연을 바깥에서 잰다.
- probabilities 가 전부 비슷한 값(예: 모두 약 0.2)으로 퇴화했는지 눈여겨보고 보고에 적는다 (RESEARCH A8, Q4_K_M 양자화 위험).

**실패 시:** 501 이면 5단계 실패 시 항목으로. 400 이면 본문 JSON 형식을 확인하고 전체 응답을 보고.

---

## 7. 지연 교차 검증 (Orin localhost vs Mac)

**목적:** Mac 셀렉터가 쓰는 지연 분리 방식이 맞는지 실측으로 확인한다. 서버가 처리 시간을 돌려주지 않으므로 셀렉터는

- `network_rtt_ms` = `GET /health` 왕복 시간
- `orin_processing_ms` = max(0, `total_ms` − `network_rtt_ms`)

로 추정한다. 이 추정이 Orin 내부에서 잰 처리 시간과 맞는지 본다.

**명령 (Orin, 6단계의 `/tmp/d1_smoke.json` 사용):**

```bash
curl -s -o /dev/null http://localhost:8081/v1/systemone -H "Content-Type: application/json" -d @/tmp/d1_smoke.json

echo "== orin health =="
for i in $(seq 10); do curl -s -o /dev/null -w '%{time_connect} %{time_starttransfer} %{time_total}\n' http://localhost:8081/health; done

echo "== orin systemone =="
for i in $(seq 10); do curl -s -o /dev/null -w '%{time_connect} %{time_starttransfer} %{time_total}\n' http://localhost:8081/v1/systemone -H "Content-Type: application/json" -d @/tmp/d1_smoke.json; done
```

**명령 (Mac, 같은 본문 파일을 가져와서):**

```bash
scp <user>@<orin-ip>:/tmp/d1_smoke.json /tmp/
curl -s -o /dev/null http://<orin-ip>:8081/v1/systemone -H "Content-Type: application/json" -d @/tmp/d1_smoke.json

echo "== mac health =="
for i in $(seq 10); do curl -s -o /dev/null -w '%{time_connect} %{time_starttransfer} %{time_total}\n' http://<orin-ip>:8081/health; done

echo "== mac systemone =="
for i in $(seq 10); do curl -s -o /dev/null -w '%{time_connect} %{time_starttransfer} %{time_total}\n' http://<orin-ip>:8081/v1/systemone -H "Content-Type: application/json" -d @/tmp/d1_smoke.json; done
```

**읽는 법:**

- 각 줄은 `time_connect time_starttransfer time_total` 이고 단위는 **초**다 (×1000 하면 ms).
- Orin 의 systemone `time_total` ≈ Orin 처리 시간 (네트워크가 거의 0).
- Mac 의 health `time_total` ≈ `network_rtt_ms`.
- 확인할 식: **Mac systemone `time_total` − Mac health `time_total` ≈ Orin systemone `time_total`**.
  몇 ms 안에서 맞으면 `/health` 왕복으로 나누는 방식이 유효하다. 차이가 크면 추정이 치우친 것이므로 그대로 보고한다.
- curl 은 매번 새 TCP 연결을 연다 (셀렉터의 urllib 클라이언트와 같다). 그래서 연결 비용이 health 와 systemone 양쪽에 똑같이 들어가 뺄셈에서 상쇄된다.

**실패 시:** Mac 쪽 명령이 실패하면 8단계(접속)부터 확인한다.

---

## 8. Mac 에서 접속

**목적:** Mac 의 판단 셀렉터가 Orin 의 서버에 붙을 수 있는지 확인한다.

**명령 (Mac):**

```bash
curl -s -o /dev/null -w "%{http_code}\n" http://<orin-ip>:8081/health
```

**기대 결과:** `200`. Mac 셀렉터는 `D1_SERVER_URL=http://<orin-ip>:8081` 로 이 서버를 쓴다.

01-04 / 01-05 계획이 들어온 뒤라면 선택적으로 live 테스트:

```bash
cd WavePilot && D1_SERVER_URL=http://<orin-ip>:8081 uv run pytest -m d1
```

**실패 시:** Orin 내부 `/health` 는 200 인데 Mac 에서 실패하면 방화벽(ufw)·바인딩 주소(`LLAMA_BIND_ADDR`)·네트워크를 확인한다.

---

## 9. 보안 (상태 변경: 방화벽·설정)

**목적:** 인증 없는 포트를 LAN 에 여는 위험을 줄인다.

- 기본 바인딩은 `0.0.0.0:8081` 이고 **인증이 없다** (프로젝트 결정: 인증 없음). 신뢰할 수 있는 LAN 에서만 기동한다.
- 특정 인터페이스로 좁히기:

  ```bash
  LLAMA_BIND_ADDR=<orin-lan-ip> docker compose up -d llama-decision
  ```

- 방화벽으로 Mac 만 허용 (예시):

  ```bash
  sudo ufw allow from <mac-ip> to any port 8081 proto tcp
  ```

---

## 10. 중지·롤백 (상태 변경)

**명령 (Orin, `~/wavepilot/modules/decision/deploy/jetson` 에서):**

```bash
docker compose down
docker image rm wavepilot/llama-server:b11541-r36.4.0
docker builder prune
```

`docker compose down` 은 프로젝트 `wavepilot-decision` 의 컨테이너만 내린다.

다른 llama.cpp 태그로 재빌드:

```bash
LLAMA_CPP_REF=<tag> docker compose build llama-decision
```

---

## 11. 결과 보고 양식

아래를 채워 붙여 넣는다 (성공이면 맨 앞에 `orin d1 ok`):

```text
L4T / JetPack 버전 (cat /etc/nv_tegra_release, dpkg-query):
사용한 llama.cpp ref (기본 b11541 / 바꿨다면 그 태그):
빌드 성공 여부 / 소요 시간 / BUILD_JOBS / L4T_TAG:
llama-server --version 출력:
decision model type 로그 줄:
/health 코드 — Orin 내부:
/health 코드 — Mac 에서:
/v1/models 에 "decisions" 있음 (yes/no):
모델 파일 크기 / sha256sum (HF 와 일치 여부):
스모크 응답 JSON (두 번째 호출):
probabilities 퇴화 여부 (모두 비슷한가):
7단계 원본 출력 — orin health 10줄:
7단계 원본 출력 — orin systemone 10줄:
7단계 원본 출력 — mac health 10줄:
7단계 원본 출력 — mac systemone 10줄:
free -h (기동 후):
실패 시 해당 단계 + 로그 마지막 50줄:
```
