# Cold email infra — common workflows

These are the long, multi-step flows. Walk through them step-by-step with the user, asking questions where marked.

---

## 0. Provider-account prerequisites (one-time per account, easy to miss)

Before any new-client work, confirm the relevant provider accounts have these set up — every one of them is a "silent failure" trigger that wastes 15+ min of debugging if missing.

### Cloudflare account (if it's a fresh CF account — not needed for 10x-managers CF)

- ✅ **Default Address Book entry** at Manage Account → Domain Registration → Address Book → "Set as default". Without it, `/domains/register` fails with CF code 10000 `"No registrant contact provided and no default address book entry found"`.
- ✅ **Payment method on file** at Manage Account → Billing → Payment Methods. Without it, `/domains/register` looks like it succeeds but CF silently marks the registration `state: failed` with `error.code: billing_quote_failed`. Our `wait_for_zone` then times out at 20 min showing a misleading `TimeoutError: did not become active`.
- Verify both before bulk-registering any domains.

### Webdock account (every new client gets their own)

- ✅ **Service Credit topped up to ~€20** at Settings → Billing → Add Funds. New Webdock accounts default to **prepaid** mode — even with a card on file, they won't auto-charge. Without credit, `/deploy` fails at step 2 with Webdock 400 `"Payment failed during server creation"`.
- ✅ (Optional, recommended) **Auto-recharge** enabled at Settings → Billing so the account refills when the balance drops below ~€5.
- Established accounts (months of clean use) can be moved to post-paid by Webdock support, but expect prepaid as the default for any new account.

### Cloudflare Registrar API rate limit (operational)

CF Registrar caps at roughly 5 POSTs per minute per account. **Use ≥15s pacing** between `domains/register` calls if bulk-registering. The skill's `purchase` flow already paces correctly, but homegrown bulk scripts must too.

---

## 1. Onboard a new client

**Triggers**: "onboard <client>", "add a new cold email client", "set up <name> for cold outbound"

Total time: ~15–30 min (most of it is the user fetching API keys from Webdock/Bison)

**Before starting**: confirm the prerequisites in Workflow 0 are done for whichever Cloudflare account this client will use AND the new Webdock account they're creating. Skipping these costs 15–30 min of debugging later.

### Step 1 — Identity (you ask, user answers)

- "What's the client's name (full company name)?"
- "What slug should we use? (short, lowercase, hyphens — e.g. `acme-co`)"
- "What's their main website URL? (we'll use a default of `https://<website>` as the redirect target for cold email domains)"

Validate the slug isn't already taken:
```bash
curl -s -H "Authorization: Bearer $INFRA_API_KEY" "$INFRA_API_BASE/api/clients" | jq -r '.clients[].slug'
```

### Step 2 — Cloudflare account choice

Ask: "Will this client use the existing ReachOS agency Cloudflare account (default for external clients), or do they need their own?"

- If ReachOS shared (default): use `cloudflare_accounts.slug = 'reachos'`. **Confirm `account_id` is set first** — query `SELECT account_id FROM cloudflare_accounts WHERE slug='reachos'`. If null, ask the user for the CF account ID and update it.
- If their own: ask for a Cloudflare API token (with Zone + Registrar perms) and the CF account ID, then insert a new `cloudflare_accounts` row (with token stored in vault via `create_vault_secret` RPC).

### Step 3 — Webdock account

Ask: "Have you created a Webdock account for this client?"

- If no: walk them through the signup at https://webdock.io. Tell them to create the account, fund it with at least the cost of one VPS plan, and create an API token under Account → API Tokens.
- Once they have a token: store it in Vault via the SQL function, then create the `client_credentials` row.

```sql
-- Run via Supabase MCP (project_id: jiwsuukvnaazwytrymjr)
DO $$
DECLARE
  v_client_id uuid;
  v_secret_id uuid;
BEGIN
  -- Replace 'acme-co' and the token below
  SELECT id INTO v_client_id FROM clients WHERE slug = 'acme-co';
  SELECT public.create_vault_secret('<webdock_token>', 'webdock_acme_co') INTO v_secret_id;
  INSERT INTO client_credentials (client_id, webdock_api_token_secret_id, webdock_account_email)
  VALUES (v_client_id, v_secret_id, '<account_email>');
END $$;
```

