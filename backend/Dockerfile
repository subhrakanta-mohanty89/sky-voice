# syntax=docker/dockerfile:1.7
# ──────────────────────────────────────────────────────────────────────────
# Sky Voice AI — Twilio backend (Flask + flask-sock + SQLAlchemy)
# Optimised for Google Cloud Run. Honors $PORT, runs as non-root,
# uses gthread workers (required for flask-sock WebSockets).
# ──────────────────────────────────────────────────────────────────────────

# ---------- 1. Builder stage: compile wheels into a virtualenv ----------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# psycopg[binary] ships its own libpq, but build-essential is still useful
# for any wheel that needs to compile (e.g. SQLAlchemy C extension).
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential \
 && rm -rf /var/lib/apt/lists/*

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.txt ./
RUN pip install --upgrade pip \
 && pip install -r requirements.txt

# ---------- 2. Runtime stage: minimal image with venv + source ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    PORT=8080 \
    HOST=0.0.0.0 \
    WORKERS=1 \
    THREADS=8 \
    TIMEOUT=0 \
    LOG_LEVEL=INFO

# Minimal runtime libs (libpq runtime is bundled with psycopg[binary], so
# we only need tini for clean PID-1 signal handling).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid 1000 app \
 && useradd  --system --uid 1000 --gid app --home /app --shell /usr/sbin/nologin app

WORKDIR /app

# Copy the pre-built virtualenv from the builder
COPY --from=builder /opt/venv /opt/venv

# Copy application source (build context = ./backend, see .dockerignore)
COPY --chown=app:app . /app

USER app

EXPOSE 8080

# tini → gunicorn with gthread workers (required for flask-sock WebSockets).
# Cloud Run sets $PORT (8080); the shell expansion below picks it up.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["sh", "-c", "exec gunicorn \
  --bind 0.0.0.0:${PORT} \
  --workers ${WORKERS} \
  --threads ${THREADS} \
  --worker-class gthread \
  --timeout ${TIMEOUT} \
  --graceful-timeout 30 \
  --keep-alive 65 \
  --access-logfile - \
  --error-logfile - \
  --log-level ${LOG_LEVEL} \
  run:app"]
