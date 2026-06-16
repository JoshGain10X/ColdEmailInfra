"""Ad-hoc Spamhaus Intelligence check, used by the CRM diagnosis drawer.

Wraps the same persistence shape as blacklist/spamhaus_monthly.py so the
CRM only ever reads from one table (spamhaus_checks) regardless of whether
the row came from the monthly cron or an admin button.

Credits are metered (75/month shared across all Spamhaus endpoints) so the
endpoint is admin-gated upstream in the CRM — this function does not enforce
quotas itself.
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timezone

import requests
from supabase import Client, create_client


EMAILGUARD_BASE = os.environ.get("EMAILGUARD_BASE", "https://app.emailguard.io")

CHECK_PATHS = {
    "domain_reputation":     ("/api/v1/spamhaus-intelligence/domain-reputation/create",      "/api/v1/spamhaus-intelligence/domain-reputation"),
    "domain_context":        ("/api/v1/spamhaus-intelligence/domain-contexts/create",         "/api/v1/spamhaus-intelligence/domain-contexts"),
    "domain_senders":        ("/api/v1/spamhaus-intelligence/domain-senders/create",          "/api/v1/spamhaus-intelligence/domain-senders"),
    "a_record_reputation":   ("/api/v1/spamhaus-intelligence/a-record-reputation/create",     "/api/v1/spamhaus-intelligence/a-record-reputation"),
    "nameserver_reputation": ("/api/v1/spamhaus-intelligence/nameserver-reputation/create",   "/api/v1/spamhaus-intelligence/nameserver-reputation"),
}


def _sb() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['EMAILGUARD_API_KEY']}", "Content-Type": "application/json"}


def _extract_summary(check_type: str, data: dict) -> dict:
    out: dict = {}
    if check_type == "domain_reputation":
        out["reputation_score"] = data.get("reputation")
        bl = data.get("blacklist_listing_status") or {}
        out["is_listed"] = bl.get("is-listed")
        general = data.get("domain_general_info") or {}
        out["abused"] = general.get("abused")
        out["worst_score"] = data.get("reputation")
    elif check_type == "domain_senders":
        senders = data.get("sender") or []
        scores = [s.get("score") for s in senders if isinstance(s, dict) and s.get("score") is not None]
        out["worst_score"] = max(scores) if scores else None
    elif check_type == "a_record_reputation":
        recs = data.get("a_record_reputation") or []
        scores = [r.get("score") for r in recs if isinstance(r, dict) and r.get("score") is not None]
        out["worst_score"] = max(scores) if scores else None
    elif check_type == "nameserver_reputation":
        nss = data.get("nameserver_reputation") or []
        scores = [n.get("score") for n in nss if isinstance(n, dict) and n.get("score") is not None]
        out["worst_score"] = max(scores) if scores else None
    return out


def run_ad_hoc_check(check_type: str, domain: str,
                     shard_domain: str | None, client_slug: str | None,
                     max_poll_seconds: int = 60) -> dict:
    """POST a Spamhaus check, persist the queued row, poll briefly for completion.

    Returns the final spamhaus_checks row (or the queued state if it didn't
    complete within max_poll_seconds — caller can refresh later).
    """
    post_path, get_base = CHECK_PATHS[check_type]
    sb = _sb()

    # 1. Kick off
    r = requests.post(f"{EMAILGUARD_BASE}{post_path}", headers=_headers(),
                      json={"domain": domain}, timeout=30)
    r.raise_for_status()
    data = (r.json() or {}).get("data") or {}
    uuid = data.get("uuid")
    if not uuid:
        raise RuntimeError(f"EmailGuard did not return a uuid: {r.text[:200]}")

    payload = {
        "check_uuid": uuid,
        "check_type": check_type,
        "target": domain,
        "shard_domain": shard_domain,
        "client_slug": client_slug,
        "status": data.get("status", "queued"),
        "result": data,
        **_extract_summary(check_type, data),
    }
    sb.table("spamhaus_checks").upsert(payload, on_conflict="check_uuid").execute()

    # 2. Poll up to max_poll_seconds for completion
    start = time.time()
    while time.time() - start < max_poll_seconds:
        time.sleep(5)
        gr = requests.get(f"{EMAILGUARD_BASE}{get_base}/{uuid}", headers=_headers(), timeout=30)
        if gr.status_code == 404:
            continue
        gr.raise_for_status()
        latest = (gr.json() or {}).get("data") or {}
        if latest.get("status") == "completed":
            sb.table("spamhaus_checks").update({
                "status": "completed",
                "result": latest,
                "completed_at": datetime.now(timezone.utc).isoformat(),
                **_extract_summary(check_type, latest),
            }).eq("check_uuid", uuid).execute()
            return {"check_uuid": uuid, "status": "completed", "data": latest}

    return {"check_uuid": uuid, "status": "queued", "data": data}


CHECK_COSTS = {
    "domain_reputation":     4,
    "nameserver_reputation": 1,
    "a_record_reputation":   1,
    "domain_senders":        1,
    "domain_context":        1,
}


def _active_shards() -> list[dict]:
    sb = _sb()
    rows = (
        sb.table("infra_shards")
        .select("domain,client_id,bison_loaded,clients!inner(slug)")
        .in_("status", ["active", "verified"])
        .eq("bison_loaded", True)
        .execute().data or []
    )
    retirements = (
        sb.table("bison_root_retirements")
        .select("root_domain,status")
        .not_.in_("status", ["cancelled", "reversed", "torn_down"])
        .execute().data or []
    )
    blocked = {r["root_domain"].lower() for r in retirements}
    return [r for r in rows if r["domain"].lower() not in blocked]


def _distinct_nameserver_reps(sb: Client, shards: list[dict]) -> list[str]:
    """Return one representative domain per distinct NS pair across the fleet.

    Cloudflare clusters many domains onto the same NS pair, so this collapses
    ~50 shards into 6-12 representative checks.
    """
    from collections import defaultdict
    by_pair = defaultdict(list)
    domain_names = [s["domain"] for s in shards]
    domain_rows = (
        sb.table("infra_domains").select("domain,nameservers")
        .in_("domain", domain_names).execute().data or []
    )
    for row in domain_rows:
        ns = tuple(sorted((row.get("nameservers") or [])))
        if ns:
            by_pair[ns].append(row["domain"])
    return [domains[0] for domains in by_pair.values()]


def estimate_fleet_scan(check_type: str) -> dict:
    """Dry-run estimate — returns target count + credit cost without firing.

    Lets the CRM show a confirmation dialog with the credit hit before the user
    commits. No API calls made.
    """
    sb = _sb()
    shards = _active_shards()
    if check_type == "nameserver_reputation":
        targets = _distinct_nameserver_reps(sb, shards)
        return {
            "check_type": check_type,
            "target_count": len(targets),
            "credit_cost": len(targets) * CHECK_COSTS[check_type],
            "targets_preview": targets[:5],
        }
    # domain_reputation: one per shard
    return {
        "check_type": check_type,
        "target_count": len(shards),
        "credit_cost": len(shards) * CHECK_COSTS[check_type],
        "targets_preview": [s["domain"] for s in shards][:5],
    }


def run_fleet_scan(check_type: str) -> dict:
    """Fire the bulk scan. Returns the count actually initiated.

    Does NOT wait for completion — each individual check polls itself via
    the daily cron's polling phase (if we add it later) or via the CRM's
    auto-refresh. Pollers can pick up queued rows by check_uuid.
    """
    sb = _sb()
    shards = _active_shards()
    if check_type == "nameserver_reputation":
        targets_with_slug = [(t, None) for t in _distinct_nameserver_reps(sb, shards)]
    else:
        targets_with_slug = [(s["domain"], (s.get("clients") or {}).get("slug")) for s in shards]

    fired = 0
    for target, slug in targets_with_slug:
        try:
            run_ad_hoc_check(check_type, target, target, slug, max_poll_seconds=0)
            fired += 1
            time.sleep(0.2)  # gentle pace to avoid concurrency limits
        except Exception:
            continue

    return {
        "check_type": check_type,
        "fired": fired,
        "credit_cost": fired * CHECK_COSTS[check_type],
    }
