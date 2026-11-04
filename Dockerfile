# ToolNet ReelShort Metadata API — production image (multi-stage).
# Secrets (.env) are NEVER baked in: compose injects them at runtime via env_file.
ARG PYTHON_VERSION=3.12

FROM python:${PYTHON_VERSION}-slim AS builder
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY requirements.txt .
# Wheels only (no compiler toolchain kept in the final image).
RUN pip install --prefix=/install -r requirements.txt

FROM python:${PYTHON_VERSION}-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
ENV DB_PATH=/srv/data/reelshort.sqlite3 LOG_FILE=/srv/logs/reelshort-api.log
WORKDIR /srv
# Non-root user matching host ubuntu (uid/gid 1000) so bind mounts stay writable.
RUN groupadd -g 1000 appuser && useradd -u 1000 -g 1000 -m -s /usr/sbin/nologin appuser \
    && mkdir -p /srv/data /srv/logs /srv/app \
    && chown -R 1000:1000 /srv
COPY --from=builder /install /usr/local
COPY --chown=1000:1000 app ./app
COPY --chown=1000:1000 migrations ./migrations
COPY --chown=1000:1000 alembic.ini requirements.txt ./
COPY --chown=1000:1000 scripts ./scripts
COPY --chown=1000:1000 docker/entrypoint.sh /srv/entrypoint.sh
RUN chmod +x /srv/entrypoint.sh /srv/scripts/*.sh 2>/dev/null || true
USER 1000:1000
EXPOSE 8077
HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8077/health', timeout=5)"
ENTRYPOINT ["/srv/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8077", "--workers", "1", "--proxy-headers", "--no-server-header"]
