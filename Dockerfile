# One `pac-sync run` per container start; schedule it from cron, Task Scheduler or a Kubernetes CronJob (README).
# Mount a folder at /data holding config/config.yaml and .env: the state file and lock land beside them.
FROM python:3.12-slim

# git: history.mode git / both
RUN apt-get update && apt-get install -y --no-install-recommends git && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml ./
COPY pac_sync ./pac_sync
COPY templates ./templates
RUN pip install --no-cache-dir . && useradd --create-home --uid 10001 pacsync
USER pacsync

ENV PAC_SYNC_CONFIG=/data/config/config.yaml
VOLUME /data
ENTRYPOINT ["pac-sync"]
CMD ["run"]
