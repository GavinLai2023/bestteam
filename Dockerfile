FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
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
EXPOSE 8000
CMD ["uvicorn", "ui.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
