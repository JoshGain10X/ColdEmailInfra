#!/usr/bin/env python3
"""Inbound-ingestion health monitor for the cold-email fleet.

Three checks. The first two are aimed at the failure mode that bit us in Jul 2026: a shard's
Bison IMAP reply-ingestion silently stalls while Bison still reports the sender
as "Connected". Status flags lie, so we watch *behaviour* instead.

  Layer 1 - DNS drift guard (prevention):
    Scans every Cloudflare zone for a `mail.*` AAAA record whose content is a
    /64-base address (ends in `::`). That is the exact dead-record that broke
    reply ingestion fleet-wide. Any hit = a shard (new or regressed) is about
    to go dark. Cheap; catches the cause before the symptom.

  Layer 2 - ingestion-stall detector (detection, any cause):
    An actively-sending shard receives inbound constantly (bounces, real
    replies), so a shard whose freshest ingested item is older than STALL_HOURS
    is stalled - regardless of whether the cause is DNS, a cert, auth, or a
    dropped IMAP connection. Judged per SHARD across every one of its mailboxes,
    not per sampled mailbox: inbound spreads thinly over ~100 mailboxes, so any
    single mailbox is quiet for days at a time even on a perfectly healthy
    shard. Rolled up per shard so one dead shard is one alert, not 100.

  Layer 3 - resource-ceiling guard (prevention):
    Watches the two silent ceilings that left every shard degraded for months
    with no alert: fs.inotify.max_user_instances (Dovecot needs ~225, kernel
    default is 128, so it silently stopped watching mailboxes) and Dovecot's
    imap-login process_limit (one process per connection in high-security mode,
    ~400 sessions against a 500 ceiling, connections dropped). Both are fixed
    now; this layer notices if that regresses or if a growing mailbox count
    eats the new headroom. Also flags imap-login config drift directly, so we
    hear about it before saturation rather than after. Reported as a WARNING,
    not a critical - it is degradation, not an outage. Failsafe: unreachable
    shards are skipped, and a missing SSH key skips the layer entirely.

On any finding it POSTs a single JSON alert to N8N_ALERT_WEBHOOK_URL (an n8n
webhook that notifies MS Teams). If the webhook is unset it just logs - so the
monitor is safe to run before the n8n side is wired.

Env (from .env.v2):
  CLOUDFLARE_API_TOKEN        - Layer 1 DNS scan
  BISON_API_BASE, EB_SUPERADMIN_KEY - Layer 2 (superadmin switches workspaces)
  SUPABASE_URL, SUPABASE_SERVICE_KEY - active-shard list (infra_shards)
  N8N_ALERT_WEBHOOK_URL       - where alerts go (optional; logs if unset)
  STALL_HOURS   (default 18)  - ingestion-age threshold
  MIN_SENT      (default 5)   - only judge shards whose senders have really sent
  MAX_REPLY_PAGES (default 100) - cap on the reply-stream walk per workspace
  SHARD_SSH_KEY (default /root/.ssh/id_ed25519) - Layer 3; layer skipped if absent
  SHARD_SSH_USER (default admin)  - Layer 3 ssh user
  INOTIFY_WARN_PCT (default 80)   - Layer 3 inotify-usage alert threshold
"""
from __future__ import annotations
import json, os, sys
from datetime import datetime, timedelta, timezone

import requests

BASE = os.environ.get("BISON_API_BASE", "").rstrip("/")
SUPER = os.environ.get("EB_SUPERADMIN_KEY", "")
CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
WEBHOOK = os.environ.get("N8N_ALERT_WEBHOOK_URL", "")
STALL_HOURS = float(os.environ.get("STALL_HOURS", "18"))
MIN_SENT = int(os.environ.get("MIN_SENT", "5"))
MAX_REPLY_PAGES = int(os.environ.get("MAX_REPLY_PAGES", "100"))
# Layer 3: shard resource ceilings. Optional - skipped cleanly if the deploy key
# is not mounted, so the monitor never breaks because of it.
SHARD_SSH_KEY = os.environ.get("SHARD_SSH_KEY", "/root/.ssh/id_ed25519")
SHARD_SSH_USER = os.environ.get("SHARD_SSH_USER", "admin")
INOTIFY_WARN_PCT = float(os.environ.get("INOTIFY_WARN_PCT", "80"))
CF = "https://api.cloudflare.com/client/v4"


