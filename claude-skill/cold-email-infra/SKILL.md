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

## Email Bison variable conventions (canonical)

When writing or uploading any campaign copy to Email Bison (via API, MCP, or paste), use **single curly braces with SCREAMING_SNAKE_CASE** for personalisation variables. Bison's spintax uses the same single-curly syntax but is distinguished by the presence of a pipe (`|`). Bison's parser handles both correctly when this convention is followed.

**Standard built-in variables:**

| Variable | Resolves to |
|---|---|
| `{FIRST_NAME}` | Lead first name |
| `{LAST_NAME}` | Lead last name |
| `{EMAIL}` | Lead email address |
| `{TITLE}` | Lead job title |
| `{COMPANY}` | Lead company name (**not** `{COMPANY_NAME}` — Bison uses `{COMPANY}`) |
| `{SENDER_FIRST_NAME}` | Sending mailbox first name |
| `{SENDER_FULL_NAME}` | Sending mailbox full name |
| `{SENDER_EMAIL_SIGNATURE}` | Pre-configured sender signature block |

**Never use:**
- `{{first_name}}` / `{{company_name}}` (double-curly snake_case — that's the Instantly/Smartlead/Lemlist convention, not Bison)
- `{COMPANY_NAME}` — Bison's variable is `{COMPANY}`, no `_NAME` suffix
- `{first_name}` (lowercase) — Bison expects UPPERCASE

**Custom variables** beyond this list are per-client/workspace and must be confirmed before use. Ask the user or check the workspace's lead schema via `mcp__emailbison__list_leads` before referencing a custom variable.

**Spintax vs variables in Bison:**
- Spintax: `{option1|option2|option3}` — has pipe, no underscores in the option keys
- Variable: `{FIRST_NAME}` — UPPERCASE identifier, no pipe
- Nested is fine: `{Hi {FIRST_NAME}|Hey {FIRST_NAME}|Quick one, {FIRST_NAME}}` — Bison parses spintax first (recognises the pipe), then resolves the variable inside the chosen option

## Authoritative facts (as of last session)

- **Three clients exist today**:
  - `10x-managers` — active, 18 verified shards (legacy fixed-20-subdomain layout), multi-persona signatures, on its own Webdock account, uses the 10x-managers Cloudflare account.
  - `reachos` — active, 2 shards (`reachosdfy.com`, `tryreachos.com`), **single-persona mailbox mode** (every mailbox is "Josh Gain"), founder-led HTML signature formula, on its own Webdock account, uses the 10x-managers Cloudflare account for legacy reasons.
  - `scouted` — active, 0 shards yet, 19 domains aging (registered 2026-05-20, deploy-ready ~2026-06-03), multi-persona signatures with tech-sales-recruitment titles, on its own Webdock account, uses the ReachOS Agency Cloudflare account.
- **Two Cloudflare accounts**, both with payment + Address Book set up:
  - `10x-managers` (account_id `07e00a96…c7e`): hosts 10X brand + ReachOS brand domains.
  - `reachos` (account_id `f1b3b7ad…6cfd`): hosts external agency clients (currently Scouted's 19 domains; future external clients).
- **Variable shard topology** (since 2026-05-21): each new shard random-samples 6-8 subdomains from the client's `client_settings.subdomain_pool` and distributes 5-15 mailboxes per subdomain to total `mailbox_count` (default 100). No two shards have an identical layout. Existing 10X shards still on the fixed-20-subdomain layout from before — only new deploys get the new topology.
- **Single-persona mode** (ReachOS): when `client_settings.mailbox_local_parts` is set, every mailbox uses the same `mailbox_display_first_name + last_name` and an alias from the pool. Math constraint: `mailbox_count ≤ n_subs × len(local_parts)` (ReachOS: 10 aliases × 6-8 subs = 60-80 per shard, not 100).
- **MTA-STS + TLS-RPT + IPv6** are configured on every new shard since 2026-05-21 (DNS records + Caddy on the mail VPS, AAAA records, `ip6:` in SPF). MTA-STS starts in `mode: testing` — promote to `mode: enforce` after 2-4 weeks per shard via Caddyfile edit + systemctl reload.
- **One control VPS, one image** runs everything (`infraapi1` at 193.180.211.74). New clients do NOT each get a control VPS — they share the API container. Only their mailserver VPSes are separate.
- **Webdock = mailserver VPSes, one per shard.** Each client has their own Webdock account, so a 400 from Webdock on a deploy is per-client (not global).
- **Signature formulas are per-client, with optional per-workspace overrides.** Default plan for any new client is `vps-epyc-advanced-2025` (€4.30/mo). Em dashes are blocked at three layers (schema CHECK + generator + PATCH).

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
- **NEVER deploy a shard on a client's main brand domain.** Burning the brand domain's reputation is irreversible. Before any `deploy` call, verify the target domain is NOT equal to `clients.website_url` (or a parent thereof). See `workflows.md` → "Brand domain protection" for the current known main-brand domains per client.
- **No em dashes in cold-email content, ever.** The signature_formulas table has CHECK constraints that reject inserts containing `—`, and the generator + PATCH boundary both call `_sanitize_signature()`. If you ever construct an outbound email body, subject, or signature outside the existing code paths, run it through `_sanitize_signature()` first.
- **Deploys are idempotent — when one fails, retry the same `deploy <client> <domain>`, don't destroy first.** The control VPS's per-shard state file (`/home/admin/ColdEmailInfra/shards/<domain>.json`) records which steps completed. Re-running `deploy` skips done steps and resumes at the failed one. No re-spending on Webdock, no double-registration on CF. Destroy only when you genuinely want the shard gone.
- **Confirm provider-account prerequisites before any new-client work** (`workflows.md` → "0. Provider-account prerequisites"). Two specific things have eaten 15-30 min each historically: (a) new Cloudflare accounts need a default Address Book entry + payment method before `/domains/register` works, (b) new Webdock accounts need ~€20 Service Credit before `/deploy` works. Both fail silently with misleading errors if missed.

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
