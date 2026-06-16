#!/usr/bin/env bash
# Build + run the coldemail-blacklist-monitor sidecar.
#
# Layers on top of coldemail-api:v2. Daily cron at 06:00 UTC scans every
# active shard's VPS IP + root domain against EmailGuard's 100+ blacklists.
# Results land in Supabase tables blacklist_checks (history) and
# latest_blacklist_checks (view), and are joined into shard_health_recommendations
# so the CRM badge surfaces them per shard.
#
# Tail logs: docker logs -f coldemail-blacklist-monitor
# Force scan: docker exec coldemail-blacklist-monitor python /app/blacklist/poll.py
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

docker stop coldemail-blacklist-monitor 2>/dev/null || true
docker rm coldemail-blacklist-monitor 2>/dev/null || true

docker build -t coldemail-blacklist-monitor:latest -f blacklist/Dockerfile .

EXTRA_ENV=()
if [ -n "${EMAILGUARD_API_KEY:-}" ]; then
  EXTRA_ENV=(-e "EMAILGUARD_API_KEY=$EMAILGUARD_API_KEY")
fi

docker run -d \
  --name coldemail-blacklist-monitor \
  --env-file "$ENV_FILE" \
  "${EXTRA_ENV[@]}" \
  --log-opt max-size=10m --log-opt max-file=3 \
  --tmpfs /tmp:size=200M \
  --restart unless-stopped \
  coldemail-blacklist-monitor:latest

echo
echo "Blacklist monitor sidecar:"
docker ps --filter name=coldemail-blacklist-monitor --format "  {{.Names}}  {{.Status}}"
echo
echo "Tail logs:    docker logs -f coldemail-blacklist-monitor"
echo "Force scan:   docker exec coldemail-blacklist-monitor python /app/blacklist/poll.py"
