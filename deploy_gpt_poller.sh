#!/usr/bin/env bash
# Build + run the coldemail-gpt-poller sidecar.
#
# Requires env vars on .env.v2: GPT_CLIENT_ID, GPT_CLIENT_SECRET, GPT_REFRESH_TOKEN
# (added after Josh's one-off OAuth bootstrap delivers them).
#
# Tail logs:  docker logs -f coldemail-gpt-poller
# Force run:  docker exec coldemail-gpt-poller python /app/gpt-poller/poll.py
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env.v2"
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found." >&2; exit 1; }

if ! docker image inspect coldemail-api:v2 >/dev/null 2>&1; then
  echo "ERROR: coldemail-api:v2 image missing. Run ./deploy_api_v2.sh first." >&2
  exit 1
fi

docker stop coldemail-gpt-poller 2>/dev/null || true
docker rm coldemail-gpt-poller 2>/dev/null || true

docker build -t coldemail-gpt-poller:latest -f gpt-poller/Dockerfile .

docker run -d \
  --name coldemail-gpt-poller \
  --env-file "$ENV_FILE" \
  --log-opt max-size=10m --log-opt max-file=3 \
  --tmpfs /tmp:size=50M \
  --restart unless-stopped \
  coldemail-gpt-poller:latest

echo
docker ps --filter name=coldemail-gpt-poller --format "  {{.Names}}  {{.Status}}"
echo
echo "Tail logs:  docker logs -f coldemail-gpt-poller"
echo "Force run:  docker exec coldemail-gpt-poller python /app/gpt-poller/poll.py"
