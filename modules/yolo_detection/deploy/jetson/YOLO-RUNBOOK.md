# Jetson Orin Nano — YOLO(yolo11n) TensorRT 탐지·측정 런북 (DET-02)

> **이 문서의 모든 명령은 사용자가 실행한다. 에이전트는 실행하지 않는다.**
> 컨테이너 기동·엔진 export·전력 모드 변경·llama-server 중지/기동 등 Orin 상태를 바꾸는 작업은 전부 사용자 몫이다.

| 구분 | 단계 | 성격 |
|------|------|------|
| 읽기 전용 | 1, 3 | Orin 상태를 바꾸지 않는다 (점검만) |
| 상태 변경 | 0, 2, 4 ~ 12 | 파일 복사, 이미지 pull, 컨테이너·엔진·전력 모드, 측정 실행과 로그 생성 |
| 보고 | 13 | 결과를 붙여 넣어 에이전트에게 전달 |
| 실시간 서버 (Phase 7) | R0 ~ R8, 보고 양식 | 맨 아래 "실시간 YOLO 서버 (Phase 7)" 절의 표에서 단계별로 구분한다 |

측정 실행과 로그 생성 단계(9 ~ 11)도 디스크와 프로세스를 바꾸므로 상태 변경으로 분류한다.

구성 요약:

- 이미지: `ultralytics/ultralytics:latest-jetson-jetpack6` (L4T R35 이면 `ultralytics/ultralytics:latest-jetson-jetpack5`).
- 컨테이너: 이름 `yolo-bench`, `--runtime nvidia --ipc=host`, 마운트 `-v ~/wavepilot:/workspace/wavepilot -w /workspace/wavepilot -e PYTHONPATH=.`.
- 어댑터는 Mac 과 같은 `YoloDetectionSource` 이고, 가중치 경로만 `models/yolo11n.engine` 으로 바뀐다.
- 선행 조건: `modules/llm_report/deploy/jetson/RUNBOOK.md` 0 ~ 5단계로 llama-report 를 한 번 띄워 본 상태 (시나리오 B·C 에 필요. 아니면 시나리오 A 만 가능).
- 엔진은 이 컨테이너 이미지의 TensorRT 에 묶인다. 이미지를 바꾸면 6단계부터 다시 한다.

아래에서 `<user>`, `<orin-ip>`, `<mac-path>` 는 실제 값으로 바꾼다.

---

## 0. Mac → Orin 코드·가중치 전달

**목적:** 어댑터 코드와 CLI, (있으면) 가중치를 Orin 의 `~/wavepilot` 로 복사한다. git push 가 아니라 파일 복사다.

**명령 (Mac, WavePilot-yolo 디렉터리에서):**

```bash
ssh <user>@<orin-ip> 'mkdir -p ~/wavepilot/models ~/wavepilot/logs'
rsync -avR modules/__init__.py modules/yolo_detection <user>@<orin-ip>:~/wavepilot/
rsync -av models/yolo11n.pt <user>@<orin-ip>:~/wavepilot/models/
```

`-R` 는 상대 경로를 유지한다. Phase 1 의 `deploy/jetson` 다른 파일을 덮지 않도록 필요한 경로만 보낸다. 세 번째 줄은 Mac 에 `models/yolo11n.pt` 가 있을 때만 실행한다.

**기대 결과:** Orin 에 `~/wavepilot/modules/yolo_detection/` (interface·어댑터·`run_yolo.py`·런북) 가 생긴다.

**실패 시:** ssh 불가면 IP·계정·네트워크를 확인한다. 가중치가 Mac 에 없으면 4단계에서 Orin 이 직접 받는다.

---

## 1. 읽기 전용 점검 (A7)

**목적:** L4T/JetPack 버전, nvidia 런타임, 메모리·디스크를 확인해 이미지 태그를 정하고, 현재 전력 모드를 기록한다 (12단계 복구용).

**명령 (Orin):**

```bash
cat /etc/nv_tegra_release
dpkg-query --show nvidia-jetpack
docker info | grep -i runtime
free -h
df -h ~
sudo nvpmodel -q
cd ~/wavepilot/modules/llm_report/deploy/jetson && docker compose ps
```

