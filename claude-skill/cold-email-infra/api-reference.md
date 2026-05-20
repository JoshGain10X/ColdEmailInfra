# ColdEmailInfra v2 API reference

All endpoints under `$INFRA_API_BASE` require `Authorization: Bearer $INFRA_API_KEY` except `/api/health`.

## Conventions

- `client_slug` — short identifier (e.g. `10x-managers`, `reachos`). Required on create-type endpoints.
- For domain-keyed routes, client is inferred from the shard row in `infra_shards`. Pass `client_slug` in body only if disambiguation is needed.
- Job-based endpoints return `{job_id, status: "pending"}` immediately. Poll `/api/jobs/{job_id}` for progress.
- All times are ISO8601 UTC.

## Health
```
GET /api/health → {"status":"ok","version":"2.0.0"}
```

## Clients
```
GET /api/clients → {"clients":[{id, slug, name, status, website_url, onboarded_at, created_at}, ...]}
```

## Shards
```
GET /api/shards[?client_slug=<slug>]
  → {"shards":[{id, client_id, domain, vps_ip, provider, region, status, mailbox_count,
                bison_loaded, bison_workspace, step_flags, csv_storage_path,
                bison_loaded_at, destroyed_at, created_at}, ...]}

GET /api/shards/{domain}[?client_slug=<slug>]
  → {"shard": {...}} | 404 | 409 (if multiple clients share that domain — unlikely)

GET /api/shards/{domain}/csv[?client_slug=<slug>]
  → {"url": "<signed Supabase Storage URL, 5min ttl>", "filename": "<domain>_bison.csv"}
```

Shard status values: `deploying`, `active`, `verified`, `failed`, `destroying`, `destroyed`.

## Deploy
```
POST /api/deploy
  body: {
    "client_slug": "10x-managers",       # required
    "domain": "newshard.com",            # required
    "provider": "webdock",               # default — only other supported value is "contabo"
    "product_id": null,                  # null → client_settings.vps_plan
    "region": null,                      # null → client_settings.vps_region
    "image_id": null,                    # null → client_settings.vps_image_slug
    "ssl_type": null,                    # null → client_settings.ssl_type ('letsencrypt' or 'self-signed')
    "created_by": null                   # optional audit string
  }
  → {"job_id":"...","domain":"...","client_slug":"...","status":"pending"}
```

10-step background job (~15 min). Poll `/api/jobs/{job_id}` and stream `logs[]`.

Step labels in `step_flags` (jsonb on `infra_shards`):
`ensure_domain`, `provision_vps`, `set_ptr`, `configure_dns`, `install_mailserver`, `create_mailboxes`, `setup_dkim`, `export_bison`, `generate`.

## Destroy
```
POST /api/destroy/{domain}
  body: {"client_slug": "...", "created_by": "..."}  # both optional
  → {"job_id":"...","domain":"...","status":"pending"}
```

4-step job (~2 sec). Destroys VPS via client's Webdock token, deletes Cloudflare DNS records and redirect rules, archives the shard state file to `shards/archived/<domain>-<ts>.json`.

## Verify
```
POST /api/verify/{domain}
  body: {"client_slug": "...", "created_by": "..."}  # both optional
  → {"job_id":"...","domain":"...","status":"pending"}
```

5-step job (~5 sec). Checks A/MX/SPF/DMARC/DKIM records for 3 sample subdomains, FCrDNS (PTR), SMTP banner, TLS certificate.

## Load to Bison
```
POST /api/load-to-bison/{domain}
  body: {
    "workspace": null,             # null → client's default workspace, otherwise pick a named one
    "tag": "Custom SMTP",          # tag applied to all created senders
    "client_slug": null,           # only needed to disambiguate
    "created_by": null
  }
  → {"job_id":"...","domain":"...","status":"pending"}
```

6-step job (~4 min for 100 mailboxes). Creates each sender individually in Bison, attaches the tag, then PATCHes signatures (deterministic, formula-driven per workspace).

