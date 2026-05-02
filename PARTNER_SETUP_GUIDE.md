# Cold Email Infrastructure — Partner Setup Guide

> **Give this entire document to Claude Code.** It contains everything Claude needs to walk you through setting up your own cold email shard infrastructure from scratch.

---

## What This Does

This toolkit provisions **cold email sending infrastructure** — one "shard" at a time. Each shard:

1. **Registers a domain** (or uses one you already own) via Cloudflare Registrar
2. **Provisions a VPS** on Webdock (pay-per-hour, ~€4/mo per shard)
3. **Installs docker-mailserver** with proper TLS (Let's Encrypt)
4. **Generates 100 unique mailboxes** across 4 subdomains (e.g. `hello.`, `intro.`, `new.`, `join.`)
5. **Configures DNS** — MX, SPF, DKIM, DMARC records
6. **Exports a Bison-ready CSV** for bulk-uploading senders to Email Bison
7. **Verifies everything** — DNS propagation, TLS, SMTP banner, FCrDNS

You can deploy as many shards as you need. Each shard = 1 domain = 1 VPS = 100 mailboxes.

---

## Prerequisites

You need accounts with:

| Service | Purpose | Sign Up |
|---------|---------|---------|
| **Cloudflare** | DNS management + domain registration | https://dash.cloudflare.com/sign-up |
| **Webdock** | VPS hosting (pay-per-hour) | https://webdock.io |
| **Email Bison** (optional) | Cold email sending platform | Your existing Bison instance |

### Important Webdock Note
Link a **credit card** (not PayPal) to your Webdock account. PayPal payments are non-refundable, but credit card billing is prorated — you only pay for the time a server exists, and destroying within 24 hours costs nothing (free trial window).

---

## Step-by-Step Setup (Claude Code Instructions)

### Step 1: Clone the Repository

```bash
git clone https://github.com/JoshGain10X/ColdEmailInfra.git
cd ColdEmailInfra
```

### Step 2: Install Python Dependencies

Requires Python 3.10+.

```bash
pip install -r requirements.txt
```

### Step 3: Generate an SSH Key Pair

If you don't already have one at `~/.ssh/id_ed25519`:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/id_ed25519 -N ""
```

### Step 4: Get Your API Credentials

You need to gather these credentials and put them in a `.env` file:

#### Cloudflare
1. Go to https://dash.cloudflare.com/profile/api-tokens
2. Create a token with these permissions:
   - **Zone: DNS: Edit** (All zones)
   - **Zone: Zone: Read** (All zones)
   - **Zone: Zone Settings: Edit** (All zones)
   - **Account: Cloudflare Registrar: Edit**
   - **Zone: Dynamic Redirect: Edit** (All zones)
3. Copy the token → this is your `CLOUDFLARE_API_TOKEN`
4. Go to any zone dashboard → the Account ID is in the right sidebar → this is your `CLOUDFLARE_ACCOUNT_ID`

#### Webdock
1. Go to https://webdock.io/en/dash/account/apitokens
2. Generate a new API token
3. Copy it → this is your `WEBDOCK_API_TOKEN`

#### Email Bison (optional, for loading senders)
1. In your Bison instance, go to Settings → API
2. Generate one API token **per workspace** (not a super-admin token)
3. List them comma-separated as `BISON_API_TOKENS`

### Step 5: Create Your `.env` File

Copy the example and fill in your values:

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# Cloudflare
CLOUDFLARE_API_TOKEN=your_token_here
CLOUDFLARE_ACCOUNT_ID=your_account_id_here

# Webdock
WEBDOCK_API_TOKEN=your_webdock_token_here

# These are the defaults — verify with: python3 scripts/webdock_discover.py
# If the slugs below 404 on deploy, run discover to find current valid values.
WEBDOCK_PROFILE_SLUG=vps-epyc-advanced-2025
WEBDOCK_LOCATION_ID=dk
WEBDOCK_IMAGE_SLUG=webdock-ubuntu-jammy-cloud

# Customise these for your business
REDIRECT_TARGET=https://yourbusiness.com
DMARC_RUA=dmarc@yourbusiness.com
LE_EMAIL=ops@yourbusiness.com

# SSH key paths (defaults work if you followed Step 3)
SSH_PUBLIC_KEY_PATH=~/.ssh/id_ed25519.pub
SSH_PRIVATE_KEY_PATH=~/.ssh/id_ed25519

# Email Bison (optional)
BISON_API_BASE=https://your-bison-instance.com
BISON_API_TOKENS=token1,token2
```

### Step 6: Discover Available Webdock Options

Run this to see valid profile slugs, locations, and images for your account:

```bash
python3 scripts/webdock_discover.py
```

Update `WEBDOCK_PROFILE_SLUG`, `WEBDOCK_LOCATION_ID`, and `WEBDOCK_IMAGE_SLUG` in `.env` if the defaults don't match.

---

## Deploying a Shard

```bash
python3 scripts/deploy_shard.py --domain yourdomain.com --ssl-type letsencrypt --yes
```

**What happens (10 steps, ~8-12 minutes):**

| Step | Action |
|------|--------|
| 0 | Checks if domain is on Cloudflare; registers it if not |
| 1 | Generates 4 subdomains + 100 mailboxes (25 per subdomain) |
| 2 | Provisions a Webdock VPS |
| 3 | Sets reverse DNS (PTR record) |
| 4 | Configures all DNS records (A, MX, SPF, DMARC) |
| 5 | Installs docker-mailserver with Let's Encrypt TLS |
| 6 | Creates 100 mailboxes on the server |
| 7 | Generates DKIM keys and publishes to Cloudflare |
| 8 | Exports Bison-compatible CSV |
| 10 | Summary |

**The script is idempotent** — if it fails midway, just re-run the same command and it resumes from where it left off. State is saved in `shards/<domain>.json`.

### Key Options

| Flag | Description |
|------|-------------|
| `--domain` | The domain to deploy (required) |
| `--ssl-type letsencrypt` | Use Let's Encrypt certs (recommended) |
| `--yes` | Skip confirmation prompts |
| `--skip-purchase` | Error if domain isn't already on Cloudflare (don't auto-register) |
| `--provider webdock` | VPS provider (default: webdock) |
| `--product-id` | Override VPS size |
| `--region` | Override VPS region |

