# Phase A + B setup — step by step (Josh)

Two integrations to unlock. Phase A = Google's signal. Phase B = Microsoft's signal. Both run in the background once set up. Everything else is already built — once you've completed these steps and pasted me the values, the integrations go live.

**Total time on your side: ~30 min of clicking + 1-3 days waiting for Microsoft to approve.**

---

## Phase A — Google Postmaster Tools (~20 min)

### Step 1 — Create a Google Cloud project

1. Open https://console.cloud.google.com in a browser **signed in as `josh.gain@reachos.co`**.
2. Top bar → project dropdown (might say "Select a project") → **NEW PROJECT**.
3. Project name: `reachos-postmaster`. Click **CREATE**.
4. Wait ~10 seconds. Click the bell icon → "Select project" once it's ready.

### Step 2 — Enable the Postmaster Tools API

1. Open this URL: https://console.cloud.google.com/apis/library/gmailpostmastertools.googleapis.com
2. Make sure the project name shown at the top is `reachos-postmaster`.
3. Click **ENABLE**. Wait ~15 seconds for it to activate.

### Step 3 — Configure the OAuth consent screen (one-off)

1. Sidebar → **APIs & Services** → **OAuth consent screen**.
2. User Type: **External**. Click **CREATE**.
3. Fill in just the required fields:
   - App name: `reachos-postmaster`
   - User support email: `josh.gain@reachos.co`
   - Developer contact: `josh.gain@reachos.co`
4. Click **SAVE AND CONTINUE** through the next 3 screens (Scopes, Test users, Summary) — nothing to fill on those, just click through.
5. On the dashboard, find **Test users** → Add: `josh.gain@reachos.co`. Save.

### Step 4 — Create the OAuth client credentials

1. Sidebar → **APIs & Services** → **Credentials**.
2. Click **+ CREATE CREDENTIALS** → **OAuth client ID**.
3. Application type: **Desktop app**.
4. Name: `postmaster-bootstrap`. Click **CREATE**.
5. A dialog appears with the client ID/secret. Click **DOWNLOAD JSON** (top right) and save the file as **`gpt_oauth_client.json`** somewhere you can find it.

### Step 5 — Run the bootstrap on your laptop

In a terminal:
```bash
# Move the downloaded JSON to the right place
mv ~/Downloads/client_secret_*.json /home/dev/ColdEmailInfra/gpt-poller/gpt_oauth_client.json

# Install the one dep (skip if already done)
pip install google-auth-oauthlib

# Run the script
cd /home/dev/ColdEmailInfra/gpt-poller && python gpt_oauth_bootstrap.py
```