def log(m: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {m}", flush=True)


def _age_hours(iso: str) -> float:
    dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - dt).total_seconds() / 3600.0


# ---------- Layer 1: Cloudflare AAAA drift ----------
def cf(path, params=None):
    r = requests.get(CF + path, params=params or {},
                     headers={"Authorization": f"Bearer {CF_TOKEN}"}, timeout=30)
    return r.json()


def layer1_dns_drift() -> list[dict]:
    if not CF_TOKEN:
        log("Layer 1 skipped: no CLOUDFLARE_API_TOKEN")
        return []
    bad, page = [], 1
    while True:
        z = cf("/zones", {"per_page": 50, "page": page})
        for zone in z.get("result", []):
            recs = cf(f"/zones/{zone['id']}/dns_records", {"type": "AAAA", "per_page": 200})
            for rec in recs.get("result", []):
                if rec["name"].startswith("mail.") and rec["content"].strip().lower().endswith("::"):
                    bad.append({"zone": zone["name"], "record": rec["name"], "content": rec["content"]})
        info = z.get("result_info") or {}
        if page >= (info.get("total_pages") or 1):
            break
        page += 1
    if bad:
        log(f"Layer 1 FAIL: {len(bad)} dead mail AAAA record(s) found")
    else:
        log("Layer 1 OK: no dead mail AAAA records fleet-wide")
    return bad


# ---------- Layer 2: Bison ingestion stall ----------
def bison(path, method="GET", body=None, tok=None):
    fn = getattr(requests, method.lower())
    kw = {"headers": {"Authorization": f"Bearer {tok or SUPER}",
                      "Content-Type": "application/json", "Accept": "application/json"},
          "timeout": 30}
    if body is not None:
        kw["json"] = body
    return fn(BASE + path, **kw).json()


def active_shards() -> list[dict]:
    """Shards that SHOULD be ingesting: live + loaded into Bison."""
    url = (f"{SB_URL}/rest/v1/infra_shards"
           "?select=domain,vps_ip,bison_workspace,status,bison_loaded"
           "&status=in.(active,verified)&bison_loaded=eq.true")
    r = requests.get(url, headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}, timeout=30)
    return r.json()


def workspace_senders() -> list[dict]:
    """Every sender in the CURRENT workspace, walked page by page.

    We used to fetch per shard with `?search=<domain>`, but that endpoint caps
    what it returns - on a 100-mailbox shard it handed back only ~15 senders,
    always the same subdomain. Shards were then judged on that unrepresentative
    slice, and any shard whose slice happened to be low-volume was written off
    as "not judged". Walk the full list instead.
    """
    page, out = 1, []
    while True:
        r = bison(f"/api/sender-emails?per_page=100&page={page}")
        rows = r.get("data") or []
        out += rows
        meta = r.get("meta") or {}
        if page >= (meta.get("last_page") or 1) or not rows:
            break
        page += 1
    return out


def newest_ingest_by_host(sender_host: dict[int, str], cutoff: str) -> dict[str, str]:
    """Newest ingested inbound item per mail host, from the workspace stream.

    Freshness has to be judged per SHARD, not per mailbox. A shard carries ~100
    mailboxes and inbound spreads thinly across all of them, so any individual
    mailbox routinely goes days without a bounce or reply while the shard as a
    whole ingests constantly. Sampling a handful of mailboxes and taking the
    freshest reads those quiet mailboxes as an outage - that is what produced
    the false critical alerts on 2026-08-15/16, when the five "stalled" shards
    had in fact ingested 21-60 items each during the alert window.

    /api/replies is newest-first, so the first time a host appears is its newest
    item. Walk back only as far as the stall cutoff.
    """
    newest: dict[str, str] = {}
    page = 1
    while page <= MAX_REPLY_PAGES:
        r = bison(f"/api/replies?per_page=100&page={page}")
        rows = r.get("data") or []
        if not rows:
            break
        for x in rows:
            created = str(x.get("created_at") or "")
            if created[:19] < cutoff:
                return newest
            host = sender_host.get(x.get("sender_email_id"))
            if host and host not in newest:
                newest[host] = created
        page += 1
    return newest


