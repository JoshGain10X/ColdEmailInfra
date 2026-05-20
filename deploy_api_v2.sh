#!/usr/bin/env bash
# Build + run the v2 multi-tenant API container alongside the existing v1.
#
# v1 (existing): coldemail-api    on :8000  -> infra-api.10xmanagers.com   -> Commission Calculator Supabase project
# v2 (this):     coldemail-api-v2 on :8001  -> infra-api-v2.10xmanagers.com -> Cold Email Data Supabase project
#
# Requires:
#   - .env.v2 file alongside this script (see .env.v2.example)
#   - Caddy site config for infra-api-v2.10xmanagers.com -> 127.0.0.1:8001
#   - DNS A record infra-api-v2.10xmanagers.com -> this host (already provisioned)
#
# v1 is NOT touched. To roll back v2: docker stop coldemail-api-v2; docker rm coldemail-api-v2.
set -euo pipefail
cd "$(dirname "$0")"

ENV_FILE=".env.v2"
if [ ! -f "$ENV_FILE" ]; then
  echo "ERROR: $ENV_FILE not found. Copy .env.v2.example and fill in the CED Supabase service key." >&2
  exit 1
fi

docker stop coldemail-api-v2 2>/dev/null || true
docker rm coldemail-api-v2 2>/dev/null || true

# Build a separate image tag so v1 stays pinned at whatever it was last built with
docker build -t coldemail-api:v2 -f api/Dockerfile .

docker run -d \
  --name coldemail-api-v2 \
  --env-file "$ENV_FILE" \
  -p 8001:8000 \
  -v "$(pwd)/shards:/app/shards" \
  -v "$HOME/.ssh:/root/.ssh:ro" \
  --restart unless-stopped \
  coldemail-api:v2

echo
echo "v2 API container:"
docker ps --filter name=coldemail-api-v2 --format "  {{.Names}}  {{.Status}}  {{.Ports}}"
echo
echo "Tail logs:    docker logs -f coldemail-api-v2"
echo "Health check: curl -s http://127.0.0.1:8001/api/health"