**기대 결과:** `R36` 계열 = JetPack 6.x 이므로 `ultralytics/ultralytics:latest-jetson-jetpack6` 를 쓴다. Runtimes 에 `nvidia` 가 있다. 디스크 여유 10 GB 이상. `nvpmodel -q` 의 모드 번호를 적어 둔다.

**실패 시:** R35 (JetPack 5.x) 이면 이후 모든 이미지 태그를 `ultralytics/ultralytics:latest-jetson-jetpack5` 로 바꾸고 보고에 적는다. nvidia 런타임이 없으면 여기서 멈추고 보고한다. 디스크 여유가 10 GB 미만이면 보고한다.

---

## 2. 컨테이너 기동 (상태 변경)

**목적:** ultralytics 공식 Jetson 이미지를 받고, 이름 붙은 장기 실행 컨테이너를 만든다. `--rm` 이 아닌 이름 붙은 컨테이너를 쓰는 이유는 3단계의 lapx 설치가 측정 내내 유지되고, `docker exec` 로 단계별 실행을 하기 위해서다.

**명령 (Orin):**

```bash
docker pull ultralytics/ultralytics:latest-jetson-jetpack6
docker image inspect --format '{{index .RepoDigests 0}}' ultralytics/ultralytics:latest-jetson-jetpack6
docker run -d --name yolo-bench --runtime nvidia --ipc=host -v ~/wavepilot:/workspace/wavepilot -w /workspace/wavepilot -e PYTHONPATH=. ultralytics/ultralytics:latest-jetson-jetpack6 sleep infinity
```

**기대 결과:** `docker ps` 에 `yolo-bench` 가 Up 이다. digest 를 보고에 적는다.

**실패 시:** 이름 충돌이면 `docker rm -f yolo-bench` 후 다시 실행한다. pull 실패면 네트워크·디스크를 확인하고 보고한다.

---

## 3. 컨테이너 내부 점검 (A1·A2, 읽기 전용)

**목적:** 컨테이너 Python 버전, 라이브러리 버전, 추적기 의존성 `lap`, 마운트된 repo 를 확인한다.

**명령 (Orin, 컨테이너):**

```bash
docker exec yolo-bench python3 --version
docker exec yolo-bench python -c "import ultralytics, tensorrt, torch; print(ultralytics.__version__, tensorrt.__version__, torch.__version__, torch.cuda.is_available())"
docker exec yolo-bench python -c "import lap; print('lap ok')"
docker exec yolo-bench python -c "import yolo_detection, modules.yolo_detection.interface.detection; print('repo ok')"
```

**기대 결과:** Python 3.10.x (A2), 마지막 값 `True` (CUDA 가용), `lap ok` (A1), `repo ok`.

**실패 시:**

- `import lap` 실패: https://pypi.org/project/lapx/ 에서 패키지를 직접 확인한 뒤 `docker exec yolo-bench pip install lapx` 를 실행하고 `import lap` 을 다시 확인한다 (lapx 는 같은 `lap` 모듈명을 제공). 보고에 "lapx 사용" 을 적는다.
- Python 이 3.10 보다 낮으면 멈추고 보고한다.
- `torch.cuda.is_available()` 가 False 면 `--runtime nvidia` 누락을 확인한다.
- `repo ok` 실패면 0단계 rsync 경로와 `-w` 를 확인한다.

---

## 4. 가중치 배치 (상태 변경)

**목적:** `models/yolo11n.pt` 를 공식 release 에서 받거나 0단계 결과를 확인하고, 해시를 기록한다.

**명령 (Orin):**

```bash
ls -la ~/wavepilot/models/yolo11n.pt
curl -L -o ~/wavepilot/models/yolo11n.pt https://github.com/ultralytics/assets/releases/download/v8.3.0/yolo11n.pt
sha256sum ~/wavepilot/models/yolo11n.pt
```

두 번째 줄 `curl` 은 첫 줄에서 파일이 없을 때만 실행한다. Mac 의 `shasum -a 256 models/yolo11n.pt` 와 비교할 수 있다.

**보안:** `.pt` 는 pickle 이라 로딩 시 코드 실행이 가능하다. 공식 Ultralytics GitHub release (키 불필요) 에서 받은 파일만 쓰고, git 에 넣지 않는다 (`.gitignore` 의 `*.pt`, `models/`).

