#!/usr/bin/env python3
"""Inbound-ingestion health monitor for the cold-email fleet.

Two checks, both aimed at the failure mode that bit us in Jul 2026: a shard's
Bison IMAP reply-ingestion silently stalls while Bison still reports the sender
as "Connected". Status flags lie, so we watch *behaviour* instead.

  Layer 1 - DNS drift guard (prevention):
    Scans every Cloudflare zone for a `mail.*` AAAA record whose content is a
    /64-base address (ends in `::`). That is the exact dead-record that broke
    reply ingestion fleet-wide. Any hit = a shard (new or regressed) is about
    to go dark. Cheap; catches the cause before the symptom.

  Layer 2 - ingestion-stall detector (detection, any cause):
    Every actively-sending mailbox receives inbound constantly (warmup, bounces,
    real replies). So an active shard whose *freshest ingested item* is older
    than STALL_HOURS is stalled - regardless of whether the cause is DNS, a
    cert, auth, or a dropped IMAP connection. Rolled up per shard so one dead
    shard is one alert, not 100.

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
  SAMPLE_PER_SHARD (default 3)- mailboxes sampled per shard
"""
from __future__ import annotations
import json, os, sys
from datetime import datetime, timezone

import requests

BASE = os.environ.get("BISON_API_BASE", "").rstrip("/")
SUPER = os.environ.get("EB_SUPERADMIN_KEY", "")
CF_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "")
SB_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SB_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
WEBHOOK = os.environ.get("N8N_ALERT_WEBHOOK_URL", "")
STALL_HOURS = float(os.environ.get("STALL_HOURS", "18"))
MIN_SENT = int(os.environ.get("MIN_SENT", "5"))
SAMPLE_PER_SHARD = int(os.environ.get("SAMPLE_PER_SHARD", "3"))
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
           "?select=domain,bison_workspace,status,bison_loaded"
           "&status=in.(active,verified)&bison_loaded=eq.true")
    r = requests.get(url, headers={"apikey": SB_KEY, "Authorization": f"Bearer {SB_KEY}"}, timeout=30)
    return r.json()


def layer2_ingestion_stall() -> list[dict]:
    if not (BASE and SUPER and SB_URL and SB_KEY):
        log("Layer 2 skipped: missing BISON/SUPABASE env")
        return []
    shards = active_shards()
    ws_list = bison("/api/workspaces").get("data", [])
    ws_id = {w["name"]: w["id"] for w in ws_list}
    # group shards by workspace so we switch once per workspace
    by_ws: dict[str, list[str]] = {}
    for s in shards:
        by_ws.setdefault(s.get("bison_workspace") or "", []).append(s["domain"])

    stalled = []
    for ws_name, domains in by_ws.items():
        wid = ws_id.get(ws_name)
        if not wid:
            log(f"Layer 2: workspace '{ws_name}' not found via superadmin; skipping {len(domains)} shard(s)")
            continue
        bison("/api/workspaces/switch-workspace", "POST", {"team_id": wid})
        for domain in domains:
            mail_host = f"mail.{domain}"
            res = bison(f"/api/sender-emails?search={domain}&per_page=25")
            senders = [s for s in (res.get("data") or [])
                       if s.get("type") == "custom" and s.get("imap_server") == mail_host
                       and (s.get("emails_sent_count") or 0) >= MIN_SENT]
            if not senders:
                continue  # not a live sending shard (or too new to judge)
            freshest = None
            for s in senders[:SAMPLE_PER_SHARD]:
                rep = bison(f"/api/sender-emails/{s['id']}/replies?per_page=1")
                data = rep.get("data") or []
                if data:
                    age = _age_hours(data[0]["created_at"])
                    freshest = age if freshest is None else min(freshest, age)
            if freshest is None or freshest > STALL_HOURS:
                stalled.append({
                    "shard": domain, "workspace": ws_name,
                    "senders_checked": len(senders[:SAMPLE_PER_SHARD]),
                    "hours_since_last_ingest": round(freshest, 1) if freshest is not None else None,
                })
                log(f"Layer 2 STALL: {domain} ({ws_name}) - "
                    f"{'no inbound ever' if freshest is None else str(round(freshest,1))+'h since last inbound'}")
    if not stalled:
        log("Layer 2 OK: all active shards ingesting within threshold")
    return stalled


# ---------- alerting ----------
def alert(dns_bad, stalled):
    lines = []
    if dns_bad:
        lines.append(f"{len(dns_bad)} dead mail-host AAAA record(s) (reply-ingestion risk): "
                     + ", ".join(sorted({d["zone"] for d in dns_bad})[:10]))
    if stalled:
        lines.append(f"{len(stalled)} shard(s) with stalled inbound ingestion: "
                     + ", ".join(s["shard"] for s in stalled[:10]))
    summary = "Cold-email ingestion monitor: " + " | ".join(lines)
    payload = {
        "source": "coldemail-ingestion-monitor",
        "severity": "critical",
        "summary": summary,
        "detected_at": datetime.now(timezone.utc).isoformat(),
        "dns_drift": dns_bad,
        "stalled_shards": stalled,
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
    if dns_bad or stalled:
        alert(dns_bad, stalled)
        log("=== finished WITH findings ===")
    else:
        log("=== finished: all healthy ===")


if __name__ == "__main__":
    main()
