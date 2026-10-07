FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home app
USER app
EXPOSE 8000
# Migrations are idempotent; apply them, then serve.
CMD ["sh", "-c", "alembic upgrade head && uvicorn --factory metering.api.app:create_app --host 0.0.0.0 --port 8000"]