**기대 결과:** `yolo11n.pt` 가 약 5 MB 로 존재하고 sha256 이 기록된다.

**실패 시:** 404 면 release 태그가 바뀐 것이다. Mac 에서 받은 파일을 0단계 방식으로 복사한다.

---

## 5. 벤치 입력 프레임 준비 (상태 변경: 디스크)

**목적:** 같은 입력으로 세 시나리오를 재기 위한 `bench_frames/` 를 만든다.

**명령 (Mac, 시뮬레이터 녹화 프레임 1280x720 jpg/png 가 있을 때):**

```bash
rsync -av <mac-path>/frames/ <user>@<orin-ip>:~/wavepilot/bench_frames/
```

**명령 (Orin, 컨테이너, 녹화 프레임이 없을 때의 대체 — ultralytics 내장 bus.jpg 를 300장 복사):**

```bash
docker exec yolo-bench python -c "import shutil, pathlib; from ultralytics.utils import ASSETS; d = pathlib.Path('bench_frames'); d.mkdir(exist_ok=True); [shutil.copy(ASSETS / 'bus.jpg', d / f'{i:04d}.jpg') for i in range(300)]"
```

**기대 결과:** `ls ~/wavepilot/bench_frames | wc -l` 이 100 이상. 보고에 입력 출처·해상도·장수를 적는다. bus.jpg 는 1080x810 정지 영상이라 처리량 측정용이며 추적 이동 검증은 아니다.

**실패 시:** 디스크 부족과 권한 (컨테이너가 root 로 파일을 만든다) 을 확인한다.

---

## 6. TensorRT 엔진 export (상태 변경, llama-server 중지)

**목적:** `yolo11n.pt` 를 이 기기의 TensorRT 엔진으로 변환한다. 첫 export 는 수 분 걸리고 8 GB 공유 메모리를 압박하므로 llama-server 를 내린 채로 한다.

**명령 (Orin, 컨테이너):**

```bash
cd ~/wavepilot/modules/llm_report/deploy/jetson && docker compose stop llama-report
free -h
time docker exec yolo-bench yolo export model=models/yolo11n.pt format=engine half=True imgsz=640 device=0
ls -la ~/wavepilot/models/yolo11n.engine
```

`half` deprecation 경고 (최신 ultralytics 는 `quantize` 로 매핑) 는 무시해도 된다. 이미지 버전에 따라 `quantize` 를 모를 수 있어 `half=True` 를 쓴다. 엔진은 이 기기·이 이미지의 TensorRT 에서만 유효하므로 복사하거나 커밋하지 않는다. llama-report 재기동은 시나리오 B 에서 한다.

**기대 결과:** `models/yolo11n.engine` 이 생성된다. 소요 시간과 파일 크기를 기록한다.

**실패 시:** OOM·프로세스 kill 이면 `free -h` 와 로그 마지막 50줄을 보고하고, 재시도 때 `workspace=2` 를 덧붙여 TensorRT 작업 메모리를 줄여 본다. `onnx`/`tensorrt` import 오류면 이미지 태그·digest 와 함께 보고한다.

---

## 7. 전력 모드·클럭 고정 (상태 변경)

**목적:** 세 시나리오를 같은 조건으로 비교하기 위해 전력 모드와 클럭을 고정한다.

**명령 (Orin):**

```bash
sudo jetson_clocks --store ~/wavepilot/logs/jetson_clocks_before.conf
sudo nvpmodel -m 0
sudo nvpmodel -q
sudo jetson_clocks
sudo jetson_clocks --show
```

모드 0 의 이름 (예: MAXN / 15W) 은 보드·JetPack 마다 다르니 `nvpmodel -q` 출력을 그대로 기록한다.

**기대 결과:** `nvpmodel -q` 가 모드 0 을 보이고, `--show` 의 클럭이 최대로 고정된다.

**실패 시:** 모드 전환이 재부팅을 요구하면 재부팅 여부는 사용자가 판단한다. 재부팅하면 2단계 컨테이너를 `docker start yolo-bench` 로 다시 켜고 7단계를 다시 한다. 바꾸지 못하면 현재 모드로 측정하고 보고에 적는다.

