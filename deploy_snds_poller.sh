#!/usr/bin/env bash
# Build + run the coldemail-snds-poller sidecar.
#
# Requires env var on .env.v2: SNDS_DATA_URL (the per-workspace CSV URL
# Microsoft issues after SNDS approval).
#
# Tail logs:  docker logs -f coldemail-snds-poller
# Force run:  docker exec coldemail-snds-poller python /app/snds-poller/poll.py
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env.v2"
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found." >&2; exit 1; }

if ! docker image inspect coldemail-api:v2 >/dev/null 2>&1; then
  echo "ERROR: coldemail-api:v2 image missing. Run ./deploy_api_v2.sh first." >&2
  exit 1
fi

docker stop coldemail-snds-poller 2>/dev/null || true
docker rm coldemail-snds-poller 2>/dev/null || true

docker build -t coldemail-snds-poller:latest -f snds-poller/Dockerfile .

docker run -d \
  --name coldemail-snds-poller \
  --env-file "$ENV_FILE" \
  --log-opt max-size=10m --log-opt max-file=3 \
  --tmpfs /tmp:size=50M \
  --restart unless-stopped \
  coldemail-snds-poller:latest

echo
docker ps --filter name=coldemail-snds-poller --format "  {{.Names}}  {{.Status}}"
echo
echo "Tail logs:  docker logs -f coldemail-snds-poller"
echo "Force run:  docker exec coldemail-snds-poller python /app/snds-poller/poll.py"
