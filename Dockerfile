# syntax=docker/dockerfile:1.7

FROM python:3.14-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv

RUN apt-get update && apt-get install -y --no-install-recommends \
      build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VIRTUAL_ENV"
ENV PATH="$VIRTUAL_ENV/bin:$PATH"
WORKDIR /build

# Keep the expensive dependency layer independent of application source.
COPY requirements.txt ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --upgrade pip setuptools wheel \
    && python -m pip install -r requirements.txt \
    && python -m pip check

COPY pyproject.toml README.md LICENSE NOTICE THIRD_PARTY_NOTICES.md ./
COPY src ./src
RUN python -m pip install --no-deps . \
    && python -m pip check


FROM python:3.14-slim-bookworm AS runtime

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH=/opt/venv/bin:$PATH \
    HOME=/home/biomni \
    BIOMNI_DATA_PATH=/data \
    BIOMNI_OUTPUT_PATH=/output \
    BIOMNI_SKIP_DATA_DOWNLOAD=true \
    BIOMNI_TIMEOUT_SECONDS=1200 \
    BIOMNI_LLM_STREAM_TRANSPORT=true \
    BIOMNI_QWEN_DISABLE_THINKING=true \
    BIOMNI_CREDENTIAL_MODE=auto \
    BIOMNI_UI_PUBLIC_ENDPOINTS_ONLY=true \
    BIOMNI_SESSION_TTL_SECONDS=3600 \
    GRADIO_SERVER_NAME=0.0.0.0 \
    GRADIO_SERVER_PORT=7860 \
    GRADIO_TEMP_DIR=/tmp/gradio \
    GRADIO_ANALYTICS_ENABLED=False \
    MPLCONFIGDIR=/tmp/matplotlib \
    XDG_CACHE_HOME=/tmp/cache

# Pango/Harfbuzz/font libraries are needed by WeasyPrint. The small shell and
# HTTP utilities cover common Biomni-generated workflows without shipping a
# full compiler toolchain in the runtime image. tini reaps subprocesses cleanly.
RUN apt-get update && apt-get install -y --no-install-recommends \
      bash \
      ca-certificates \
      curl \
      jq \
      tini \
      wget \
      fonts-dejavu-core \
      libffi8 \
      libgdk-pixbuf-2.0-0 \
      libglib2.0-0 \
      libharfbuzz0b \
      libharfbuzz-subset0 \
      libjpeg62-turbo \
      libopenjp2-7 \
      libpango-1.0-0 \
      libpangoft2-1.0-0 \
      shared-mime-info \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid "$APP_GID" biomni \
    && useradd --uid "$APP_UID" --gid "$APP_GID" --create-home --shell /bin/bash biomni \
    && mkdir -p /data /output /tmp/gradio /tmp/matplotlib /tmp/cache /workspace \
    && chown -R biomni:biomni /data /output /tmp/gradio /tmp/matplotlib /tmp/cache /workspace

COPY --from=builder /opt/venv /opt/venv

# Test the *runtime* layer, including WeasyPrint native libraries and the exact
# Biomni 0.0.8 compatibility seam, before an image can be published.
COPY scripts/smoke_import.py /tmp/smoke_import.py
RUN python /tmp/smoke_import.py && rm /tmp/smoke_import.py

WORKDIR /workspace
USER biomni

EXPOSE 7860

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:7860/', timeout=3).read(1)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["biomni-bridge"]
