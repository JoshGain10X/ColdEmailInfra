# Cold Email Infrastructure

Self-hosted cold email sending fleet. One shard = one VPS + one domain + 20 subdomains + 100 mailboxes. Exported to Email Bison for sending.

Hands-off after first-time setup: the deploy command takes a single `--domain` argument, buys the domain via Cloudflare Registrar if you don't already own it, discovers the zone, provisions everything.

## Architecture (per shard)

```
1 root domain (bought or existing on Cloudflare)
  └── 20 auto-generated subdomains
        └── 5 mailboxes each (first.last@sub.domain, British female names)
              = 100 mailboxes per shard

1 Mailcheap VPS (port 25 open by default, cold email supported)
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

### 2. Mailcheap account

1. Sign up at https://mailcheap.co, buy a VPS plan that allows Docker (≥1GB RAM).
2. Generate an API key. Confirm port 25 is unblocked for cold email on that plan.

### 3. Local environment

```bash
git clone <this-repo> && cd ColdEmailInfra
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in the token, account ID, and Mailcheap key
ssh-keygen -t ed25519 -C "coldemail" -f ~/.ssh/id_ed25519 -N ""   # if you don't have one
```

## Deploy a shard (per domain, after first-time setup)

```bash
./scripts/deploy_shard.py --domain example.co.uk
```

That's it. The script:

1. Checks if the domain is already on Cloudflare. If not, checks availability via the CF Registrar API, shows the price, and (with your confirmation) registers it at cost.
2. Generates 20 subdomains + 100 mailboxes.
3. Provisions a Mailcheap VPS and sets PTR to `mail.<domain>`.
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

## Runbook notes

- **Warmup**: first two weeks post-deploy, keep Bison's warmup at 2–5/inbox/day, then ramp to 10.
- **Spam complaint ceiling**: Gmail/Yahoo cut off at 0.3%. List-hygiene concern, not infra.
- **One-click unsubscribe**: Bison must include `List-Unsubscribe` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click` in outgoing mail. Manual header spot-check at go-live.
- **If a shard gets flagged**: destroy it. Reputation is shard-isolated — don't try to rehabilitate.
