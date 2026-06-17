#!/usr/bin/env bash
# Build + run the coldemail-warmup-poller sidecar.
#
# Layers on top of coldemail-api:v2. Runs the bison-deliverability
# process_warmup_poller.py inside a supercronic-driven container every 4 hours.
# Pulls health_score + days_warming from Instantly into instantly_warmup_state
# so the CRM wmh% column shows fresh signal.
#
# Tail logs:  docker logs -f coldemail-warmup-poller
# Force run:  docker exec coldemail-warmup-poller python /app/warmup-poller/process_warmup_poller.py
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env.v2"
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found." >&2
  exit 1
fi

if ! docker image inspect coldemail-api:v2 >/dev/null 2>&1; then
  echo "ERROR: coldemail-api:v2 image missing. Run ./deploy_api_v2.sh first." >&2
  exit 1
fi

docker stop coldemail-warmup-poller 2>/dev/null || true
docker rm coldemail-warmup-poller 2>/dev/null || true

docker build -t coldemail-warmup-poller:latest -f warmup-poller/Dockerfile .

docker run -d \
  --name coldemail-warmup-poller \
  --env-file "$ENV_FILE" \
  --log-opt max-size=10m --log-opt max-file=3 \
  --tmpfs /tmp:size=50M \
  --restart unless-stopped \
  coldemail-warmup-poller:latest

echo
echo "Warmup poller sidecar:"
docker ps --filter name=coldemail-warmup-poller --format "  {{.Names}}  {{.Status}}"
echo
echo "Tail logs:  docker logs -f coldemail-warmup-poller"
echo "Force run:  docker exec coldemail-warmup-poller python /app/warmup-poller/process_warmup_poller.py"