---

## 8. 어댑터 스모크 (A3·A1 실제 확인)

**목적:** Mac 과 같은 `YoloDetectionSource` 가 엔진 가중치로 돌고 추적 ID 가 붙는지 확인한다.

**명령 (Orin, 컨테이너):**

```bash
docker exec yolo-bench python modules/yolo_detection/run_yolo.py --frames bench_frames --weights models/yolo11n.engine --device 0 --output logs/smoke_frames.jsonl
head -n 2 ~/wavepilot/logs/smoke_frames.jsonl
grep -c '"track_id": [0-9]' ~/wavepilot/logs/smoke_frames.jsonl
```

**기대 결과:** 각 줄이 Frame JSON (frame_id, timestamp_sec, image_width, image_height, detections) 이고, 추적 ID 가 붙은 줄 수가 0 보다 크다.

**실패 시:**

- `--device 0` 관련 오류 (A3): 같은 명령에서 `--device 0` 을 빼고 다시 실행한다. 이후 9 ~ 11단계도 `--device` 없이 쓰고, 보고에 "A3: device 생략" 을 적는다.
- `ModuleNotFoundError: lap`: 3단계 lapx 대체를 따른다.
- 엔진 로드 오류 (task 추론 실패 등): 로그 마지막 50줄을 보고한다.

---

## 9. 시나리오 A — YOLO 단독 (llama-report 중지 상태 유지)

**목적:** 탐지만 돌릴 때의 FPS·지연·메모리를 잰다.

**명령 (Orin, 같은 셸에서 순서대로):**

```bash
tegrastats --interval 1000 > ~/wavepilot/logs/tegrastats_A.log & TEGRA_PID=$!
docker exec yolo-bench python modules/yolo_detection/run_yolo.py --frames bench_frames --weights models/yolo11n.engine --device 0 --bench --warmup 10 | tee ~/wavepilot/logs/bench_A.json
docker stats --no-stream | tee ~/wavepilot/logs/docker_stats_A.txt
free -h | tee ~/wavepilot/logs/free_A.txt
kill $TEGRA_PID
```

tegrastats 요약 (최대 RAM 사용량, 최대 GPU 사용률):

```bash
grep -o 'RAM [0-9]*/[0-9]*MB' ~/wavepilot/logs/tegrastats_A.log | sort -t' ' -k2 -n | tail -n 1
grep -o 'GR3D_FREQ [0-9]*%' ~/wavepilot/logs/tegrastats_A.log | sort -t' ' -k2 -n | tail -n 1
```

`free -h` 도 함께 남기는 이유는 tegrastats RAM 이 통합 메모리 전체를 반영한다는 가정 (A6) 을 교차 확인하기 위해서다.

선택: 같은 bench 명령을 `--weights models/yolo11n.pt` 로 돌려 `bench_A_pt.json` 으로 남기면 엔진 대비 PyTorch 비교가 된다.

**기대 결과:** `--bench` 출력은 JSON 한 줄이며 키는 `weights`, `device`, `frames_measured`, `warmup`, `fps_mean`, `latency_ms_mean`, `latency_ms_p95`, `image_width`, `image_height` 9개다.

**실패 시:** `tegrastats` 권한 오류면 `sudo tegrastats ...` 로 실행한다. bench 출력이 여러 줄이거나 JSON 이 아니면 그대로 보고한다 (CLI 계약 위반, 01.1-02 gap).

---

## 10. 시나리오 B — llama-server 만 (YOLO 미실행)

**목적:** 보고 LLM 만 올렸을 때의 메모리를 잰다. yolo-bench 컨테이너는 `sleep` 으로 대기 중이다.

**명령 (Orin):**

```bash
cd ~/wavepilot/modules/llm_report/deploy/jetson && docker compose up -d llama-report
until [ "$(curl -s -o /dev/null -w '%{http_code}' http://localhost:8080/health)" = "200" ]; do sleep 5; done
timeout 30 tegrastats --interval 1000 > ~/wavepilot/logs/tegrastats_B.log
docker stats --no-stream | tee ~/wavepilot/logs/docker_stats_B.txt
free -h | tee ~/wavepilot/logs/free_B.txt
```

