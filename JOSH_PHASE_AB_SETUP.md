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

This is the screen the browser will show you when the bootstrap script asks you to consent. We're filling out who the "app" is (just us) so Google knows what to display.

**Heads up before starting**: GCP renamed this section recently. The sidebar entry might say "OAuth consent screen", "Audience", or "Branding" depending on the rollout state of your account. They all lead to the same wizard. If you're not sure which to click, the URL is: https://console.cloud.google.com/auth/branding (substitute your project ID if needed).

**Make sure the project name at the top of the page is `reachos-postmaster` for every step below.**

**Sub-step 3a — Open the wizard**

1. Sidebar (left side) → click **APIs & Services**
2. Within that submenu, click **OAuth consent screen** (might be labelled "Branding" in newer UIs — same thing)
3. If you see a "Get started" intro page, click **GET STARTED**.

**Sub-step 3b — App information**

You'll see a form. Fill EXACTLY these fields, skip everything else:

- **App name**: `reachos-postmaster`
- **User support email**: select `josh.gain@reachos.co` from the dropdown
- **App logo**: skip (leave empty)
- **Application home page**: skip
- **Application privacy policy link**: skip
- **Application terms of service link**: skip

Click **NEXT** at the bottom.

**Sub-step 3c — Audience**

Choose **External**. (Internal only works inside a Google Workspace org and we don't need that complication.)

Click **NEXT**.

**Sub-step 3d — Contact information**

- **Email addresses**: type `josh.gain@reachos.co` and press Enter

Click **NEXT**.

**Sub-step 3e — Finish**

You'll see a summary. Tick the "I agree to the Google API Services User Data Policy" checkbox.

Click **CONTINUE** then **CREATE**.

**Sub-step 3f — Add yourself as a test user**

The app is now in "Testing" mode, which means ONLY emails added to the test-user list can authenticate. We need to add `josh.gain@reachos.co`.

1. Still in APIs & Services → look for **Audience** in the sidebar (or stay on the current page; depending on UI it might be a tab labelled **Audience** or **Test users**)
2. Scroll to the section called **Test users**
3. Click **+ ADD USERS**
4. Type `josh.gain@reachos.co` → press Enter → click **SAVE**

You should see one test user listed. If it's there, Step 3 is done.

**Skipping the Scopes screen**

If anywhere in the flow you land on a "Scopes" screen and don't know what to do, leave it empty and click **SAVE AND CONTINUE**. Our bootstrap script declares the scope it needs (`postmaster.readonly`) at runtime; we don't need to pre-declare it here. Google may complain that no scopes are selected — ignore the warning, click through.

**What "Testing" status means + the scary warning you'll see later**

The app stays in Testing status forever — we don't need to publish it. When you run the bootstrap script in Step 5 and the browser opens the consent page, you'll see a big yellow warning:

> Google hasn't verified this app
> The app is requesting access to sensitive info in your Google Account. Until the developer (you) verifies this app with Google, you shouldn't use it.

This is **expected and safe** — Google warns about all unverified Testing-mode apps. To proceed:
1. Click **Advanced** (a small text link at the bottom of the warning panel)
2. Click **Go to reachos-postmaster (unsafe)** (the "(unsafe)" is just Google being dramatic)
3. The proper consent screen appears
4. Click **Continue** / **Allow**

That's it.

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
