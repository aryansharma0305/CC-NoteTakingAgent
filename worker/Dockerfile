# Build context is the repo root (see docker-compose.yml).
# Deno is the JavaScript runtime yt-dlp needs for current YouTube extraction.
FROM denoland/deno:bin-2.1.4 AS deno

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/*
COPY --from=deno /deno /usr/local/bin/deno

WORKDIR /app
COPY shared /src/shared
RUN pip install /src/shared

COPY worker/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

# Person A's Notion library, imported by the publish stage.
COPY notion_publisher/*.py ./notion_publisher/
COPY worker/app ./app

# Stateless: no ports, no container_name, so `--scale worker=N` works.
CMD ["python", "-m", "app.main"]