### Step 4 — Bison workspaces

Ask: "How many Bison workspaces does this client need to start? (1 is fine — they can add more later via `add-workspace`)"

For each workspace, ask for the per-workspace API key from Bison (Settings → API Keys in the workspace). Then call the API to register and validate:

```bash
curl -s -X POST -H "Authorization: Bearer $INFRA_API_KEY" -H "Content-Type: application/json" \
  "$INFRA_API_BASE/api/bison/workspaces" \
  -d '{"client_slug":"acme-co","api_key":"<bison_key>","purpose":"cold_outbound"}'
```

Then mark one as default:
```bash
WS_ID=<from-response>
curl -s -X PUT -H "Authorization: Bearer $INFRA_API_KEY" \
  "$INFRA_API_BASE/api/bison/workspaces/$WS_ID/default"
```

### Step 5 — Deploy settings

Ask each (with sensible defaults):
- VPS region — default `dk` (EU). Alternatives: `nl`, `us`, `sg`.
- VPS plan slug — default `webdocknano` (entry-level). For higher volume use `vps-epyc-advanced-2025` (what 10X uses).
- Mailbox count per shard — default 100.
- Subdomain count per shard — default 20.
- Daily send limit per mailbox — default 10.
- Redirect URL — defaults to the client's website (cold email domains 301 here for branding).
- DMARC RUA email — where DMARC reports go.
- Let's Encrypt email — where cert renewal notifications go.

These all live in `client_settings`. The skill needs an SQL insert (no API endpoint yet):

```sql
INSERT INTO client_settings (
  client_id, vps_region, vps_plan, vps_image_slug, mailbox_count, subdomain_count,
  redirect_url, default_daily_limit, dmarc_rua, le_email, ssl_type
)
SELECT
  id, 'dk', 'webdocknano', 'webdock-ubuntu-jammy-cloud', 100, 20,
  '<redirect_url>', 10, '<dmarc_rua>', '<le_email>', 'letsencrypt'
FROM clients WHERE slug = 'acme-co';
```

### Step 5b — Subdomain pool (NEW — relevant per-business names)

Each shard random-samples 6-8 subdomains from a per-client pool of ~20-25
business-relevant names. This breaks the Smartlead/Instantly fingerprint
where every shard uses the same `hello/hi/contact/mail/team/...` set.

Interview the user:
- "What's the natural vocabulary around your client's product/service?"
- Brainstorm 20-25 plausible subdomain words that fit. Mix:
  - Direct product terms (recruitment, candidates, employers, app, platform)
  - Action words (hire, connect, intro, demo, meet, reach, find)
  - Departmental (sales, talent, careers, partnerships)
  - Generic-but-on-brand fillers (hi, hello, team, growth, direct)

Example pools we've used:
- 10X Managers (leadership training): leadership, managers, develop, team,
  programmes, learning, growth, talent, lab, mentorship, coaching, community,
  performance, strategy, executive, board, partner, direct, meet, connect,
  lead, reach, talk, inbox, journey, grow, culture, careers
- Scouted (tech sales recruitment): recruitment, candidates, employers,
  partnerships, talent, careers, opportunities, hire, roles, connect, intro,
  talk, meet, partners, growth, match, place, network, team, pipeline,
  sales, reach, find, top-talent, direct, inbox
- ReachOS (founder-led SaaS): hi, hello, hey, founder, build, team, app,
  try, get, demo, meet, chat, talk, intro, direct, inbox, start, growth,
  from, at, platform, outbound, outreach, reply, reachout

```sql
UPDATE client_settings
SET subdomain_pool = ARRAY[ /* the 20-25 names */ ]
WHERE client_id = (SELECT id FROM clients WHERE slug = '...');
```

The deploy code will random-sample 6-8 of these per shard (and use a varied
5-15 mailboxes per subdomain summing to mailbox_count), so two shards from
the same client never have an identical layout.