def layer2_ingestion_stall() -> list[dict]:
    if not (BASE and SUPER and SB_URL and SB_KEY):
        log("Layer 2 skipped: missing BISON/SUPABASE env")
        return []
    shards = active_shards()
    ws_list = bison("/api/workspaces").get("data", [])
    # Match each live shard to its senders by walking EVERY Bison workspace and
    # matching on mail host - NOT on the infra_shards.bison_workspace label,
    # which is stale/mismatched for some shards. This keeps coverage fleet-wide
    # (10X + ReachOS + Scouted + any client) regardless of how shards are labelled.
    remaining = {s["domain"]: s for s in shards}
    stalled = []
    for w in ws_list:
        if not remaining:
            break
        bison("/api/workspaces/switch-workspace", "POST", {"team_id": w["id"]})
        by_host: dict[str, list[dict]] = {}
        sender_host: dict[int, str] = {}
        for s in workspace_senders():
            if s.get("type") == "custom":
                by_host.setdefault(s.get("imap_server"), []).append(s)
                sender_host[s.get("id")] = s.get("imap_server")
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=STALL_HOURS)).isoformat()[:19]
        newest = newest_ingest_by_host(sender_host, cutoff)
        for domain in list(remaining):
            mail_host = f"mail.{domain}"
            senders = [s for s in by_host.get(mail_host, [])
                       if (s.get("emails_sent_count") or 0) >= MIN_SENT]
            if not senders:
                continue  # this shard's senders aren't in this workspace
            del remaining[domain]  # matched - don't search further workspaces
            # Anything the shard ingested inside the window puts it in the clear.
            freshest = _age_hours(newest[mail_host]) if mail_host in newest else None
            if freshest is None or freshest > STALL_HOURS:
                stalled.append({
                    "shard": domain, "workspace": w["name"],
                    "mailboxes_live": len(senders),
                    "hours_since_last_ingest": round(freshest, 1) if freshest is not None else None,
                })
                log(f"Layer 2 STALL: {domain} ({w['name']}) - "
                    f"{'no inbound ever' if freshest is None else str(round(freshest,1))+'h since last inbound'}")
    if remaining:
        log(f"Layer 2: {len(remaining)} live shard(s) had no sender with >={MIN_SENT} sends in any "
            f"workspace - not judged (likely new/low-volume): {list(remaining)}")
    if not stalled:
        log("Layer 2 OK: all matched shards ingesting within threshold")
    return stalled


