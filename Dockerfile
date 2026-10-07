# Needs a named build context "jobq" with Project 1's source (docker-compose.yml passes the
# GitHub repo by default):
#   docker build --build-context jobq=https://github.com/Rishavbhattarai/distributed-job-queue.git#main .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

# jobq: the client this API uses to enqueue jobs, and the worker that runs the billing jobs.
COPY --from=jobq pyproject.toml README.md /opt/jobq/
COPY --from=jobq src /opt/jobq/src
RUN pip install "/opt/jobq[server]"

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install .

COPY alembic.ini ./
COPY migrations ./migrations

RUN useradd --create-home --uid 10002 app && mkdir -p /data && chown app /data
USER app
EXPOSE 8000
CMD ["uvicorn", "--factory", "metering.api.app:create_app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