**기대 결과:** `/health` 가 200 을 돌려주고, 로그 3종이 생긴다. 9단계의 tegrastats 요약 명령을 `_B` 로그에 적용한다.

**실패 시:** 200 이 안 나오면 `docker compose logs --tail 50 llama-report` 를 보고한다 (llama 쪽 문제는 `modules/llm_report/deploy/jetson/RUNBOOK.md` 5단계 참고). 시나리오 A 결과만이라도 13단계로 보고한다.

---

## 11. 시나리오 C — YOLO + llama-server 동시 (LLM 요청 부하)

**목적:** 탐지와 보고 LLM 이 8 GB 공유 메모리에 함께 들어가는지, LLM 요청이 오가는 동안 FPS 가 얼마나 나오는지 잰다. llama-report 는 10단계에서 띄운 채로 유지한다.

**명령 (Orin, 같은 셸에서 순서대로):**

```bash
while true; do curl -s http://localhost:8080/v1/chat/completions -H "Content-Type: application/json" -d '{"model": "nemotron-3-nano-4b", "messages": [{"role": "user", "content": "Summarize: track T1 vehicle moving north."}], "chat_template_kwargs": {"enable_thinking": false}, "max_tokens": 64}' >> ~/wavepilot/logs/llm_load_C.log; done & LOAD_PID=$!
tegrastats --interval 1000 > ~/wavepilot/logs/tegrastats_C.log & TEGRA_PID=$!
docker exec yolo-bench python modules/yolo_detection/run_yolo.py --frames bench_frames --weights models/yolo11n.engine --device 0 --bench --warmup 10 | tee ~/wavepilot/logs/bench_C.json
docker stats --no-stream | tee ~/wavepilot/logs/docker_stats_C.txt
free -h | tee ~/wavepilot/logs/free_C.txt
kill $LOAD_PID $TEGRA_PID
grep -c '"choices"' ~/wavepilot/logs/llm_load_C.log
```

마지막 줄은 측정 중 LLM 응답 수다. 9단계의 tegrastats 요약 명령을 `_C` 로그에 적용한다.

**기대 결과:** `bench_C.json` 이 JSON 한 줄이고, LLM 응답 수가 1 이상이다.

**실패 시:** llama-report 가 exited 면 `docker compose ps` 와 `sudo dmesg | grep -i -E 'oom|killed' | tail -n 20` (읽기 전용) 결과를 보고한다. "동시 기동 시 OOM" 자체가 측정 결과이므로 고치지 말고 기록한다. bench 가 멈추면 `docker exec yolo-bench pkill -f run_yolo.py` 후 보고한다.

---

## 12. 정리·복구 (상태 변경)

**목적:** 컨테이너를 지우고 전력 모드·클럭을 원래대로 돌린다.

**명령 (Orin):**

```bash
docker rm -f yolo-bench
sudo jetson_clocks --restore ~/wavepilot/logs/jetson_clocks_before.conf
sudo nvpmodel -m <1단계에서 기록한 모드 번호>
cd ~/wavepilot/modules/llm_report/deploy/jetson && docker compose stop llama-report
```

마지막 줄은 llama-report 를 더 쓰지 않을 때만 실행한다. 엔진·`bench_frames`·`logs` 는 남겨 두어도 되며, 지우려면 `rm -rf ~/wavepilot/bench_frames ~/wavepilot/models/yolo11n.engine` 를 쓴다.

**기대 결과:** `docker ps -a` 에 `yolo-bench` 가 없고, `sudo nvpmodel -q` 가 1단계에서 기록한 모드다.

**실패 시:** `--restore` 파일이 없으면 재부팅으로 기본 클럭으로 돌아간다.

---

## 13. 결과 보고 양식

맨 앞에 `orin yolo ok` (또는 실패한 단계 번호) 를 적고, 아래 항목을 붙여 넣는다.

