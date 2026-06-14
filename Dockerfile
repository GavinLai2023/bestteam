FROM python:3.11-slim
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
COPY ui ./ui
RUN pip install --no-cache-dir ".[ui,tools]"
EXPOSE 8000
CMD ["uvicorn", "ui.backend.main:app", "--host", "0.0.0.0", "--port", "8000"]
