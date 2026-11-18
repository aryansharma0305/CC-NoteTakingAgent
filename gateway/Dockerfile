# Build context is the repo root (see docker-compose.yml).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY shared /src/shared
RUN pip install /src/shared

COPY gateway/requirements.txt /tmp/requirements.txt
RUN pip install -r /tmp/requirements.txt

COPY gateway/app ./app

EXPOSE 8000
# Apply migrations (advisory-locked, idempotent), then serve.
CMD ["sh", "-c", "python -m vidnotes_common.migrate && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers"]
