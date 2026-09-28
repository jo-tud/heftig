# Heftig – one image for the web server and the worker.
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HEFTIG_ARCHIVE_DIR=/archive \
    HEFTIG_CONSUME_DIR=/consume \
    HEFTIG_HOST=0.0.0.0 \
    HEFTIG_PORT=8765

# Offline OCR (Tesseract with German + English data). Nothing else is needed at runtime.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
# Dependencies first, in their own layer (code changes rebuild without touching PyPI): exactly
# the versions of uv.lock, every file checked against its hash.
COPY requirements.lock ./
RUN PIP_DEFAULT_TIMEOUT=60 pip install --require-hashes --no-deps -r requirements.lock
# The application itself runs from its source (no build step, no build tools in the image).
COPY src ./src
ENV PYTHONPATH=/app/src
RUN printf '#!/usr/local/bin/python\nfrom heftig.cli import main\nraise SystemExit(main())\n' \
      > /usr/local/bin/heftig && chmod 755 /usr/local/bin/heftig

# The entrypoint drops root privileges to PUID:PGID (default 1000:1000) under rootful Docker
# and keeps the (already unprivileged) mapped user under rootless Podman/Docker.
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN mkdir -p /archive /consume /folder && chmod 755 /usr/local/bin/docker-entrypoint.sh
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
VOLUME ["/archive"]
EXPOSE 8765

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; urllib.request.urlopen('http://127.0.0.1:8765/health', timeout=4)" || exit 1

# web server and worker in one process; compose.yaml runs them as two services instead
CMD ["heftig", "run"]