```text
orin yolo ok
L4T / JetPack: (1단계 cat /etc/nv_tegra_release, nvidia-jetpack)
nvpmodel 측정 모드: (7단계 nvpmodel -q)
Docker 이미지 태그 / digest: (2단계)
python3 --version: (3단계)
ultralytics / TensorRT / torch 버전, cuda 가용: (3단계)
lap 또는 lapx: (3단계)
yolo11n.pt sha256: (4단계)
엔진 export: 성공 여부 / 소요 시간 / yolo11n.engine 크기 (6단계)
A3 결과: --device 0 사용 가능 / 생략 (8단계)
스모크 track_id 줄 수: (8단계)
벤치 입력: 출처 / 해상도 / 장수 (5단계)
시나리오 A bench_A.json 한 줄 전체:
시나리오 C bench_C.json 한 줄 전체:
시나리오별 tegrastats 최대 RAM used/total MB 와 GR3D_FREQ 최대 % (A / B / C):
시나리오별 free -h Mem used/total (A / B / C):
시나리오별 docker stats MEM USAGE, yolo-bench / llama-report (A / B / C):
시나리오 C LLM 응답 수:
실패 시: 해당 단계 번호 + 로그 마지막 50줄
```

fps_mean, latency_ms_p95 는 bench JSON 값을 그대로 붙인다. 에이전트가 `.planning/phases/01.1-yolo-detectionsource/01.1-JETSON-RESULTS.md` 에 옮긴다.

참고: 0 ~ 13단계는 Orin 단독 측정까지만 한다. Mac 시뮬레이터가 Orin 의 YOLO 서버로 프레임을 보내는 라이브 연동은 아래 "실시간 YOLO 서버 (Phase 7)" 절에서 한다.

---

## 실시간 YOLO 서버 (Phase 7)

> **이 절의 모든 명령도 사용자가 실행한다. 에이전트는 실행하지 않는다.**
> Orin 에서 YOLO 를 HTTP 서버(`yolo-server`, 포트 8082)로 띄우고, Mac 의 시뮬레이터·실행기가 프레임을 보내 탐지 결과를 받는다.
> Mac 쪽 전환은 `--yolo orin --yolo-url http://<orin-ip>:8082` 플래그뿐이다 (`--yolo local` 이 기본값).

| 구분 | 단계 | 성격 |
|------|------|------|
| 읽기 전용 | R1, R5 | 엔진 파일·서버 상태 확인만 한다 |
| 상태 변경 | R0, R2, R3, R4, R6, R7, R8 | 파일 복사, 컨테이너 기동·정지, 방화벽, 서버 트래커 상태, 실행 로그 |
| 보고 | 보고 양식 | 결과를 붙여 넣어 에이전트에게 전달 |

구성 요약:

- compose: 이 디렉터리의 `compose.yaml` — 프로젝트 이름 `wavepilot-yolo`, 서비스 `yolo-server`, 이미지 `ultralytics/ultralytics:latest-jetson-jetpack6`.
- 서버 명령 (compose 가 실행): `python -m modules.yolo_detection.remote.server --weights models/yolo11n.engine --host 0.0.0.0 --port 8082 --device 0 --imgsz 640`. `lap` 이 없으면 기동 전에 lapx 를 설치한다 (3단계와 같은 대체).
- 인증 없음 (해커톤 정책): 바인딩 주소 `YOLO_BIND_ADDR` 와 ufw 로만 접근을 좁힌다.
- 엔진은 compose 와 같은 이미지 태그에서 export 한 것만 쓴다. 이미지를 바꾸면 6단계부터 다시 한다.

아래에서 `<user>`, `<orin-ip>`, `<mac-ip>`, `<orin-lan-ip>`, `<port>`, `<dir>` 는 실제 값으로 바꾼다.

### R0. 코드 전달 (상태 변경, Mac)

**목적:** 서버 모듈(`modules/yolo_detection/remote/`)과 이 compose 파일을 Orin 의 `~/wavepilot` 로 복사한다. git push 가 아니라 파일 복사다.

**명령 (Mac, WavePilot 디렉터리에서):**

```bash
rsync -avR modules/__init__.py modules/yolo_detection <user>@<orin-ip>:~/wavepilot/
```

디렉터리가 아직 없으면 0단계 첫 줄 (`mkdir -p ~/wavepilot/models ~/wavepilot/logs`) 을 먼저 한다.

**기대 결과:** Orin 에 `~/wavepilot/modules/yolo_detection/remote/` 와 `~/wavepilot/modules/yolo_detection/deploy/jetson/compose.yaml` 이 생긴다.