**Constraint for single-persona clients:** the mailbox total is capped at
`n_subdomains * len(mailbox_local_parts)`. ReachOS with 10 aliases × 8 subs
caps at 80 mailboxes/shard. Expand the local_parts list if you need 100.

### Step 6 — Signature formula (the conversational bit)

Use `signature-formula-help.md` for the interview pattern. You'll be drafting:
- Company name variants (e.g. ["Acme", "Acme Inc", "Acme Ltd"])
- 10–15 plausible job titles for the people sending these emails
- 4–8 quotes/POV statements that match the client's brand voice
- 12–18 opt-out lines (varied, natural)
- Rates: `include_pronouns_rate` (0.2–0.4), `include_quote_rate` (0.3–0.5), `include_email_rate` (0.3–0.5)

Then insert the row:

```sql
INSERT INTO signature_formulas (
  client_id, client_bison_workspace_id, style,
  company_names, titles, quotes, optouts,
  include_pronouns_rate, include_quote_rate, include_email_rate, format_variants
)
SELECT id, NULL, 'html',
  ARRAY[...],  -- company_names
  ARRAY[...],  -- titles
  ARRAY[...],  -- quotes
  ARRAY[...],  -- optouts
  0.3, 0.43, 0.4, 6
FROM clients WHERE slug = 'acme-co';
```

### Step 7 — First domains (optional, can defer)

Ask: "Want to purchase 5–10 starting domains now, or wait?"

If yes, jump into the "Purchase domains" workflow below.

### Step 8 — Finalise

Confirm everything is set up:

```bash
curl -s -H "Authorization: Bearer $INFRA_API_KEY" "$INFRA_API_BASE/api/clients" | jq '.clients[] | select(.slug=="acme-co")'
curl -s -H "Authorization: Bearer $INFRA_API_KEY" "$INFRA_API_BASE/api/bison/workspaces?client_slug=acme-co" | jq
```

Report back to the user: "Onboarded — slug `acme-co`, N workspaces, default `<name>`. Cloudflare account: `<cf_slug>`. Webdock token stored in Vault. First deploy ready when domains are aged."

---

## 2. Purchase domains

**Triggers**: "buy <N> domains for <client>", "purchase domains", "we need more domains"

### Step 1 — Brainstorm names

Take the client's website / brand (from `clients.website_url` or `client_settings.domain_naming_hints`) and generate 30–50 candidate domains using the patterns:
- `try-<brand>.com`, `use-<brand>.com`, `get-<brand>.com`, `<brand>-team.com`, `email-<brand>.com`, `<brand>-co.com`, `<brand>-hq.com`, `with-<brand>.com`, `the-<brand>.com`, `my-<brand>.com`, `one-<brand>.com`, `develop-<brand>.com`, `become-a-<brand>.com`, `<brand>-now.com`

Also include obvious variants: `<brand>.org`, `<brand>.co`, `<brand>.io`, `<brand>.uk`, `<brand>.co.uk`.

Show the list to the user and ask which they like before checking availability (saves CF API calls).

### Step 2 — Check availability

Loop through the candidates:

```bash
for d in "${candidates[@]}"; do
  curl -s -X POST -H "Authorization: Bearer $INFRA_API_KEY" -H "Content-Type: application/json" \
    "$INFRA_API_BASE/api/domains/check" \
    -d "{\"client_slug\":\"$CLIENT\",\"domain\":\"$d\"}"
  sleep 0.5  # gentle rate limiting on CF
done
```

Present a table to the user: domain | available | price. Filter to the available ones.

### Step 3 — Confirm cost + purchase

**Always confirm before purchase.** Show total: "$8.57 × 7 = $59.99. Proceed?"

If yes, register each:

```bash
for d in "${selected[@]}"; do
  curl -s -X POST -H "Authorization: Bearer $INFRA_API_KEY" -H "Content-Type: application/json" \
    "$INFRA_API_BASE/api/domains/register" \
    -d "{\"client_slug\":\"$CLIENT\",\"domain\":\"$d\"}" | jq -r .job_id
done
```

