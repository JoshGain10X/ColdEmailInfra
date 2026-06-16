#!/usr/bin/env python3
"""Daily blacklist scan — one EmailGuard check per active-shard IP + root domain.

For each shard with status active/verified and a non-null vps_ip:
  1. POST /api/v1/blacklist-checks with {domain: <vps_ip>}      -> async UUID
  2. POST /api/v1/blacklist-checks with {domain: <root_domain>} -> async UUID
  3. Insert both rows into blacklist_checks with status='in_progress'
  4. Poll each UUID every 30s for up to 10 min; update row when completed

Output: one row per (target, day) in blacklist_checks. The view
latest_blacklist_checks materialises 'current state' per target; the
shard_health_recommendations view joins that into the CRM dashboard.

Run inside the coldemail-blacklist-monitor container; supercronic fires at
06:00 UTC daily. Also runnable on-demand via `docker exec ... python poll.py`.
"""
from __future__ import annotations

import os
import sys
import time
import traceback
from datetime import datetime, timezone

import requests

sys.path.insert(0, "/app/scripts")
from supabase import Client, create_client  # noqa: E402


EMAILGUARD_BASE = os.environ.get("EMAILGUARD_BASE", "https://app.emailguard.io")
EMAILGUARD_KEY = os.environ["EMAILGUARD_API_KEY"]
POLL_INTERVAL = 30      # seconds between polls
MAX_POLL_TIME = 600     # 10 minutes total per check


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def _sb() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def initiate_check(target: str) -> dict | None:
    """POST an ad-hoc check, return the created object (with uuid, status=in_progress).

    Endpoint: POST /api/v1/blacklist-checks/ad-hoc
    Body field: domain_or_ip (NOT 'domain' — confirmed via CLI source on 2026-06-16).
    """
    url = f"{EMAILGUARD_BASE}/api/v1/blacklist-checks/ad-hoc"
    headers = {"Authorization": f"Bearer {EMAILGUARD_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json={"domain_or_ip": target}, timeout=30)
        r.raise_for_status()
        return r.json().get("data")
    except Exception as exc:
        _log(f"  initiate err for {target}: {exc}")
        return None


def get_check(uuid: str) -> dict | None:
    url = f"{EMAILGUARD_BASE}/api/v1/blacklist-checks/{uuid}"
    headers = {"Authorization": f"Bearer {EMAILGUARD_KEY}"}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("data")
    except Exception as exc:
        _log(f"  get err for {uuid}: {exc}")
        return None


def insert_row(sb: Client, target: str, target_type: str, shard_domain: str | None,
               client_slug: str | None, data: dict) -> int | None:
    """Insert in_progress row; return PK id."""
    payload = {
        "check_uuid": data["uuid"],
        "target_type": target_type,
        "target": target,
        "shard_domain": shard_domain,
        "client_slug": client_slug,
        "status": data.get("status", "in_progress"),
        "blacklists_count": data.get("blacklists_count") or 0,
        "blacklists": data.get("blacklists"),
        "resolved_ip": data.get("ip"),
    }
    res = sb.table("blacklist_checks").upsert(payload, on_conflict="check_uuid").execute()
    return res.data[0]["id"] if res.data else None


def update_row(sb: Client, uuid: str, data: dict) -> None:
    payload = {
        "status": data.get("status"),
        "blacklists_count": data.get("blacklists_count") or 0,
        "blacklists": data.get("blacklists"),
        "resolved_ip": data.get("ip"),
        "completed_at": datetime.now(timezone.utc).isoformat() if data.get("status") == "completed" else None,
    }
    sb.table("blacklist_checks").update(payload).eq("check_uuid", uuid).execute()


def initiate_surbl(domain: str) -> dict | None:
    """POST a SURBL check. Synchronous — response is already completed.

    SURBL flags domains found in URI / mailbody blocklists (separate from
    IP DNSBLs). EmailGuard's SURBL endpoint returns immediately:
      { uuid, domain, status: "completed", listed: bool }
    """
    url = f"{EMAILGUARD_BASE}/api/v1/surbl-blacklist-checks"
    headers = {"Authorization": f"Bearer {EMAILGUARD_KEY}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json={"domain": domain}, timeout=30)
        r.raise_for_status()
        return r.json().get("data")
    except Exception as exc:
        _log(f"  surbl err for {domain}: {exc}")
        return None


def upsert_surbl(sb: Client, domain: str, shard_domain: str | None,
                 client_slug: str | None, data: dict) -> None:
    payload = {
        "check_uuid": data["uuid"],
        "target": domain,
        "shard_domain": shard_domain,
        "client_slug": client_slug,
        "listed": bool(data.get("listed")),
    }
    sb.table("surbl_checks").upsert(payload, on_conflict="check_uuid").execute()


def main() -> int:
    sb = _sb()
    shards = (
        sb.table("infra_shards")
        .select("domain,vps_ip,status,client_id,clients!inner(slug)")
        .in_("status", ["active", "verified"])
        .execute().data or []
    )
    shards = [s for s in shards if s.get("vps_ip")]
    _log(f"scanning {len(shards)} active shards (DNSBL: 1 IP + 1 domain; SURBL: 1 domain)")

    # Phase 1: kick off every check, store the UUIDs
    pending: list[tuple[str, str, str, str | None]] = []  # (uuid, target, target_type, shard_domain)
    for shard in shards:
        domain = shard["domain"]
        ip = shard["vps_ip"]
        client_slug = (shard.get("clients") or {}).get("slug")

        ip_check = initiate_check(ip)
        if ip_check and ip_check.get("uuid"):
            insert_row(sb, ip, "ip", domain, client_slug, ip_check)
            pending.append((ip_check["uuid"], ip, "ip", domain))

        domain_check = initiate_check(domain)
        if domain_check and domain_check.get("uuid"):
            insert_row(sb, domain, "domain", domain, client_slug, domain_check)
            pending.append((domain_check["uuid"], domain, "domain", domain))

        # SURBL is synchronous — fire it right after the DNSBL pair and
        # persist immediately. Cheap and unmetered (same family as DNSBL).
        surbl_data = initiate_surbl(domain)
        if surbl_data and surbl_data.get("uuid"):
            upsert_surbl(sb, domain, domain, client_slug, surbl_data)
            if surbl_data.get("listed"):
                _log(f"  ⚠ SURBL: {domain} is listed")

        # Tiny jitter so we don't burst — EmailGuard's per-account concurrency
        # limits aren't documented; 0.2s between init calls is conservative.
        time.sleep(0.2)

    _log(f"initiated {len(pending)} checks; polling for results")

    # Phase 2: poll until everything's done or budget exhausted
    start = time.time()
    still_pending = list(pending)
    completed = 0
    listed_warnings: list[str] = []
    while still_pending and (time.time() - start) < MAX_POLL_TIME:
        time.sleep(POLL_INTERVAL)
        next_round = []
        for entry in still_pending:
            uuid, target, target_type, shard_domain = entry
            data = get_check(uuid)
            if not data:
                continue
            if data.get("status") == "completed":
                update_row(sb, uuid, data)
                completed += 1
                count = data.get("blacklists_count") or 0
                if count > 0:
                    listed_warnings.append(f"  ⚠ {target_type}:{target} ({shard_domain}) listed on {count} blacklist(s)")
            else:
                next_round.append(entry)
        still_pending = next_round
        _log(f"  poll: {completed}/{len(pending)} complete, {len(still_pending)} pending")

    if still_pending:
        _log(f"timed out with {len(still_pending)} still in_progress (will resolve on next run)")

    if listed_warnings:
        _log("=== BLACKLIST HITS ===")
        for w in listed_warnings:
            _log(w)
    else:
        _log("clean across the fleet ✓")

    return 0 if not still_pending else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