**실패 시:** ssh 불가면 IP·계정·네트워크를 확인한다 (0단계와 같다).

### R1. 엔진 확인 (읽기 전용, Orin)

**목적:** 서버가 읽을 `models/yolo11n.engine` 이 있는지 확인한다.

**명령 (Orin):**

```bash
ls -la ~/wavepilot/models/yolo11n.engine
docker image inspect --format '{{index .RepoDigests 0}}' ultralytics/ultralytics:latest-jetson-jetpack6
```

**기대 결과:** 엔진 파일이 있고, 이미지 digest 가 엔진을 export 할 때 (2단계 보고) 와 같다.

**실패 시:** 엔진이 없거나 digest 가 다르면 2 ~ 6단계 (컨테이너, lapx, 가중치, export) 를 한 뒤 `docker rm -f yolo-bench` 로 측정 컨테이너를 지우고 R2 로 간다.

### R2. 메모리 확보 (상태 변경, Orin)

**목적:** 8 GB 공유 메모리에서 서버가 엔진을 올릴 자리를 만든다.

**명령 (Orin):**

```bash
free -h
docker ps
docker rm -f yolo-bench
cd ~/wavepilot/modules/llm_report/deploy/jetson && docker compose stop llama-report
cd ~/wavepilot/modules/decision/deploy/jetson && docker compose stop llama-decision
```

세 번째 줄부터는 해당 컨테이너가 떠 있고 메모리가 빠듯하며, 지금 그 서버를 쓰지 않을 때만 실행한다. 무엇을 멈췄는지 적어 둔다 (R8 에서 되돌린다).

**기대 결과:** `free -h` 의 available 이 2 GB 이상이다.

**실패 시:** 여유가 부족한 채로 R3 을 해도 되지만, OOM 이면 보고에 `free -h` 와 함께 적는다.

### R3. 서버 기동 (상태 변경, Orin)

**목적:** compose 로 `yolo-server` 를 띄운다.

**명령 (Orin):**

```bash
cd ~/wavepilot/modules/yolo_detection/deploy/jetson && docker compose up -d yolo-server
docker compose ps
```

한 인터페이스에만 열려면 `YOLO_BIND_ADDR=<orin-lan-ip> docker compose up -d yolo-server` 로 띄운다. 모델 로드·워밍업이 끝나야 리슨하므로 `docker compose ps` 가 `healthy` 가 될 때까지 (최대 수 분) 다시 본다.

**기대 결과:** `yolo-server` 가 `Up (healthy)` 다.

**실패 시:** `docker compose logs --tail 50 yolo-server` 를 보고한다. `pip install lapx` 가 실패하면 3단계처럼 https://pypi.org/project/lapx/ 를 확인한다. 엔진 로드 오류면 R1 (이미지 digest) 을 다시 확인한다.

### R4. 접근 제한 (상태 변경, Orin, 선택)

**목적:** 인증 없는 포트 8082 를 Mac 에서만 닿게 좁힌다. 해커톤 정책상 키 인증은 두지 않고 바인딩 주소와 ufw 로만 막는다.

**명령 (Orin):**

```bash
sudo ufw status
sudo ufw allow from <mac-ip> to any port 8082 proto tcp
```

**기대 결과:** `sudo ufw status` 에 8082 규칙이 Mac 주소로만 보인다. ufw 가 꺼져 있으면 (inactive) 이 단계는 건너뛰고 R3 의 `YOLO_BIND_ADDR` 만 쓴다.

**실패 시:** 규칙을 잘못 넣었으면 `sudo ufw status numbered` 로 번호를 보고 `sudo ufw delete <번호>` 로 지운다.

### R5. Mac 헬스 확인 (읽기 전용, Mac)

**목적:** Mac 에서 서버가 보이는지 확인한다.

**명령 (Mac):**

```bash
curl -s http://<orin-ip>:8082/health
```

**기대 결과:** `{"status": "ok", "weights": "models/yolo11n.engine", "device": ..., "frames": ...}` 형태의 JSON.

**실패 시:** 연결 거부면 R3 의 `docker compose ps` 와 R4 의 ufw 규칙, `YOLO_BIND_ADDR` 를 확인한다. 타임아웃이면 Mac 과 Orin 이 같은 네트워크인지 확인한다.

