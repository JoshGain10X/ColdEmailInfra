#!/usr/bin/env bash
# Rebuild and restart the ColdEmailInfra API container.
# Run from the repo root on the API VPS (193.180.211.74).
set -euo pipefail
cd "$(dirname "$0")"

docker stop coldemail-api 2>/dev/null || true
docker rm coldemail-api 2>/dev/null || true
docker build -t coldemail-api -f api/Dockerfile .
docker run -d \
  --name coldemail-api \
  --env-file .env \
  -p 8000:8000 \
  -v "$(pwd)/shards:/app/shards" \
  -v "$HOME/.ssh:/root/.ssh:ro" \
  --restart unless-stopped \
  coldemail-api

echo "API container running:"
docker ps --filter name=coldemail-api --format "  {{.Names}}  {{.Status}}"
