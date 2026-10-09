FROM golang:1.24.2-bookworm AS builder

ARG MINIO_RELEASE=RELEASE.2025-09-07T16-13-09Z
ARG MINIO_COMMIT=07c3a429bfed433e49018cb0f78a52145d4bedeb

RUN apt-get update \
    && apt-get install --yes --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --branch "${MINIO_RELEASE}" --depth 1 \
    https://github.com/minio/minio.git /src/minio \
    && test "$(git -C /src/minio rev-parse HEAD)" = "${MINIO_COMMIT}"
WORKDIR /src/minio
RUN mkdir -p /out \
    && CGO_ENABLED=0 go build -tags kqueue -trimpath \
    -ldflags "$(go run buildscripts/gen-ldflags.go)" \
    -o /out/minio

FROM debian:bookworm-slim AS runtime

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=builder /out/minio /usr/local/bin/minio

VOLUME ["/data"]
EXPOSE 9000 9001
ENTRYPOINT ["/usr/local/bin/minio"]
