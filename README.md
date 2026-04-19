# Cold Email Infrastructure

Self-hosted cold email sending fleet. One shard = one VPS + one pre-warmed domain + 20 subdomains + 100 mailboxes. Exported to Email Bison for sending.

## Architecture (per shard)

```
1 pre-warmed root domain     (you supply)
  └── 20 auto-generated subdomains
        └── 5 mailboxes each (first.last@sub.domain, British female names)
              = 100 mailboxes per shard

1 Mailcheap VPS (port 25 open by default, cold email supported)
  └── docker-mailserver (Postfix + Dovecot + OpenDKIM + Rspamd)
        └── hosts all 20 subdomains as virtual domains
```

Build and verify shard 1. When you want shard 2, run the same command against a new domain.

## Prerequisites

- Python 3.10+
- Cloudflare account with API token (Zone:Edit, DNS:Edit, Page Rules:Edit)
- Mailcheap account with API key
- Target domain on Cloudflare DNS (nameservers pointed at Cloudflare)
- An SSH keypair on your local machine

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in .env with your tokens
```

## Deploy a shard

```bash
./scripts/deploy_shard.py \
  --domain example.co.uk \
  --cloudflare-zone-id $ZONE_ID \
  --mailcheap-plan vps-starter \
  --region uk-lon
```

The script is idempotent — safe to re-run. It writes `shards/example.co.uk.json` tracking every step. Steps already done are skipped.

## Verify

```bash
./scripts/verify_shard.py --domain example.co.uk
```

Checks DNS propagation, FCrDNS alignment (PTR + HELO + forward A), DKIM/SPF/DMARC pass headers, and flags any missing one-click unsubscribe in Bison test sends.

## Export for Email Bison

After a successful deploy, the script writes `shards/example.co.uk_bison.csv` with Bison's exact import headers:

```
Name, Email, Password, IMAP Server, IMAP Port, SMTP Server, SMTP Port, Daily Limit, SMTP Secure, IMAP Secure
```

Defaults: SMTP 465/SSL, IMAP 993/SSL, Daily Limit 10.

## Destroy a shard

```bash
./scripts/destroy_shard.py --domain example.co.uk
```

Deletes the VPS, removes all DNS records for that zone's subdomains, archives the state file.

## Runbook notes

- **Warmup**: the first two weeks post-deploy, keep Bison's warmup setting low (2–5/inbox/day), then ramp to 10.
- **Spam complaint ceiling**: Gmail/Yahoo cut off at 0.3% — a list-hygiene concern, not infra.
- **One-click unsubscribe**: Bison must include `List-Unsubscribe` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click` in outgoing mail. The verify script flags if missing.
- **If a shard gets flagged**: destroy it, don't try to rehabilitate. Reputation is shard-isolated.