---

## Verifying a Shard

After deploy completes:

```bash
python3 scripts/verify_shard.py --domain yourdomain.com
```

Checks DNS propagation, FCrDNS alignment, SMTP banner, and TLS certificate. All should show `[PASS]`.

---

## Loading Senders into Email Bison

After deploy + verify:

```bash
python3 scripts/load_to_bison.py --domain yourdomain.com --yes
```

This bulk-uploads the 100 mailboxes from `shards/<domain>_bison.csv` into your Bison workspace and tags them with "Custom SMTP".

If you have multiple workspaces configured in `BISON_API_TOKENS`, use `--workspace "Workspace Name"` to target a specific one.

---

## Destroying a Shard

```bash
python3 scripts/destroy_shard.py --domain yourdomain.com --yes
```

This will:
- Delete the Webdock VPS (billing stops immediately)
- Remove all DNS records from Cloudflare
- Archive the state file to `shards/archived/`

**Note:** This does NOT delete the domain from Cloudflare or remove senders from Bison.

---

## File Structure

```
ColdEmailInfra/
├── .env                        # Your credentials (never commit this)
├── .env.example                # Template for .env
├── requirements.txt            # Python dependencies
├── shards/                     # State files + CSVs per domain
│   ├── yourdomain.com.json     # Deploy state (auto-created)
│   ├── yourdomain.com_bison.csv # Bison import CSV (auto-created)
│   └── archived/               # Destroyed shard archives
├── scripts/
│   ├── deploy_shard.py         # Deploy a new shard
│   ├── destroy_shard.py        # Tear down a shard
│   ├── verify_shard.py         # Verify DNS/TLS/SMTP
│   ├── load_to_bison.py        # Upload senders to Bison
│   ├── webdock_discover.py     # List Webdock options
│   └── lib/                    # Shared libraries (don't edit)
├── data/
│   ├── british_female_names.txt # Name generation data
│   └── british_surnames.txt
└── docker-mailserver/          # Mailserver config templates
```

---

## Troubleshooting

### "Missing env var: CLOUDFLARE_API_TOKEN"
Your `.env` file is missing required values. Check Step 5.

### Deploy fails at Step 2 (VPS provision)
Run `python3 scripts/webdock_discover.py` to verify your profile/location/image slugs are valid. Webdock changes these periodically.

### Deploy fails at Step 5 (mailserver install)
The VPS may need more time to boot. Wait 60 seconds and re-run the deploy command — it will resume from Step 5.

### Verify shows WARN on SMTP/TLS checks
If you're on a residential ISP (especially UK), outbound ports 25 and 465 are likely blocked by your ISP. This is normal — Bison sends from its own datacenter, not your local machine. The WARN does not affect actual email delivery.

### "webdock" slug 404s
Webdock periodically updates their product catalog. Run `python3 scripts/webdock_discover.py` to find current valid slugs.

---

## Quick Reference Commands

```bash
# Deploy a new shard
python3 scripts/deploy_shard.py --domain example.com --ssl-type letsencrypt --yes

# Verify it
python3 scripts/verify_shard.py --domain example.com

# Load into Bison
python3 scripts/load_to_bison.py --domain example.com --workspace "My Workspace" --yes

# Destroy it
python3 scripts/destroy_shard.py --domain example.com --yes

# See available Webdock options
python3 scripts/webdock_discover.py
```
