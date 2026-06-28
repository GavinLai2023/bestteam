FROM python:3.11-slim
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY src ./src
COPY ui ./ui
RUN pip install --no-cache-dir ".[ui,tools]"
EXPOSE 8000
CMD ["uvicorn", "ui.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
