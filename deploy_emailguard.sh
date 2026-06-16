#!/usr/bin/env bash
# Build + run the coldemail-emailguard-ingest sidecar.
#
# Layers on top of coldemail-api:v2 (which must already be built — run
# deploy_api_v2.sh first if it isn't). Uses .env.v2 plus an EMAILGUARD_API_KEY
# env var (read from the deployer's shell or .env.v2).
#
# Cron is supercronic running */5 * * * * /app/emailguard/poll.py.
# Logs to docker stdout: `docker logs -f coldemail-emailguard-ingest`.
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env.v2"
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found." >&2
  exit 1
fi

if [ -z "${EMAILGUARD_API_KEY:-}" ] && ! grep -q "^EMAILGUARD_API_KEY=" "$ENV_FILE"; then
  echo "ERROR: EMAILGUARD_API_KEY must be set in the shell or in $ENV_FILE" >&2
  exit 1
fi

if ! docker image inspect coldemail-api:v2 >/dev/null 2>&1; then
  echo "ERROR: coldemail-api:v2 image missing. Run ./deploy_api_v2.sh first." >&2
  exit 1
fi

docker stop coldemail-emailguard-ingest 2>/dev/null || true
docker rm coldemail-emailguard-ingest 2>/dev/null || true

docker build -t coldemail-emailguard-ingest:latest -f emailguard/Dockerfile .

EXTRA_ENV=()
if [ -n "${EMAILGUARD_API_KEY:-}" ]; then
  EXTRA_ENV=(-e "EMAILGUARD_API_KEY=$EMAILGUARD_API_KEY")
fi

docker run -d \
  --name coldemail-emailguard-ingest \
  --env-file "$ENV_FILE" \
  "${EXTRA_ENV[@]}" \
  --log-opt max-size=10m --log-opt max-file=3 \
  --tmpfs /tmp:size=200M \
  --restart unless-stopped \
  coldemail-emailguard-ingest:latest

echo
echo "EmailGuard ingest sidecar:"
docker ps --filter name=coldemail-emailguard-ingest --format "  {{.Names}}  {{.Status}}"
echo
echo "Tail logs:    docker logs -f coldemail-emailguard-ingest"
echo "Force poll:   docker exec coldemail-emailguard-ingest python /app/emailguard/poll.py"
