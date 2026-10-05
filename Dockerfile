ARG PYTHON_IMAGE=python:3.13.16-slim-trixie@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81
ARG UV_VERSION=0.12.19

FROM ${PYTHON_IMAGE} AS builder
ARG UV_VERSION
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    UV_PYTHON=/usr/local/bin/python3 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
RUN pip install "uv==${UV_VERSION}"
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

FROM ${PYTHON_IMAGE} AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH=/opt/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    HOME=/tmp \
    JOBLIB_TEMP_FOLDER=/tmp \
    MPLCONFIGDIR=/tmp \
    OMP_NUM_THREADS=1 \
    DL_HOST=0.0.0.0 \
    DL_PORT=8080
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus tesseract-ocr-eng tini \
 && rm -rf /var/lib/apt/lists/* /var/cache/apt/archives/*
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid 10001 --no-create-home --home-dir /tmp --shell /usr/sbin/nologin app \
 && mkdir -p /data && chown 10001:10001 /data && chmod 0711 /data
ENV DL_DATA_DIR=/data
RUN python -m compileall -q -j 0 --invalidation-mode unchecked-hash \
      "$(python -c 'import sysconfig; print(sysconfig.get_path("stdlib"))')"
WORKDIR /app
COPY --from=builder /opt/venv /opt/venv
COPY src ./src
RUN python -m compileall -q --invalidation-mode unchecked-hash /app/src
COPY config ./config
COPY models ./models
COPY data/demo ./data/demo
COPY docs/metrics ./docs/metrics
USER 10001
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --start-interval=2s --retries=3 \
    CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3).status == 200 else 1)"]
ENV DL_TRUSTED_PROXIES=""
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "deficitlens_api.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--no-server-header", "--no-proxy-headers"]
