# llama-server 전용 이미지. Python·llm_module·모델 파일은 넣지 않는다(D-08).
# 빌드·실행은 사용자가 Orin 에서(D-09) — RUNBOOK.md 참고.
#
# 기동 플래그는 compose.yaml 한 곳에만 둔다. 여기에는 CMD 를 두지 않는다 (이중 정의 방지).

ARG L4T_TAG=r36.4.0

# ---------------------------------------------------------------------------
# build 스테이지: llama.cpp 를 릴리스 태그로 받아 llama-server 하나만 CUDA 빌드
# ---------------------------------------------------------------------------
FROM nvcr.io/nvidia/l4t-jetpack:${L4T_TAG} AS build

# LLAMA_CPP_REF: SKELETON 계약 릴리스 태그 (2026-10-05).
# CUDA_ARCH: Orin 의 compute capability 는 8.7 (SM87). 86 은 데스크톱 Ampere 라 틀린다.
# BUILD_JOBS: Orin Nano 8 GB 는 CPU/GPU 가 메모리를 공유해 nvcc 병렬 컴파일이 OOM 나기 쉽다. 실패 시 2.
ARG LLAMA_CPP_REF=v0.6.0
ARG CUDA_ARCH=87
ARG BUILD_JOBS=4

ENV PATH=/usr/local/cuda/bin:${PATH} \
    LIBRARY_PATH=/usr/local/cuda/lib64/stubs:${LIBRARY_PATH}

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      git cmake build-essential libcurl4-openssl-dev ca-certificates \
 && rm -rf /var/lib/apt/lists/*

RUN git clone --depth 1 --branch "${LLAMA_CPP_REF}" https://github.com/ggml-org/llama.cpp /src

WORKDIR /src

# BUILD_SHARED_LIBS=OFF: 정적 단일 바이너리로 만들어 런타임 스테이지에 .so 를 복사하지 않으려는 것.
# [ASSUMED — 런북 4단계 `--version` 실행으로 확인. 실패하면 업스트림처럼 공유 라이브러리 복사 방식으로 전환]
RUN cmake -B build \
      -DGGML_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES=${CUDA_ARCH} \
      -DBUILD_SHARED_LIBS=OFF \
      -DLLAMA_BUILD_TESTS=OFF \
      -DCMAKE_BUILD_TYPE=Release \
 && cmake --build build --target llama-server -j"${BUILD_JOBS}"

# ---------------------------------------------------------------------------
# 런타임 스테이지: llama-server 바이너리 + healthcheck 용 curl 만
# ---------------------------------------------------------------------------
FROM nvcr.io/nvidia/l4t-jetpack:${L4T_TAG}

RUN apt-get update \
 && apt-get install -y --no-install-recommends curl \
 && rm -rf /var/lib/apt/lists/*

COPY --from=build /src/build/bin/llama-server /usr/local/bin/llama-server

ENV NVIDIA_VISIBLE_DEVICES=all \
    NVIDIA_DRIVER_CAPABILITIES=compute,utility

EXPOSE 8080

ENTRYPOINT ["llama-server"]