These each take ~10 min for the zone to activate. Don't poll all simultaneously — start them, then return to the user with the list of pending registrations. They can come back to deploy in 14 days.

### Step 4 — Report

"Started registration for 7 domains. They'll show as `pending` for ~10 min, then move to `active` once the zone activates. Domains need 14+ days of aging before you should deploy a shard on them — check `aging <client>` later this month to see what's ready."

---

## 3. Deploy a shard

**Triggers**: "deploy a shard for <client>", "deploy <domain>", "spin up a new shard"

### Step 1 — Pick the domain

If the user gave one, use it. Verify the domain is in `infra_domains` for that client with `zone_status = 'active'` and `registrar_created_at < now() - 14 days`:

```bash
curl -s -H "Authorization: Bearer $INFRA_API_KEY" "$INFRA_API_BASE/api/domains?client_slug=$CLIENT" \
  | jq -r '.domains[] | select(.zone_status=="active") | "\(.domain)\t\(.registrar_created_at)"'
```

If the domain isn't aged enough, warn: "Domain is only N days old — recommendation is 14+ days for deliverability. Deploy anyway, or pick a different domain?"

### Step 2 — Confirm deploy params

Show what's about to happen:
- Client: `<slug>` (Webdock account: `<email>`, CF account: `<cf_slug>`)
- Domain: `<domain>`
- VPS: `<vps_plan>` in `<vps_region>` (estimate £X/month based on plan)
- Mailboxes: `<mailbox_count>` across `<subdomain_count>` subdomains
- Redirect: `<redirect_url>`
- Signature formula: HTML, N titles, N quotes

Ask: "Proceed?"

### Step 3 — Kick it off + stream

```bash
JOB=$(curl -s -X POST -H "Authorization: Bearer $INFRA_API_KEY" -H "Content-Type: application/json" \
  "$INFRA_API_BASE/api/deploy" \
  -d "{\"client_slug\":\"$CLIENT\",\"domain\":\"$DOMAIN\"}" | jq -r .job_id)
echo "Job: $JOB"
```

Stream progress (see "Streaming job progress" in SKILL.md). The deploy has 10 steps and takes ~15 min total:
1. Ensure domain on Cloudflare (~1s)
2. Generate subdomains and mailboxes (~0.2s)
3. Provision VPS via Webdock (~1 min)
4. Set PTR (~0.3s)
5. Configure Cloudflare DNS (~1 min)
6. Install docker-mailserver (~12 min — the long step)
7. Create mailboxes (~25s)
8. Generate DKIM keys (~1 min)
9. Export Bison CSV (~1s)
10. Upload CSV to storage (~1s)

Update the user every ~2 min, not every log line. Phrase progress in user terms ("Mailserver installing — 8 min remaining" not "Step 6 of 10").

### Step 4 — On completion

If success: "Deploy complete. Shard active at `<vps_ip>` with 100 mailboxes across 20 subdomains. Next: `verify <domain>` to confirm DNS records propagated, then `load-bison <domain>` to push the senders into Bison."

If failure: surface the last 5 log lines and the `error` field, then suggest a recovery action based on the failure mode (see "Common failures" below).

---

## 4. Full deploy cycle (deploy + verify + load-bison)

**Triggers**: "full deploy <domain>", "deploy and load <domain>", "ship a new shard for <client>"

Chain the three operations:

1. **Deploy** (~15 min) — stream progress
2. Wait 2–3 min for DNS to propagate
3. **Verify** (~5s) — if any check fails, surface it and stop. Common: DKIM not yet propagated → wait 5 min and retry.
4. **Load to Bison** (~4 min) — uses the client's default workspace unless the user picked another with `--workspace`

Report the total elapsed time and final state.

---

## 5. Destroy a shard

**Triggers**: "destroy <domain>", "tear down the shard for <domain>", "kill <domain>"

### Step 1 — Show what's being torn down

