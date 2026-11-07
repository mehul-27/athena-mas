# syntax=docker/dockerfile:1
# Athena: one container, one machine, one volume.
#
#   uvicorn            0.0.0.0:${PORT}        the browser talks to this
#   crew_server        127.0.0.1:9001         the A2A role server (loopback only)
#   athena.run         subprocess per run     spawned by the API, one at a time
#
# State that must survive a restart lives on the volume at /app/data: the
# SQLite databases, the corpus and vector index, the encrypted settings vault
# (data/settings/master.key + secrets.enc.json) and the active profile. Nothing
# secret is baked into the image; .dockerignore keeps .env, data/settings and
# .venv out of the build context.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

# Every pin installs as a wheel, so no compiler is needed. libgomp1 is the one
# runtime library onnxruntime (pulled in by fastembed/chromadb) links against.
RUN apt-get update \
 && apt-get install -y --no-install-recommends libgomp1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first: a code-only change must not reinstall the world.
COPY requirements.txt ./requirements.txt
RUN python -m pip install --no-cache-dir -r requirements.txt

# Only what the API needs at runtime. Tests, docs, evaluation, spike and the
# local data directory stay out of the image (.dockerignore).
COPY athena ./athena
COPY scripts ./scripts
COPY data/demo ./data/demo
COPY data/eval ./data/eval
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh

# Build identity, reported by /readyz and the startup log line.
ARG ATHENA_VERSION=0.1.0
ARG ATHENA_GIT_SHA=unknown
ENV ATHENA_VERSION=${ATHENA_VERSION} \
    ATHENA_GIT_SHA=${ATHENA_GIT_SHA}

# Every writable path points at the volume. ATHENA_ALLOWED_HOSTS and
# ATHENA_ACCESS_TOKEN are deliberately NOT set here: they belong to the
# deployment's secrets/env, so the image cannot be baked for one hostname.
ENV ATHENA_DATA_DIR=/app/data \
    EVENTS_DB=/app/data/events.db \
    BLACKBOARD_DB=/app/data/blackboard.db \
    CHAT_DB=/app/data/chat.db \
    RATELIMIT_DB=/app/data/ratelimit.db \
    RATELIMIT_OUT_DB=/app/data/ratelimit_out.db \
    CHROMA_PATH=/app/data/chroma \
    CORPUS_DIR=/app/data/corpus \
    FASTEMBED_CACHE_PATH=/app/data/fastembed_cache \
    ATHENA_START_CREW=1 \
    ATHENA_LOG_FORMAT=json \
    PORT=8080 \
    TMPDIR=/app/data/tmp \
    TMP=/app/data/tmp \
    TEMP=/app/data/tmp \
    MPLCONFIGDIR=/app/.run/mpl

# Non-root with a fixed uid, so a platform-created volume can be chowned to it
# once (`fly ssh console --user root -C "chown -R 10001:10001 /app/data"`).
RUN groupadd --gid 10001 athena \
 && useradd --uid 10001 --gid 10001 --home /app --no-create-home \
            --shell /usr/sbin/nologin athena \
 && mkdir -p /app/data/tmp /app/.run/mpl /app/.run/runs /app/.run/api \
 && chown -R athena:athena /app \
 && chmod +x /usr/local/bin/entrypoint.sh

USER athena

EXPOSE 8080

# Liveness only: /healthz does no I/O, so a slow volume cannot make the
# platform kill a container that is still serving. Readiness is /readyz.
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
  CMD ["python", "-c", "import os,sys,urllib.request;port=os.environ.get('PORT','8080');sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:'+port+'/healthz',timeout=4).status==200 else 1)"]

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
