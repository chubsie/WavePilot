# d1-3B 판단 서버용 llama-server 전용 이미지. 코드·모델 파일은 넣지 않는다.
# 빌드·실행은 사용자가 Orin 에서(D-10) — RUNBOOK.md 참고.
#
# 기동 플래그는 compose.yaml 한 곳에만 둔다. 여기에는 CMD 를 두지 않는다 (이중 정의 방지).

ARG L4T_TAG=r36.4.0

# ---------------------------------------------------------------------------
# build 스테이지: llama.cpp 를 b-태그로 받아 llama-server 하나만 CUDA 빌드
# ---------------------------------------------------------------------------
FROM nvcr.io/nvidia/l4t-jetpack:${L4T_TAG} AS build

# LLAMA_CPP_REF: d1-3B(lfm2-d1 아키텍처) 지원은 llama.cpp PR #30110 으로 v0.6.0 이후에 들어왔다.
#   b11541 이 지원이 확인된 첫 태그다 (RESEARCH 1-2절). 빌드가 실패하면 LLAMA_CPP_REF 로 더 새 b-태그를 지정한다.
# CUDA_ARCH: Orin 의 compute capability 는 8.7 (SM87). 86 은 데스크톱 Ampere 라 틀린다.
# BUILD_JOBS: Orin Nano 8 GB 는 CPU/GPU 가 메모리를 공유해 nvcc 병렬 컴파일이 OOM 나기 쉽다. 실패 시 2.
ARG LLAMA_CPP_REF=b11541
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

EXPOSE 8081

ENTRYPOINT ["llama-server"]
