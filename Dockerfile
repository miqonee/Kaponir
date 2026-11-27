# syntax=docker/dockerfile:1
ARG KAPONIR_BUILD=source
FROM rust:1-slim-trixie AS source
WORKDIR /src
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY packs ./packs
COPY scripts ./scripts
COPY docs/experiments/privacy-policy-v1.txt ./docs/experiments/privacy-policy-v1.txt
RUN cargo build --locked --release -j 2 -p kaponir-cli && mkdir /out && cp target/release/kaponir /out/kaponir

# Optional fast local build, only for a Linux binary of the target architecture.
FROM scratch AS prebuilt
COPY target/release/kaponir /out/kaponir

FROM ${KAPONIR_BUILD} AS selected

FROM python:3.13-slim-trixie AS ocr-runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates libgomp1 libstdc++6 && rm -rf /var/lib/apt/lists/*
COPY scripts/requirements-ocr.txt scripts/requirements-worker.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements-worker.txt

FROM ocr-runtime AS proxy
COPY --from=selected /out/kaponir /usr/local/bin/kaponir
COPY deploy/entrypoint.sh /usr/local/bin/kaponir-entrypoint
ENV KAPONIR_HOME=/data
EXPOSE 8787
ENTRYPOINT ["kaponir-entrypoint"]
CMD ["serve", "--listen", "0.0.0.0:8787"]

FROM ocr-runtime AS worker
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core && rm -rf /var/lib/apt/lists/*
COPY --from=selected /out/kaponir /usr/local/bin/kaponir
ENV KAPONIR_HOME=/data KAPONIR_INPUT_ROOT=/inputs KAPONIR_MODEL_DIR=/models KAPONIR_WORKER_LISTEN=0.0.0.0:8788
EXPOSE 8788
ENTRYPOINT ["kaponir", "worker"]
CMD ["serve"]
