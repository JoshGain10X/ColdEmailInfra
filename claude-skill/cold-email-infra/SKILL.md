---
name: cold-email-infra
description: Manages cold email infrastructure across multiple clients — buying domains via Cloudflare Registrar, deploying VPS-backed mail shards via Webdock, verifying DNS/DKIM/SMTP, loading mailboxes into Email Bison workspaces, and tearing down shards when done. Use when the user wants to onboard a new cold email client, buy/purchase domains for cold outreach, check domain aging, deploy or destroy a shard, verify a deployed shard, load mailboxes into Bison, check shard/job/domain status, or report costs. Triggers on "onboard <client>", "buy domains", "purchase domains", "deploy a shard", "deploy shard for <client>", "destroy shard", "verify shard", "load to bison", "shard status", "domain inventory", "cold email infra", "add new client".
allowed-tools: [Bash, Read, Write, Edit, AskUserQuestion, WebFetch, mcp__claude_ai_Supabase__execute_sql, mcp__claude_ai_Supabase__list_tables]
---

# Cold Email Infrastructure skill

Wraps the v2 multi-tenant ColdEmailInfra API to manage cold email infrastructure across an agency-style client portfolio. Every operation is scoped to a specific client; the API resolves their Webdock account, Cloudflare account, Bison workspaces, signature formula, and deploy defaults from the Cold Email Data Supabase project.

## Resources

- **API base URL & key**: read from `~/.claude/skills/cold-email-infra/.env` (`INFRA_API_BASE`, `INFRA_API_KEY`). Always use `curl` with the bearer header — never echo the key in your responses.
- **Cold Email Data Supabase project** (project ID `jiwsuukvnaazwytrymjr`): the source of truth for clients, credentials, settings, signature formulas. Use the Supabase MCP for queries that the API doesn't expose (e.g. raw `client_settings` inspection, `signature_formulas` editing).
- **Backing API source**: https://github.com/JoshGain10X/ColdEmailInfra, branch `multi-tenant-v2`. The v2 container runs at `infra-api-v2.10xmanagers.com` on the control VPS `193.180.211.74`.
- **Reference files** (read on demand):
  - `api-reference.md` — every endpoint, request/response shape, error codes
  - `workflows.md` — full conversational walkthroughs for the big workflows (onboard, weekly add, teardown)
  - `signature-formula-help.md` — how to draft a per-client signature pool at onboarding

## Authoritative facts

