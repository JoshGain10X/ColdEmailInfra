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
    """Active = status in (active,verified) AND bison_loaded AND no in-flight retirement.

    Retirement table is `bison_domain_retirements` (NOT bison_root_retirements
    which doesn't exist — the table name was wrong in an earlier draft).
    """
    sb = _sb()
    rows = (
        sb.table("infra_shards")
        .select("domain,client_id,bison_loaded,clients!inner(slug)")
        .in_("status", ["active", "verified"])
        .eq("bison_loaded", True)
        .execute().data or []
    )
    retirements = (
        sb.table("bison_domain_retirements")
        .select("root_domain,status")
        .not_.in_("status", ["cancelled", "reversed", "torn_down"])
        .execute().data or []
    )
    blocked = {r["root_domain"].lower() for r in retirements}
    return [r for r in rows if r["domain"].lower() not in blocked]


def _resolve_nameservers(domain: str) -> tuple[str, ...]:
    """Return the sorted tuple of NS hostnames for a domain.

    Uses dnspython with explicit upstream resolvers (1.1.1.1 + 8.8.8.8) because
    the container's default /etc/resolv.conf often points at a resolver that
    refuses NS queries for arbitrary external zones — silent empty answers,
    which collapsed the whole fleet down to the 2 pairs we got out of the first
    attempt. Short 3s timeout per resolver so a sweep across ~50 shards stays
    well inside the FastAPI 60s budget.
    """
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = ["1.1.1.1", "8.8.8.8"]
        resolver.lifetime = 3.0
        resolver.timeout = 3.0
        answers = resolver.resolve(domain, "NS")
        return tuple(sorted(str(r.target).rstrip(".").lower() for r in answers))
    except Exception:
        return ()


def _distinct_nameserver_reps(sb: Client, shards: list[dict]) -> list[str]:
    """Return one representative domain per distinct NS pair across the fleet.

    We don't store nameservers in infra_domains — resolve them live via DNS at
    scan time. Cloudflare assigns 2 of ~700 NS hosts per zone, so 50 zones in
    the 10X account cluster onto roughly 8-15 distinct pairs. Parallel resolution
    keeps the sweep under 5s even at fleet size.
    """
    from collections import defaultdict
    from concurrent.futures import ThreadPoolExecutor

    domains = [s["domain"] for s in shards]
    by_pair: dict[tuple[str, ...], list[str]] = defaultdict(list)
    with ThreadPoolExecutor(max_workers=10) as pool:
        for domain, ns in zip(domains, pool.map(_resolve_nameservers, domains)):
            if ns:
                by_pair[ns].append(domain)
    return [pair_domains[0] for pair_domains in by_pair.values()]


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
    """Fire the bulk scan, then poll for completion.

    Two-phase to avoid stranding rows as 'queued' in spamhaus_checks (which the
    CRM dialog filters out via latest_spamhaus_per_shard.status='completed'):
      1. POST each check in parallel, persist queued rows with their UUIDs
      2. Poll every 5s for up to 45s, downloading + persisting completed rows

    EmailGuard's Spamhaus endpoints typically complete within 1-3s when the
    domain is well-known, slightly longer for fresh ones. 45s comfortably
    covers ~10 checks (the typical NS fleet) plus headroom. Domain-reputation
    fleet scans across the full fleet (~50 checks) would exceed this — for
    those, we'd want background polling, but they're not a current use case.
    """
    sb = _sb()
    shards = _active_shards()
    if check_type == "nameserver_reputation":
        targets_with_slug = [(t, None) for t in _distinct_nameserver_reps(sb, shards)]
    else:
        targets_with_slug = [(s["domain"], (s.get("clients") or {}).get("slug")) for s in shards]

    # Phase 1: kick off every check, capture UUIDs
    pending: list[tuple[str, str]] = []  # (uuid, target)
    for target, slug in targets_with_slug:
        try:
            res = run_ad_hoc_check(check_type, target, target, slug, max_poll_seconds=0)
            uuid = res.get("check_uuid")
            if uuid:
                pending.append((uuid, target))
            time.sleep(0.2)
        except Exception:
            continue

    # Phase 2: poll until everything is completed or budget exhausted
    _, _, get_base = CHECK_PATHS[check_type]
    start = time.time()
    completed = 0
    while pending and (time.time() - start) < 45:
        time.sleep(5)
        still_pending: list[tuple[str, str]] = []
        for uuid, target in pending:
            try:
                gr = requests.get(f"{EMAILGUARD_BASE}{get_base}/{uuid}", headers=_headers(), timeout=15)
                if gr.status_code == 404:
                    still_pending.append((uuid, target))
                    continue
                gr.raise_for_status()
                data = (gr.json() or {}).get("data") or {}
                if data.get("status") == "completed":
                    sb.table("spamhaus_checks").update({
                        "status": "completed",
                        "result": data,
                        "completed_at": datetime.now(timezone.utc).isoformat(),
                        **_extract_summary(check_type, data),
                    }).eq("check_uuid", uuid).execute()
                    completed += 1
                else:
                    still_pending.append((uuid, target))
            except Exception:
                still_pending.append((uuid, target))
        pending = still_pending

    return {
        "check_type": check_type,
        "fired": len(targets_with_slug),
        "completed": completed,
        "still_queued": len(pending),
        "credit_cost": len(targets_with_slug) * CHECK_COSTS[check_type],
    }
