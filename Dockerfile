FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
# Run as an unprivileged user; only the data directory (the SQLite database
# and knowledge-base uploads, a named volume in docker-compose.yml) is
# writable. A volume created by an earlier root-running image needs a one-off
# `chown -R 1000:1000` -- see docs/deployment.md.
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin app
COPY pyproject.toml requirements.lock ./
COPY src ./src
COPY ui ./ui
# Alembic assets are required for the documented `alembic upgrade` step in
# docs/deployment.md -- create_all() is not an upgrade mechanism (CR-006).
COPY alembic.ini ./
COPY alembic ./alembic
# `requirements.lock` pins every transitive dependency (`uv pip compile`, see
# README "Updating the lockfile"), so a rebuild reproduces the same versions a
# previous build ran on -- the beta hot-fix contract (STATUS.md, G1).
RUN pip install --no-cache-dir -c requirements.lock ".[ui,tools,providers-openai]"
COPY docker-entrypoint.sh ./
RUN chmod 0755 docker-entrypoint.sh     && mkdir -p ui/backend/data     && chown -R app:app ui/backend/data
USER app
EXPOSE 8000
# Liveness for `docker compose ps` / restart policies. The slim image has no
# curl, so the probe is the stdlib.
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3     CMD python -c "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=4).status == 200 else 1)"
# The entrypoint runs `alembic upgrade head` before starting the server (and
# only then -- see docker-entrypoint.sh).
ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["uvicorn", "ui.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