### R6. 벤치 (상태 변경: 서버 트래커 상태, Mac)

**목적:** 시뮬레이터 없이 JPEG 디렉터리를 순서대로 보내 프레임당 지연을 total (Mac 왕복) / orin (서버 처리) / net (차이) 으로 나눠 잰다.

**명령 (Mac, WavePilot 디렉터리에서):**

```bash
uv run python -m modules.yolo_detection.remote.bench --url http://<orin-ip>:8082 --images <dir> --count 200
```

`<dir>` 은 시뮬레이터 녹화 프레임 JPEG 디렉터리다. 없으면 5단계의 `bench_frames` 를 `rsync -av <user>@<orin-ip>:~/wavepilot/bench_frames/ <dir>/` 로 받아 쓴다.

**기대 결과:** n, 그리고 total_ms / orin_ms / net_ms 각각의 p50 / p95 / max 와 실패 수가 표로 나온다.

**실패 시:** 연결 오류면 R5 를 다시 한다. 실패 수가 많으면 출력 전체와 `docker compose logs --tail 50 yolo-server` 를 보고한다.

### R7. 시뮬레이터 연동 (상태 변경, Mac)

**목적:** 브라우저 시뮬레이터 프레임을 Orin 서버로 탐지해 실제 실행 경로에서 지연을 잰다.

**명령 (Mac, WavePilot 디렉터리에서, 둘 중 하나 또는 둘 다):**

```bash
uv run python -m modules.drone.run_scenario --sim browser --yolo orin --yolo-url http://<orin-ip>:8082 --operator-port <port>
uv run python -m modules.drone.sim_demo --live --yolo orin --yolo-url http://<orin-ip>:8082
```

`--yolo local` (기본값) 로 바꾸면 다른 변경 없이 Mac 로컬 YOLO 로 돌아간다. orin 모드에서는 Mac 에 yolo uv 그룹이 없어도 된다.

**기대 결과:** 브라우저 오버레이에 탐지 상자가 나오고, 종료 시 INFO 로그에 n 과 total / orin / net 의 p50 / p95 / max, 실패 수 요약이 찍힌다.

**실패 시:** 시작하자마자 오류로 끝나면 서버 미기동이다 (R3·R5 확인). 연속 실패로 멈추면 그 직전 경고 로그와 `docker compose logs --tail 50 yolo-server` 를 보고한다.

### R8. 종료 (상태 변경, Orin)

**목적:** 서버를 내리고 R2 에서 멈춘 서버를 되돌린다.

**명령 (Orin):**

```bash
cd ~/wavepilot/modules/yolo_detection/deploy/jetson && docker compose down
cd ~/wavepilot/modules/llm_report/deploy/jetson && docker compose up -d llama-report
```

두 번째 줄은 R2 에서 llama-report 를 멈췄고 다시 쓸 때만 실행한다 (llama-decision 도 같은 방식으로 자기 디렉터리에서).

**기대 결과:** `docker ps` 에 `yolo-server` 가 없다.

**실패 시:** `down` 이 멈추면 `docker compose kill yolo-server` 후 다시 `down` 한다.

### 보고 양식 (Phase 7)

맨 앞에 `orin yolo server ok` (또는 실패한 R 단계) 를 적고, 아래 항목을 붙여 넣는다.

```text
orin yolo server ok
Docker 이미지 태그 / digest: (R1)
yolo11n.engine 크기: (R1)
/health JSON: (R5)
Mac <-> Orin 연결: Wi-Fi / Ethernet
bench n: / 실패 수:
bench total_ms p50 / p95 / max:
bench orin_ms p50 / p95 / max:
bench net_ms p50 / p95 / max:
run_scenario 또는 sim_demo 종료 요약 (n, total / orin / net 의 p50 / p95 / max, 실패 수):
실행 중 free -h Mem used/total: (Orin)
R2 에서 멈춘 서버: 없음 / llama-report / llama-decision
실패 시: R 단계 + docker compose logs --tail 50 yolo-server
```

에이전트가 `.planning/phases/07-orin-yolo/07-ORIN-RESULTS.md` 에 옮긴다.
