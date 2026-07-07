# Cold Email Infrastructure

Self-hosted cold email sending fleet. One shard = one VPS + one domain + 20 subdomains + 100 mailboxes. Exported to Email Bison for sending.

Hands-off after first-time setup: the deploy command takes a single `--domain` argument, buys the domain via Cloudflare Registrar if you don't already own it, discovers the zone, provisions everything.

## Architecture (per shard)

```
1 root domain (bought or existing on Cloudflare)
  └── 20 auto-generated subdomains
        └── 5 mailboxes each (first.last@sub.domain, British female names)
              = 100 mailboxes per shard

1 Contabo VPS (port 25 open by default, cold email supported)
  └── docker-mailserver (Postfix + Dovecot + OpenDKIM + OpenDMARC)
        └── hosts all 20 subdomains as virtual domains
```

## First-time setup (do once)

### 1. Cloudflare API token (scoped to your whole account, not one zone)

1. https://dash.cloudflare.com/profile/api-tokens → **Create Token** → **Custom token**
2. Permissions:
   - `Zone` → `DNS` → `Edit`
   - `Zone` → `Zone` → `Read`
   - `Account` → `Account Rulesets` → `Edit`
   - `Account` → `Cloudflare Registrar` → `Edit`
3. **Account Resources**: `Include` → `<your account>`
4. **Zone Resources**: `Include` → `All zones from an account` → `<your account>`
5. Create and copy the token. **One token, every future domain.**
6. Grab your **Account ID** from any domain's overview page (right sidebar).

### 2. Contabo account + API credentials

1. Sign up at https://contabo.com and add a payment method. You will not buy a VPS in the UI — the script does that via API.
2. Go to https://new.contabo.com/account/security → **API** section. Copy the `Client ID` and `Client Secret` (or regenerate if you haven't yet — that's fine, do it once, save the values).
3. Contabo's API uses OAuth2 password grant, so the other two values are just your **portal login email** and **portal login password**. That's it — no separate "API user" to create.
4. You'll end up with four values total:
   - `CONTABO_CLIENT_ID` ← from the API security page
   - `CONTABO_CLIENT_SECRET` ← from the API security page
   - `CONTABO_API_USER` ← your Contabo login email
   - `CONTABO_API_PASSWORD` ← your Contabo login password
5. Port 25 is open by default. Contabo's only technical limit is 25 emails/min, far above our volume (~0.7/min peak).
6. **Product ID.** `.env.example` defaults to `CONTABO_PRODUCT_ID=V91` (current cheapest Cloud VPS, ~4GB RAM). If you get a `Product not available` error on deploy, Contabo's catalog has shifted — check https://contabo.com/en/vps/ for the current product lineup (IDs are in the V91–V107 range in 2025+), update `CONTABO_PRODUCT_ID=` in `.env`, and re-run.

### 3. Local environment

```bash
git clone <this-repo> && cd ColdEmailInfra
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in the token, account ID, and Contabo key
ssh-keygen -t ed25519 -C "coldemail" -f ~/.ssh/id_ed25519 -N ""   # if you don't have one
```

## Deploy a shard (per domain, after first-time setup)

```bash
./scripts/deploy_shard.py --domain example.co.uk
```

That's it. The script:

1. Checks if the domain is already on Cloudflare. If not, checks availability via the CF Registrar API, shows the price, and (with your confirmation) registers it at cost.
2. Generates 20 subdomains + 100 mailboxes.
3. Provisions a Contabo VPS and sets PTR to `mail.<domain>`.
4. Writes 123 DNS records (A/MX/SPF/DMARC/DKIM/redirect) via Cloudflare API.
5. Installs docker-mailserver, creates mailboxes, generates 2048-bit DKIM keys, publishes them.
6. Exports `shards/example.co.uk_bison.csv` for Email Bison import.

Flags:
- `--yes` — skip the purchase confirmation prompt (use when scripting).
- `--skip-purchase` — fail fast if the domain isn't already on Cloudflare (safety for when you don't want accidental purchases).

Idempotent — re-run on any failure, it resumes from the last completed step (`shards/<domain>.json` tracks progress).

## Verify

```bash
./scripts/verify_shard.py --domain example.co.uk
```

Checks DNS propagation, FCrDNS alignment (PTR + HELO + forward A), SMTP banner, TLS cert. Prints manual Bison-test instructions.

## Export → Email Bison

After a deploy, `shards/example.co.uk_bison.csv` has Bison's exact headers:

```
Name, Email, Password, IMAP Server, IMAP Port, SMTP Server, SMTP Port, Daily Limit, SMTP Secure, IMAP Secure
```

Defaults: SMTP 465/SSL, IMAP 993/SSL, Daily Limit 10.

## Destroy a shard

```bash
./scripts/destroy_shard.py --domain example.co.uk
```

Deletes the VPS, removes all DNS records for that zone's subdomains, archives the state file. Does NOT un-register the domain at Cloudflare Registrar (that's a manual step — register/unregister decisions should be deliberate).

## Landing pages on sending-domain roots

Opt-in per client via `client_settings.landing_page_url` (see
`migrations/2026-07-07_landing_page_url.sql`). Example value:
`https://hello.10xmanagers.com/outreach`.

When set:

- The deploy pipeline creates the apex A record **unproxied** (grey cloud)
  and skips the Cloudflare redirect rule. Caddy on the shard VPS terminates
  TLS for `https://<root>` with its own Let's Encrypt cert, the same pattern
  as the `mta-sts.<root>` vhost.
- A `setup_landing` deploy step (right after MTA-STS install) appends a
  marker-delimited vhost block to `/etc/caddy/Caddyfile` that reverse-proxies
  the landing origin. A small allowlist of framework asset paths passes
  through untouched; **every other path is rewritten to the landing path**,
  so the client's full site is never browsable on a sending domain.
- No `www` record, and no changes to sender subdomains, MX, TXT, MTA-STS or
  DKIM.

When NULL (ReachOS, Scouted, etc.) the legacy behaviour is unchanged:
proxied apex plus a 301 redirect rule to `client_settings.redirect_url`.

Retrofit existing live shards (bison_loaded=true) with:

```bash
python scripts/retrofit_landing_page.py --dry-run          # print intent
python scripts/retrofit_landing_page.py --client 10x-managers
python scripts/retrofit_landing_page.py --domain get10xleaders.com
python scripts/retrofit_landing_page.py                    # whole eligible fleet
```

The retrofit flips the apex to grey-cloud, installs the Caddy vhost over
SSH, tries to delete the old redirect rule (tolerated if the token lacks
ruleset permission - the rule is unreachable once the apex is grey-cloud),
verifies `https://<root>/` serves the landing page, and marks
`landing_page_configured` in the shard state plus `step_flags.landing_page`
in `infra_shards`. Re-runs are no-ops. On destroy, nothing extra is needed:
the apex record goes with the zone wipe and the Caddy vhost dies with the
VPS.

## Runbook notes

- **Warmup**: first two weeks post-deploy, keep Bison's warmup at 2–5/inbox/day, then ramp to 10.
- **Spam complaint ceiling**: Gmail/Yahoo cut off at 0.3%. List-hygiene concern, not infra.
- **One-click unsubscribe**: Bison must include `List-Unsubscribe` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click` in outgoing mail. Manual header spot-check at go-live.
- **If a shard gets flagged**: destroy it. Reputation is shard-isolated — don't try to rehabilitate.