# ---------- Layer 3: shard resource ceilings ----------
def layer3_shard_resources() -> list[dict]:
    """Catch the silent-ceiling class of failure on the shards themselves.

    Two ceilings put every shard into a degraded state for months without a
    single alert, because nothing watched them (see
    project-shard-dovecot-inotify-limits):

      - fs.inotify.max_user_instances: Dovecot takes one instance per watched
        mailbox. At the kernel default of 128 against ~225 demand, every shard
        sat pinned at 128/128 and Dovecot silently stopped watching the
        overflow, disabling new-mail notification for those mailboxes.
      - Dovecot service(imap-login) process_limit: in high-security mode it
        forks one process per connection, so ~400 sessions ran into a 500
        ceiling and connections were dropped outright.

    Both are now configured correctly, so this layer exists to notice the
    moment that regresses - a rebuilt shard, a reverted config, or simply a
    higher mailbox count pushing demand past the new headroom. It also flags
    config drift on imap-login directly, so we hear about it before saturation
    rather than after.

    Failsafe by design: any shard we cannot reach is logged and skipped, never
    alerted on, and a missing key skips the whole layer. A monitoring check must
    not become its own source of pages.
    """
    if not os.path.exists(SHARD_SSH_KEY):
        log(f"Layer 3 skipped: no shard SSH key at {SHARD_SSH_KEY}")
        return []
    try:
        sys.path.insert(0, "/app/scripts")
        from lib.mailserver import MailserverClient  # type: ignore
    except Exception as exc:
        log(f"Layer 3 skipped: cannot import MailserverClient ({exc})")
        return []

    findings, checked, unreachable = [], 0, 0
    for shard in active_shards():
        domain, ip = shard.get("domain"), shard.get("vps_ip")
        if not ip:
            continue
        try:
            ms = MailserverClient(ip, SHARD_SSH_KEY, user=SHARD_SSH_USER)
            ms.connect()
            try:
                ino = ms.verify_inotify_limits()
                dov = ms.verify_dovecot_limits()
                _, drops, _ = ms.sudo(
                    "docker exec mailserver sh -c "
                    "'grep -ac \"process_limit (.*) reached\" /var/log/mail/mail.log || true'",
                    check=False,
                )
            finally:
                ms.close()
        except Exception as exc:
            unreachable += 1
            log(f"Layer 3: {domain} unreachable, skipped ({type(exc).__name__})")
            continue

        checked += 1
        issues = []
        try:
            inuse, limit = int(ino.get("inuse", 0)), int(ino.get("inst", 0))
            if limit and (100.0 * inuse / limit) >= INOTIFY_WARN_PCT:
                issues.append(f"inotify {inuse}/{limit} ({100.0*inuse/limit:.0f}% of limit)")
        except (TypeError, ValueError):
            pass
        if dov.get("imap_login_service_count") != "0":
            issues.append(
                f"imap-login not in high-performance mode "
                f"(service_count={dov.get('imap_login_service_count')})"
            )
        try:
            n = int((drops or "0").strip().splitlines()[-1])
            if n > 0:
                issues.append(f"{n} 'process_limit reached' log line(s) - connections being dropped")
        except (ValueError, IndexError):
            pass

        if issues:
            findings.append({"shard": domain, "issues": issues})
            log(f"Layer 3 RESOURCE: {domain} - " + "; ".join(issues))

    if not findings:
        log(f"Layer 3 OK: {checked} shard(s) within resource ceilings"
            + (f" ({unreachable} unreachable, skipped)" if unreachable else ""))
    return findings


# ---------- alerting ----------
def alert(dns_bad, stalled, resources=None):
    lines = []
    if dns_bad:
        lines.append(f"{len(dns_bad)} dead mail-host AAAA record(s) (reply-ingestion risk): "
                     + ", ".join(sorted({d["zone"] for d in dns_bad})[:10]))
    if stalled:
        lines.append(f"{len(stalled)} shard(s) with stalled inbound ingestion: "
                     + ", ".join(s["shard"] for s in stalled[:10]))
    resources = resources or []
    if resources:
        lines.append(f"{len(resources)} shard(s) hitting a resource ceiling: "
                     + ", ".join(r["shard"] for r in resources[:10]))
    summary = "Cold-email ingestion monitor: " + " | ".join(lines)
    payload = {
        "source": "coldemail-ingestion-monitor",
        # A resource ceiling is degradation, not an outage - only page as
        # critical when ingestion or DNS is actually broken.
        "severity": "critical" if (dns_bad or stalled) else "warning",
        "summary": summary,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "dns_drift": dns_bad,
        "stalled_shards": stalled,
        "resource_ceilings": resources,
    }
    log("ALERT: " + summary)
    if WEBHOOK:
        try:
            r = requests.post(WEBHOOK, json=payload, timeout=20)
            log(f"alert posted to n8n webhook -> HTTP {r.status_code}")
        except Exception as e:
            log(f"!! failed to POST alert: {e}")
    else:
        log("N8N_ALERT_WEBHOOK_URL unset - alert logged only")


def main():
    log("=== ingestion health check start ===")
    dns_bad = layer1_dns_drift()
    stalled = layer2_ingestion_stall()
    resources = layer3_shard_resources()
    if dns_bad or stalled or resources:
        alert(dns_bad, stalled, resources)
        log("=== finished WITH findings ===")
    else:
        log("=== finished: all healthy ===")


if __name__ == "__main__":
    main()