A browser will open. Click your `josh.gain@reachos.co` account → "Advanced" → "Go to reachos-postmaster (unsafe)" → "Allow". (The "unsafe" warning is normal for new OAuth apps that haven't gone through Google's verification — it's fine for our internal use.)

After granting, the terminal prints **3 lines starting with `GPT_CLIENT_ID=`**.

### Step 6 — Send me those 3 lines

Paste them in a chat reply. I'll add them to the production env and start the daily pulls.

### Step 7 — Add your active domains in Postmaster Tools

1. Open https://postmaster.google.com
2. Click the **red + button** (bottom right) → **Add domain**
3. Type a root domain (e.g. `tryreachos.com`) → **NEXT**
4. Google shows a TXT record. Don't paste it yet — copy the **token only** (the long `google-site-verification=...` string).
5. Repeat for every active root domain.

When you're done, give me the list (just the root domains — I can pull the tokens from postmaster.google.com's API once verified, OR you can paste the domain+token pairs to me and I'll bulk-write the TXT records via Cloudflare API).

**Easiest path**: paste me the domains as a list, I'll write a small script that uses the Postmaster API to fetch each domain's verification token and writes the TXT record automatically. You then click **VERIFY** on each one in the UI.

---

## Phase B — Microsoft SNDS (~10 min + 1-3 day wait)

### Step 1 — Provision `abuse@reachos.co` (Cloudflare Email Routing)

This is just a forwarder so Microsoft can reach you.

1. Log into Cloudflare → select the `reachos.co` zone.
2. Sidebar → **Email** → **Email Routing**.
3. If it's not enabled, click **Get started**. Cloudflare auto-adds the MX + SPF records.
4. Tab: **Routing addresses** → **Destination addresses** → **+ Add destination address** → enter `josh.gain@reachos.co`. Click the verification link Cloudflare emails you.
5. Tab: **Routes** → **Custom addresses** → **+ Create address**:
   - Custom address: `abuse`
   - Domain: `reachos.co`
   - Action: **Send to an email**
   - Destination: `josh.gain@reachos.co`
6. **Test**: send an email from your phone to `abuse@reachos.co`. Confirm it lands in josh.gain@reachos.co's inbox.

### Step 2 — Submit SNDS access requests (12 ranges)

1. Open https://sendersupport.olc.protection.outlook.com/snds/addnetwork.aspx
2. You'll submit 12 separate requests — once per /24 range below.

For EACH range:
- **Network range**: paste the /24 (e.g. `193.180.208.0/24`)
- **Contact email**: `abuse@reachos.co`
- **Company**: `10X Managers / ReachOS`
- **Justification** (copy-paste this verbatim for each):
  > We operate transactional and cold outreach mail from these IPs on behalf of our clients via Webdock and Contabo VPS infrastructure. Requesting SNDS access to monitor IP reputation, complaint rates, and trap hits.

The 12 ranges to submit:
```
193.180.208.0/24
193.180.209.0/24
193.180.211.0/24
193.180.213.0/24
193.180.215.0/24
193.181.210.0/24
193.181.211.0/24
193.181.213.0/24
45.148.29.0/24
45.148.30.0/24
92.113.150.0/24
92.113.151.0/24
```

For each, Microsoft sends a verification email to `abuse@reachos.co`. Click each link as they arrive.

### Step 3 — Submit JMRP at the same time

Same form pattern at https://sendersupport.olc.protection.outlook.com/snds/JMRP.aspx — submit each of the 12 ranges again. Same contact, same justification. JMRP is the spam-complaint feedback loop; SNDS is the aggregate reputation data. We want both.

### Step 4 — Wait for Microsoft (1-3 business days)

When approved, Microsoft emails `abuse@reachos.co` with a per-workspace access key URL. It looks like:
```
https://sndsui.engineering.microsoft.com/snds/data.aspx?key=<long-secret>
```

**Forward me that URL** when it lands. I plug it into the env and the daily pulls begin.

---

## What happens after you've done your parts

**Phase A (after Step 6 + domain list):**
1. I add `GPT_CLIENT_ID` / `GPT_CLIENT_SECRET` / `GPT_REFRESH_TOKEN` to infraapi1's env
2. I deploy the `coldemail-gpt-poller` sidecar (~2 min)
3. Daily 07:00 UTC the poller pulls yesterday's stats for every verified domain
4. CRM dashboard gets a new **"gmail"** column showing each domain's reputation (HIGH/MEDIUM/LOW/BAD) + spam rate %
5. Diagnosis drawer gets a Gmail card with the full breakdown
6. Rules engine fires "Gmail BAD" → critical and "spam rate > 1%" → critical alerts

**Phase B (after Microsoft approves):**
1. I add `SNDS_DATA_URL` to infraapi1's env
2. I deploy the `coldemail-snds-poller` sidecar
3. Daily 07:15 UTC the poller pulls the CSV from Microsoft
4. CRM dashboard gets a new **"outlook"** column showing complaint rate + trap hits per IP
5. Diagnosis drawer gets a Microsoft card
6. Rules engine fires "complaint rate > 0.5%" → critical and "trap hits > 0" → warn

If you hit any snag, just send a screenshot of where you're stuck and I'll talk you through it.
