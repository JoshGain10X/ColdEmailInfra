#!/usr/bin/env bash
# Build + run the coldemail-ingestion-monitor sidecar (Layers 1+2).
#
# Requires .env.v2 with: CLOUDFLARE_API_TOKEN, BISON_API_BASE, EB_SUPERADMIN_KEY,
# SUPABASE_URL, SUPABASE_SERVICE_KEY, and (once n8n is wired) N8N_ALERT_WEBHOOK_URL.
#
# Tail logs:  docker logs -f coldemail-ingestion-monitor
# Force run:  docker exec coldemail-ingestion-monitor python /app/monitor/check_health.py
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env.v2"
[ -f "$ENV_FILE" ] || { echo "ERROR: $ENV_FILE not found." >&2; exit 1; }
if ! docker image inspect coldemail-api:v2 >/dev/null 2>&1; then
  echo "ERROR: coldemail-api:v2 image missing. Run ./deploy_api_v2.sh first." >&2
  exit 1
fi

docker stop coldemail-ingestion-monitor 2>/dev/null || true
docker rm coldemail-ingestion-monitor 2>/dev/null || true
docker build -t coldemail-ingestion-monitor:latest -f monitor/Dockerfile .
# ~/.ssh is mounted read-only so Layer 3 can probe shard resource ceilings
# (inotify usage, dovecot imap-login config). Without it Layer 3 logs a skip and
# the other two layers run unaffected.
docker run -d \
  --name coldemail-ingestion-monitor \
  --env-file "$ENV_FILE" \
  -v "$HOME/.ssh:/root/.ssh:ro" \
  --log-opt max-size=10m --log-opt max-file=3 \
  --tmpfs /tmp:size=50M \
  --restart unless-stopped \
  coldemail-ingestion-monitor:latest

echo
docker ps --filter name=coldemail-ingestion-monitor --format "  {{.Names}}  {{.Status}}"
echo "Tail logs:  docker logs -f coldemail-ingestion-monitor"
echo "Force run:  docker exec coldemail-ingestion-monitor python /app/monitor/check_health.py"
