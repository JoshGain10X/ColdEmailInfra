# Microsoft SNDS + JMRP setup brief

For Josh to walk through. ~15 min of clicking, then 1-3 days waiting for Microsoft to approve.

## Step 1: Provision abuse@reachos.co (Cloudflare Email Routing)

Microsoft needs to send a verification email; we forward it to `josh.gain@reachos.co`.

1. Log in to Cloudflare dashboard, select the `reachos.co` zone
2. Sidebar → **Email** → Email Routing
3. Click **Get started** (if not already enabled). Cloudflare adds the required MX + SPF records automatically. If reachos.co already has MX records pointing elsewhere, Cloudflare warns; pick "Replace existing" since we want CF to handle inbound.
4. Under **Routing addresses**, add destination: `josh.gain@reachos.co` (verify when Cloudflare emails you)
5. Under **Custom addresses**, click **Create address**:
   - Custom address: `abuse`
   - Action: **Send to an email**
   - Destination: `josh.gain@reachos.co`
6. Send a test email from anywhere to `abuse@reachos.co` — confirm it lands in josh.gain@reachos.co's inbox

Done. Microsoft can now reach you for verification.

## Step 2: SNDS network submission

URL: https://sendersupport.olc.protection.outlook.com/snds/addnetwork.aspx

Submit each of the 12 IP ranges below as a separate request. Use `abuse@reachos.co` as the contact email throughout.

**Active sending /24s (as of 2026-06-17, 47 IPs total):**

```
193.180.208.0/24   (7 IPs)
193.180.209.0/24   (3 IPs)
193.180.211.0/24   (1 IP)
193.180.213.0/24   (1 IP)
193.180.215.0/24   (2 IPs)
193.181.210.0/24   (5 IPs)
193.181.211.0/24   (3 IPs)
193.181.213.0/24   (17 IPs)
45.148.29.0/24     (1 IP)
45.148.30.0/24     (2 IPs)
92.113.150.0/24    (3 IPs)
92.113.151.0/24    (1 IP)
```

For each submission, fields:
- **Network range**: the /24 (e.g. `193.180.208.0/24`)
- **Contact email**: `abuse@reachos.co`
- **Company name**: 10X Managers / ReachOS
- **Justification**: "We operate transactional and cold outreach mail from these IPs on behalf of our clients via Webdock and Contabo VPS infrastructure. Requesting SNDS access to monitor IP reputation, complaint rates, and trap hits."

Microsoft sends a verification email to `abuse@reachos.co` for each. Click each link.

**Approval SLA**: 1-3 business days. They may push back if the WHOIS ownership of the IP block doesn't match — Webdock owns these, not us. If denied, Microsoft sender support handles per-IP exceptions when you can demonstrate operational control (DKIM keys, MX records).

## Step 3: JMRP (Junk Mail Reporting Program)

URL: https://sendersupport.olc.protection.outlook.com/snds/JMRP.aspx

Different from SNDS — JMRP is the feedback loop that pings you when users click "Junk" on your mail. Higher signal than aggregate complaint rates.

Same form pattern, same IP list. Same contact: `abuse@reachos.co`.

## Step 4: Once approved

Microsoft issues a per-account access key URL like:
`https://sndsui.engineering.microsoft.com/snds/data.aspx?key=<long-secret>`

Forward that URL to Claude. Claude wires it into the `coldemail-snds-poller` sidecar (already drafted, sitting in this directory) and the daily pulls begin.

## What Claude builds after Josh's setup

- `snds_daily_ip_stats` Supabase table
- `coldemail-snds-poller` sidecar (daily 07:15 UTC, CSV pull from MS)
- View extension exposing SNDS metrics per shard
- CRM badge for Microsoft signal
- Rules engine triggers on complaint_rate > 0.5% (critical) + trap hits (warn)
