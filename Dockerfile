# syntax=docker/dockerfile:1

# --- test stage: installed deps + pytest, never shipped ----------------------
FROM python:3.12-slim AS test
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
COPY requirements.txt requirements-dev.txt ./
RUN pip install -r requirements.txt -r requirements-dev.txt \
    && apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
# The suite reads the repo's own files — the launcher, the docs it cross-checks,
# the compose file, the guard, the workflow. Enumerating them here meant every new
# test that read a new file turned CI red on a missing path, so take the whole
# checkout and let .dockerignore decide (a test asserts the files the suite needs
# are not ignored).
COPY . .

RUN python -m pytest

# --- runtime stage: just the app --------------------------------------------
FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOST=0.0.0.0 \
    PORT=8000
WORKDIR /app
COPY requirements.txt ./
RUN pip install -r requirements.txt \
    && useradd --system --uid 10001 --create-home appuser
COPY app ./app
USER 10001
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os,sys,urllib.request;port=os.getenv('PORT','8000');sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/healthz', timeout=4).status == 200 else 1)"]
CMD ["sh", "-c", "exec uvicorn app.main:app --host \"$HOST\" --port \"$PORT\" --proxy-headers"]
