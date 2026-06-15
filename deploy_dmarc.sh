#!/usr/bin/env bash
# Build + run the coldemail-dmarc-ingest sidecar.
#
# Layers on top of coldemail-api:v2 (which must already be built — run
# deploy_api_v2.sh first if it isn't). Shares the same .env.v2 (Supabase
# creds) and the same shards/ + ~/.ssh mounts.
#
# Cron is supercronic running */30 * * * * /app/dmarc/dmarc_poll.py.
# Logs to docker stdout: `docker logs -f coldemail-dmarc-ingest`.
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env.v2"
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found." >&2
  exit 1
fi

# Make sure the base image exists. We don't auto-rebuild it because v2 is
# a separate concern with its own deploy pipeline.
if ! docker image inspect coldemail-api:v2 >/dev/null 2>&1; then
  echo "ERROR: coldemail-api:v2 image missing. Run ./deploy_api_v2.sh first." >&2
  exit 1
fi

docker stop coldemail-dmarc-ingest 2>/dev/null || true
docker rm coldemail-dmarc-ingest 2>/dev/null || true

docker build -t coldemail-dmarc-ingest:latest -f dmarc/Dockerfile .

docker run -d \
  --name coldemail-dmarc-ingest \
  --env-file "$ENV_FILE" \
  -v "$(pwd)/shards:/app/shards:ro" \
  -v "$HOME/.ssh:/root/.ssh:ro" \
  --log-opt max-size=10m --log-opt max-file=3 \
  --tmpfs /tmp:size=200M \
  --restart unless-stopped \
  coldemail-dmarc-ingest:latest

echo
echo "DMARC ingest sidecar:"
docker ps --filter name=coldemail-dmarc-ingest --format "  {{.Names}}  {{.Status}}"
echo
echo "Tail logs:    docker logs -f coldemail-dmarc-ingest"
echo "Force poll:   docker exec coldemail-dmarc-ingest python /app/dmarc/dmarc_poll.py"
