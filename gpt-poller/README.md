# Google Postmaster Tools poller

Pulls per-domain Gmail-side signals (domain reputation, IP reputation, spam rate, FBL volume, authentication %, encryption %) into `gpt_daily_stats` daily.

## Setup (one-off, Josh does these)

### 1. Google Cloud project + OAuth credentials

Sign in to https://console.cloud.google.com as `josh.gain@reachos.co`.

- Create a new project: **reachos-postmaster**
- Enable the **Postmaster Tools API**:
  https://console.cloud.google.com/apis/library/gmailpostmastertools.googleapis.com
- Create OAuth 2.0 Client ID:
  - APIs & Services → Credentials → Create credentials → OAuth client ID
  - Application type: **Desktop app**
  - Name: `postmaster-bootstrap`
- Download the client JSON. Save it locally as `gpt-poller/gpt_oauth_client.json`.

### 2. Run the bootstrap

```bash
cd /home/dev/ColdEmailInfra/gpt-poller
pip install google-auth-oauthlib    # one-off
python gpt_oauth_bootstrap.py
```

Browser opens for consent. After granting, the script prints three lines (CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN). Paste them to Claude.

### 3. Add domains in Postmaster Tools UI

Visit https://postmaster.google.com and add each active sending root domain. For each, GPT shows a TXT record value (looks like `google-site-verification=...`). Send the list to Claude — Claude bulk-writes the `_postmaster.<domain>` TXT records via the Cloudflare API.

Once verified by Google (~5-15 min per domain), each one becomes pollable.

## How the poller works (Claude builds this after Josh's setup)

- Daily 07:00 UTC sidecar on infraapi1 (`coldemail-gpt-poller`)
- Refreshes the access token, walks all registered domains
- Upserts `gpt_daily_stats` (domain, date) → reputation enum + spam rate + auth %
- View extension into `shard_health_recommendations` exposes the latest metrics to the CRM
- Rules engine: `spam_rate > 1%` = critical, `> 0.3%` = warn

## Files

- `gpt_oauth_bootstrap.py` — one-off OAuth handshake (Josh runs)
- `Dockerfile` — sidecar image (Claude builds)
- `crontab` — daily 07:00 UTC trigger (Claude writes)
- `poll.py` — main loop (Claude writes after Josh delivers tokens)