```bash
curl -s -H "Authorization: Bearer $INFRA_API_KEY" "$INFRA_API_BASE/api/shards/$DOMAIN" | jq
```

Show: VPS IP, mailbox count, Bison workspace, when it was loaded.

### Step 2 — **Hard confirmation**

"This will permanently destroy the VPS, delete all DNS records, and orphan 100 Bison senders. The mailboxes won't be deletable later. Type 'destroy <domain>' to confirm."

Wait for an exact match before proceeding.

### Step 3 — Run + report

```bash
JOB=$(curl -s -X POST -H "Authorization: Bearer $INFRA_API_KEY" -H "Content-Type: application/json" \
  "$INFRA_API_BASE/api/destroy/$DOMAIN" -d '{}' | jq -r .job_id)
```

Job is fast (~2s). Report the final state.

---

## 6. Status report

**Triggers**: "status", "how's the infra looking", "weekly check-in"

Run in parallel, then synthesise:

```bash
# All clients
curl -s -H "Authorization: Bearer $INFRA_API_KEY" "$INFRA_API_BASE/api/clients" | jq

# Per client: shards, jobs, domains
for CLIENT in 10x-managers reachos; do
  echo "=== $CLIENT ==="
  curl -s -H "Authorization: Bearer $INFRA_API_KEY" "$INFRA_API_BASE/api/shards?client_slug=$CLIENT" | jq -r '.shards | length, ([.[] | .status] | group_by(.) | map([.[0], length]))'
done

# Recent failed jobs
curl -s -H "Authorization: Bearer $INFRA_API_KEY" "$INFRA_API_BASE/api/jobs?limit=50" | jq '.jobs | map(select(.status=="failed")) | length'
```

Format as a concise summary, not a data dump.

---

## 7. Reattribute orphan domains to the right client

**Triggers**: "we have N reachos-named domains on the 10X CF that should be under reachos", "domains attributed to wrong client", "I think there are more domains for client X"

When you add domains to a Cloudflare account that hosts multiple clients (e.g. the 10X CF account hosts both 10X Managers brand AND ReachOS brand domains), `domain_sync` initially attributes new zones to `default_client_slug_for_new` (defaults to the calling client). You then need to reattribute the brand-specific ones.

### Step 1 — Run a fresh sync

```bash
curl -s -X POST -H "Authorization: Bearer $INFRA_API_KEY" -H "Content-Type: application/json" \
  "$INFRA_API_BASE/api/domains/refresh" \
  -d '{"client_slug":"10x-managers","default_client_slug_for_new":"10x-managers"}'
```

This pulls ALL zones from the CF account into `infra_domains`. New ones land attributed to the default; existing ones keep their existing client_id.

### Step 2 — Identify by naming pattern

```sql
SELECT domain, c.slug AS attributed_to, zone_status, registrar_created_at
FROM infra_domains d JOIN clients c ON c.id = d.client_id
WHERE lower(domain) LIKE '%<client-name-fragment>%'
ORDER BY domain;
```

### Step 3 — Bulk reattribute

```sql
UPDATE infra_domains
SET client_id = (SELECT id FROM clients WHERE slug = '<target-client>')
WHERE lower(domain) LIKE '%<pattern>%'
  AND client_id = (SELECT id FROM clients WHERE slug = '<wrong-client>');
```