## Domains
```
GET /api/domains[?client_slug=<slug>]
  → {"domains":[{id, client_id, domain, zone_id, zone_status, name_servers,
                 zone_created_on, registrar_created_at, registrar_status,
                 last_synced_at}, ...]}

POST /api/domains/check
  body: {"client_slug": "...", "domain": "candidate.com"}
  → {"domain": "...", "available": true, "price": "$8.57", "price_unknown": false}
  | {"domain": "...", "available": false, "reason": "Already on this Cloudflare account"}

POST /api/domains/refresh
  body: {"client_slug": "...", "default_client_slug_for_new": null, "created_by": null}
  → {"job_id":"...","status":"pending"}
# Syncs that client's CF account zones into infra_domains. New zones get attributed
# to default_client_slug_for_new (defaults to client_slug if not set).

POST /api/domains/register
  body: {"client_slug": "...", "domain": "newcoldname.com", "created_by": null}
  → {"job_id":"...","domain":"...","client_slug":"...","status":"pending"}
# Registers via Cloudflare Registrar on the client's CF account. ~10 min wait for
# zone activation. Inserts the new row into infra_domains with client_id.
```

Zone status values: `pending` (nameservers not pointed to CF yet) | `active` (aging).

## Bison Workspaces
```
GET /api/bison/workspaces[?client_slug=<slug>]
  → {"workspaces":[{id, client_id, workspace_name, workspace_id, base_url,
                   purpose, is_default, status, created_at}, ...]}

POST /api/bison/workspaces
  body: {"client_slug": "...", "api_key": "<bison per-workspace key>", "purpose": "cold_outbound"}
  → {"workspace": {id, client_id, workspace_name, workspace_id, is_default: false}}
# Validates the key against Bison, stores it in Supabase Vault, writes the row.

DELETE /api/bison/workspaces/{workspace_id} → {"ok": true}
# Removes the row. Leaves the Vault secret in place — vacuum manually if needed.

PUT /api/bison/workspaces/{workspace_id}/default → {"ok": true}
# Sets this workspace as the client's default; demotes the prior default.
```

## Jobs
```
GET /api/jobs[?client_slug=<slug>&limit=50]
  → {"jobs":[{id, client_id, type, domain, status, progress_step, progress_total,
              logs, error, created_by, completed_at, created_at}, ...]}

GET /api/jobs/{job_id} → {"job": {...full row including logs[] }}
```

Job types: `deploy`, `verify`, `destroy`, `load_bison`, `domain_sync`, `domain_register`.
Job status: `pending`, `running`, `completed`, `failed`.

## Error responses

All errors are JSON `{"detail": "<message>"}` with HTTP status:
- `401` — invalid bearer token
- `404` — client/shard/job/workspace not found
- `409` — domain ambiguity (multiple clients share it) or workspace already exists for client
- `400` — Bison key validation failed / invalid request shape
- `500` — server error (check container logs: `docker logs coldemail-api-v2`)

## Quick curl recipes

```bash
source ~/.claude/skills/cold-email-infra/.env

# List clients
curl -s -H "Authorization: Bearer $INFRA_API_KEY" "$INFRA_API_BASE/api/clients" | jq

# Deploy
curl -s -X POST -H "Authorization: Bearer $INFRA_API_KEY" -H "Content-Type: application/json" \
  "$INFRA_API_BASE/api/deploy" \
  -d '{"client_slug":"10x-managers","domain":"newshard.com"}' | jq

# Check + register a domain
curl -s -X POST -H "Authorization: Bearer $INFRA_API_KEY" -H "Content-Type: application/json" \
  "$INFRA_API_BASE/api/domains/check" \
  -d '{"client_slug":"10x-managers","domain":"newshard.com"}' | jq

curl -s -X POST -H "Authorization: Bearer $INFRA_API_KEY" -H "Content-Type: application/json" \
  "$INFRA_API_BASE/api/domains/register" \
  -d '{"client_slug":"10x-managers","domain":"newshard.com"}' | jq

# Watch a job
curl -s -H "Authorization: Bearer $INFRA_API_KEY" \
  "$INFRA_API_BASE/api/jobs/<job_id>" | jq '.job.logs[-5:]'
```