- **Two clients exist today**: `10x-managers` (active, 21 shards) and `reachos` (active, 0 shards, uses 10X's Cloudflare account for legacy reasons)
- **Two Cloudflare accounts**: `10x-managers` (hosts 10X + ReachOS brand domains) and `reachos` (hosts all other agency clients — its `account_id` is currently null; ask the user when first needed)
- **One control VPS, one image** runs everything. New clients do NOT each get a control VPS — they get their own Webdock + Bison + (sometimes) Cloudflare account, but the API container is shared.
- **Webdock = mailserver VPSes, one per shard.** Each client has their own Webdock account, so a 400 from Webdock on a deploy is per-client (not global).
- **Signature formulas are per-client, with per-workspace overrides.** ReachOS workspace uses a plaintext override; 10X's other workspaces use the HTML default.

## How to invoke the API

Always load the config first (the key and base URL change between local dev / VPS / future clones):

```bash
source ~/.claude/skills/cold-email-infra/.env
# Now $INFRA_API_BASE and $INFRA_API_KEY are set
```

Every call uses bearer auth:

```bash
curl -s -H "Authorization: Bearer $INFRA_API_KEY" "$INFRA_API_BASE/api/clients" | python3 -m json.tool
```

When constructing POST bodies, always pass `client_slug` for create-type endpoints (`/api/deploy`, `/api/domains/register`, `/api/domains/check`, `/api/domains/refresh`, `/api/bison/workspaces`). For domain-keyed routes (`/api/destroy/{domain}`, `/api/verify/{domain}`, `/api/load-to-bison/{domain}`) the client is inferred from the shard row; pass `client_slug` in the body only to disambiguate.

## Sub-commands

These are conversational shorthand, not strict syntax. When the user invokes one, follow the matching workflow.

| Shorthand | What you do |
|---|---|
| `clients` | `GET /api/clients` → table of slug, name, status |
| `onboard <name>` | Multi-step interactive workflow — see `workflows.md` → "Onboard a new client" |
| `domains [<client>]` | `GET /api/domains?client_slug=...` → list with age, zone status |
| `purchase <client> <count>` | Brainstorm names → `POST /api/domains/check` each → present available + price → `POST /api/domains/register` after confirmation. See `workflows.md` → "Purchase domains" |
| `aging <client>` | Filter `infra_domains` by `client_id` + `registrar_created_at < now() - INTERVAL '14 days'` (via Supabase SQL). Domains aged 14+ days are deploy-ready |
| `deploy <client> <domain>` | `POST /api/deploy` with `client_slug`, `domain`. Then poll `GET /api/jobs/{job_id}` every 30s and stream `logs[]` to chat. ~15min total. See `workflows.md` → "Deploy a shard" |
| `verify <domain>` | `POST /api/verify/{domain}`. Polls the job, streams. Quick (~5s) |
| `load-bison <domain> [--workspace W]` | `POST /api/load-to-bison/{domain}` with optional `workspace` name in body. ~4 min for 100 mailboxes |
| `destroy <client> <domain>` | **Always confirm with the user first** (high blast radius). Then `POST /api/destroy/{domain}` |
| `shards [<client>]` | `GET /api/shards?client_slug=...` → table with status, vps_ip, mailbox_count, bison_workspace |
| `jobs [<client>]` | `GET /api/jobs?client_slug=...&limit=20` → recent jobs |
| `job <job_id>` | `GET /api/jobs/{job_id}` → full log stream |
| `repair <domain>` | Re-runs the failed step. Today the API doesn't expose a per-step retry — you re-run the higher-level op (deploy/verify/load-bison) and the underlying script picks up where the state file left off |
| `workspaces [<client>]` | `GET /api/bison/workspaces?client_slug=...` |
| `add-workspace <client>` | Prompt for Bison API key → `POST /api/bison/workspaces` — validates against Bison API and stores key in Supabase Vault |
| `status [<client>]` | Composite report: total shards by status, recent failed jobs, domains pending aging, workspaces |
| `costs <client>` | Webdock plan × shard count + domain count × renewal price + mailbox count. Pull `vps_plan` from `client_settings` to know the Webdock cost |

## Streaming job progress

When the user kicks off a long-running job (deploy, verify, destroy, load-bison, domain-register), poll the job and stream incrementally:

```bash
# Kick off — get job_id from response
JOB=$(curl -s -X POST -H "Authorization: Bearer $INFRA_API_KEY" -H "Content-Type: application/json" \
  "$INFRA_API_BASE/api/deploy" -d '{"client_slug":"...","domain":"..."}' | jq -r .job_id)

# Poll every 30s. Print new log lines as they appear.
LAST_LOG_COUNT=0
while true; do
  J=$(curl -s -H "Authorization: Bearer $INFRA_API_KEY" "$INFRA_API_BASE/api/jobs/$JOB")
  echo "$J" | jq -r ".job.logs[$LAST_LOG_COUNT:][] | \"[\(.ts)] \(.msg)\""
  LAST_LOG_COUNT=$(echo "$J" | jq '.job.logs | length')
  STATUS=$(echo "$J" | jq -r .job.status)
  if [ "$STATUS" != "running" ] && [ "$STATUS" != "pending" ]; then break; fi
  sleep 30
done
```

Update the user with a summary every 2–3 log lines, not every line. Don't spam the chat.

## Critical safety rules

- **Always confirm before `destroy`.** Show what will be torn down (VPS IP, mailbox count, Bison workspace) and require an explicit "yes" before calling the endpoint.
- **Always confirm before bulk domain purchase.** Show total cost (count × price) and ask before any `POST /api/domains/register`.
- **Never paste API keys, Supabase service keys, Vault secrets, or Cloudflare/Webdock tokens into chat.** If the user pastes one, store it (via `vault.create_secret` for client credentials, or via the `.env` file for the skill's own key) and confirm with just a length/fingerprint check ("stored a 64-char hex key").
- **`destroy` failures**: if VPS destruction warns but DNS cleanup succeeded, the shard is still marked destroyed. That's intentional — the orphan Webdock VPS will get billed though, so always check Webdock dashboard after a warn.

## When to use Supabase SQL directly

The API covers the operational surface. Use Supabase MCP (`mcp__claude_ai_Supabase__execute_sql`) against project `jiwsuukvnaazwytrymjr` for:

- Reading/editing `signature_formulas` (no API endpoint yet)
- Reading/editing `client_settings` rows (no API endpoint yet)
- Diagnosing cross-table issues (e.g. orphaned vault secrets, shards with NULL `client_bison_workspace_id`)
- Setting `cloudflare_accounts.account_id` when a new CF account is added
- Listing `vault.secrets` to check what credentials exist (use `SELECT name FROM vault.secrets`, never `decrypted_secret` unless decrypting a specific known UUID via `public.get_decrypted_secret(uuid)`)

## When you're unsure

- Read `api-reference.md` for the exact request shape before constructing a call.
- Read `workflows.md` for the long workflows (onboarding, full deploy cycle).
- If a job fails with a Webdock 400, check Webdock dashboard credentials before trying again — that error has historically meant a token issue, not a transient API hiccup.
- If `get_decrypted_secret` returns null, the secret_id on the row is stale — re-create the secret via `create_vault_secret` RPC and re-link.