Always **eyeball the list first** before running the UPDATE — false positives are easy if the pattern is too loose. e.g. `'%reachos%'` would match `reachos.co` (their main website — don't burn it as a shard).

---

## Brand domain protection (always check before deploying)

When you have N domains for a client, one of them is often the **main brand website**. Deploying a cold-email shard on the brand domain would burn the brand's reputation. **Always verify the target domain is NOT in `clients.website_url`** before deploying.

| Client | Main brand domain — DO NOT use as a shard |
|---|---|
| 10x-managers | 10xmanagers.com (and `hello.10xmanagers.com` as redirect target) |
| reachos | reachos.co |
| scouted | scouted.tech |

The skill's `deploy <client> <domain>` flow should refuse to deploy on the website domain or warn loudly. If a user explicitly asks to deploy on their brand domain, ask twice and require explicit "yes, burn it" confirmation.

---

## Common failures and what to do

| Failure | Cause | Recovery |
|---|---|---|
| Deploy fails at step 3 with Webdock 400 — error mentions `"Payment failed during server creation"` | New Webdock account has no Service Credit OR card not enabled for auto-charge | User adds €20 Service Credit in Webdock Settings → Billing; re-run deploy (state file resumes from where it failed, no destroy needed) |
| Deploy fails at step 3 with Webdock 400 — error is generic "Bad Request" | Webdock token issue, profile slug deprecated, or account-level quota | Direct probe: `curl -H "Authorization: Bearer <token>" https://api.webdock.io/v1/profiles?locationId=dk` to check token + profile validity |
| Domain register fails with CF code 10000 `"No registrant contact provided..."` | CF account missing default Address Book entry | User adds one in CF dashboard → Manage Account → Domain Registration → Address Book → "Set as default" |
| Domain register fails with TimeoutError `did not become active within 20 minutes` AND CF dashboard shows nothing under Registrar → Domains | CF account missing payment method — `billing_quote_failed`. CF accepts the POST then silently fails the registration. | User adds payment method in CF Manage Account → Billing → Payment Methods. Verify via `curl /accounts/{id}/registrar/registrations/{domain}/registration-status` — look for `state: failed, error.code: billing_quote_failed`. Then re-fire the registration. |
| Bulk domain register: many 429s after the first 5-7 | CF Registrar API rate limit (~5 POST/min per account) | Use ≥15s pacing between calls; the skill's `purchase` flow already does this. Re-fire the failed ones. |
| Verify TimeoutError "did not become active" on a `domain_register` job, but CF dashboard SHOWS the domain | CF zone activation slower than our 20-min `wait_for_zone` timeout. Registration succeeded, just slow. | Run `domains refresh` for that client — the sync will pick the new zone up as active. No re-register needed. |
| Deploy fails at step 6 (mailserver install) | SSH key not on VPS, or VPS not ready yet | Re-run deploy — the state file lets it resume |
| Deploy fails inside `install_mta_sts` with `Could not open lock file /var/lib/dpkg/lock-frontend, are you root?` | Multi-command sudo bug — chained `&&` in `self.sudo()` only elevates the first command | Fix in code: split into separate `self.sudo()` calls OR wrap in `self.sudo("sh -c '...'")`. Then retry deploy (state file resumes). |
| Verify fails on DKIM | DNS not propagated yet | Wait 5–10 min, re-run verify |
| Verify fails on PTR | Webdock didn't accept the rDNS request | Check via SSH to VPS; manually set in Webdock dashboard if needed |
| Load-bison fails "CSV not found" | Shard wasn't deployed cleanly OR `shard-csvs` Storage bucket missing in the Supabase project | `SELECT * FROM storage.buckets WHERE id='shard-csvs'` — if empty, create it (private, text/csv, 10MB). For an existing shard with `csv_storage_path=NULL`, re-run deploy (it resumes at step 9). |
| Load-bison PATCH calls return HTTP 422 `"The daily limit field is required"` | Bison's PATCH /api/sender-emails/{id} requires `daily_limit` alongside `email_signature` | Include both fields in the PATCH body. The skill's load-bison code already does this; ad-hoc cleanup scripts often miss it. |
| Webdock dashboard shows the VPS but `GET /v1/servers` returns `[]` with the same token | Account-tier visibility quirk — token can create but not list under certain account configurations | Functional impact: none for verify/load-bison; possible problem for destroy. Workaround: use the dashboard's destroy. Worth investigating token scopes in Webdock UI but not blocking. |
| Cleanup script ran but signatures still show em dashes | Likely failed at HTTP 422 (missing daily_limit) — script reports 0 patched, N errors | Read the script's error logs. Include `daily_limit` from the existing sender object in every PATCH payload. |
| `Invalid API key` on every call | INFRA_API_KEY mismatch or .env not loaded | `source ~/.claude/skills/cold-email-infra/.env` first |
