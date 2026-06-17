#!/usr/bin/env python3
"""Google Postmaster Tools daily poller.

For every domain registered + verified in Postmaster Tools:
  1. Refresh access token using the stored refresh token
  2. GET /v1/domains/<domain>/trafficStats?startDate=N-2&endDate=N-1
     (Google publishes stats with a 2-day lag, so we pull yesterday's stats today)
  3. Upsert into gpt_daily_stats keyed on (domain, date)
  4. Extracted summary fields land in flat columns for fast CRM queries; full
     payload kept in raw_payload for ad-hoc analysis.

Env:
  GPT_CLIENT_ID
  GPT_CLIENT_SECRET
  GPT_REFRESH_TOKEN
  SUPABASE_URL  (or SUPABASE_COLD_EMAIL_URL)
  SUPABASE_SERVICE_KEY  (or SUPABASE_COLD_EMAIL_SERVICE_KEY)
"""
from __future__ import annotations

import os
import sys
import json
import time
import traceback
from datetime import datetime, timezone, timedelta, date

import requests

GPT_API = "https://gmailpostmastertools.googleapis.com/v1"
OAUTH_TOKEN = "https://oauth2.googleapis.com/token"


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def _env(key: str, *alts: str) -> str | None:
    for k in (key, *alts):
        v = os.environ.get(k)
        if v:
            return v.strip()
    return None


def _refresh_access_token() -> str:
    client_id = _env("GPT_CLIENT_ID")
    client_secret = _env("GPT_CLIENT_SECRET")
    refresh_token = _env("GPT_REFRESH_TOKEN")
    if not all([client_id, client_secret, refresh_token]):
        raise RuntimeError("GPT_CLIENT_ID / GPT_CLIENT_SECRET / GPT_REFRESH_TOKEN not set in env")
    r = requests.post(OAUTH_TOKEN, data={
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def _list_domains(access_token: str) -> list[dict]:
    r = requests.get(f"{GPT_API}/domains", headers={"Authorization": f"Bearer {access_token}"}, timeout=30)
    r.raise_for_status()
    return (r.json() or {}).get("domains", [])


def _get_traffic_stats(access_token: str, domain_name: str, target_date: date) -> dict | None:
    """One day's stats for one domain. Google's API returns ONE TrafficStat object
    matching the target date when ?startDate=N&endDate=N. We pass the target date
    as ISO YYYY-MM-DD via startDate.year/month/day query params (Google's
    structured-date-key convention)."""
    params = {
        "startDate.year": target_date.year,
        "startDate.month": target_date.month,
        "startDate.day": target_date.day,
        "endDate.year": target_date.year,
        "endDate.month": target_date.month,
        "endDate.day": target_date.day,
    }
    r = requests.get(
        f"{GPT_API}/{domain_name}/trafficStats",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params,
        timeout=30,
    )
    if r.status_code == 404:
        return None
    if not r.ok:
        _log(f"  trafficStats error {r.status_code} for {domain_name}: {r.text[:200]}")
        return None
    data = (r.json() or {}).get("trafficStats", [])
    return data[0] if data else None


def _domain_from_resource(name: str) -> str:
    # Resource name is "domains/<idna-domain>". Strip the prefix.
    return name.split("/", 1)[-1] if name.startswith("domains/") else name


def _sb_upsert(rows: list[dict]) -> None:
    if not rows:
        return
    url = _env("SUPABASE_URL", "SUPABASE_COLD_EMAIL_URL")
    key = _env("SUPABASE_SERVICE_KEY", "SUPABASE_COLD_EMAIL_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("Supabase env not set")
    r = requests.post(
        f"{url.rstrip('/')}/rest/v1/gpt_daily_stats?on_conflict=domain,date",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=rows,
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"supabase upsert {r.status_code}: {r.text[:300]}")


def _normalise(domain: str, target_date: date, stats: dict) -> dict:
    """Map Google's TrafficStat JSON into our flat schema. Field names match
    Google's response - see https://developers.google.com/gmail/postmaster/reference/rest/v1/domains.trafficStats"""
    return {
        "domain": domain,
        "date": target_date.isoformat(),
        "domain_reputation": stats.get("domainReputation"),
        "ip_reputations": stats.get("ipReputations"),  # list of {reputation, ipCount, sampleIps[]}
        "spam_rate": stats.get("spammyFeedbackLoops"),  # historical name varies; primary is below
        "user_reported_spam_ratio": stats.get("userReportedSpamRatio"),
        "inbound_encryption_ratio": stats.get("inboundEncryptionRatio"),
        "outbound_encryption_ratio": stats.get("outboundEncryptionRatio"),
        "delivery_errors": stats.get("deliveryErrors"),
        "dkim_success_ratio": stats.get("dkimSuccessRatio"),
        "spf_success_ratio": stats.get("spfSuccessRatio"),
        "dmarc_success_ratio": stats.get("dmarcSuccessRatio"),
        "raw_payload": stats,
    }


def main() -> int:
    _log("=== gpt poll start ===")
    try:
        token = _refresh_access_token()
    except Exception as exc:
        _log(f"OAuth refresh failed: {exc}")
        return 2

    domains = _list_domains(token)
    _log(f"  postmaster has {len(domains)} domain(s) registered")

    # CRM-flow handoff: shards marked pending_verify should transition to
    # verified as soon as Google reports them as verified. Do this BEFORE the
    # trafficStats walk so the CRM badge updates promptly (the trafficStats
    # for a freshly-verified domain may not be ready for another 24-48h).
    sb_url = _env("SUPABASE_URL", "SUPABASE_COLD_EMAIL_URL")
    sb_key = _env("SUPABASE_SERVICE_KEY", "SUPABASE_COLD_EMAIL_SERVICE_KEY")
    if sb_url and sb_key:
        # Google's /v1/domains endpoint doesn't expose verificationStatus.
        # The actual fields are name, createTime, permission (NONE/READER/OWNER).
        # A domain only appears in this list once ownership verification has
        # succeeded - if `permission` is OWNER or READER, treat as verified.
        verified_names = {
            _domain_from_resource(d.get("name", ""))
            for d in domains
            if d.get("permission") in ("OWNER", "READER")
        }
        if verified_names:
            # Pick up ANY shard (pending_register OR pending_verify) where Google
            # now reports the domain as VERIFIED. Operators sometimes complete
            # verification via Google's own flow (e.g. domain was already
            # Google-Workspace-verified) and skip our CRM token-paste step.
            # The poller is the single source of truth - if Google says verified,
            # we treat it as verified regardless of how it got there.
            r = requests.get(
                f"{sb_url.rstrip('/')}/rest/v1/infra_shards?select=domain&gpt_status=in.(pending_register,pending_verify)",
                headers={"apikey": sb_key, "Authorization": f"Bearer {sb_key}"},
                timeout=30,
            )
            if r.ok:
                pending = {row["domain"] for row in (r.json() or [])}
                newly_verified = pending & verified_names
                if newly_verified:
                    requests.patch(
                        f"{sb_url.rstrip('/')}/rest/v1/infra_shards?domain=in.({','.join(newly_verified)})",
                        headers={
                            "apikey": sb_key,
                            "Authorization": f"Bearer {sb_key}",
                            "Content-Type": "application/json",
                            "Prefer": "return=minimal",
                        },
                        json={"gpt_status": "verified", "gpt_verified_at": datetime.now(timezone.utc).isoformat()},
                        timeout=30,
                    )
                    _log(f"  transitioned {len(newly_verified)} shard(s) pending_verify -> verified: {sorted(newly_verified)}")

    if not domains:
        _log("  nothing to poll - register domains via postmaster.google.com first")
        return 0

    # Google publishes stats with a 2-day lag. Pull yesterday's data
    # (target_date = today - 1). If we run at 07:00 UTC, the previous day's
    # stats are reliably available.
    target_date = (datetime.now(timezone.utc).date() - timedelta(days=1))
    _log(f"  pulling stats for {target_date}")

    rows: list[dict] = []
    for d in domains:
        name = _domain_from_resource(d.get("name", ""))
        if not name:
            continue
        # Only pull from domains where we have OWNER/READER permission - Google
        # only grants that after ownership verification succeeds. NONE means
        # the domain is in our list but verification hasn't completed.
        perm = d.get("permission")
        if perm not in ("OWNER", "READER"):
            _log(f"  skip {name} (permission: {perm})")
            continue
        try:
            stats = _get_traffic_stats(token, d["name"], target_date)
        except Exception as exc:
            _log(f"  trafficStats exception for {name}: {exc}")
            continue
        if not stats:
            _log(f"  {name}: no stats for {target_date} (likely sub-threshold volume)")
            continue
        rows.append(_normalise(name, target_date, stats))
        time.sleep(0.1)  # gentle pace; Google's quota is generous

    _log(f"  prepared {len(rows)} rows for upsert")
    if rows:
        _sb_upsert(rows)
        _log(f"  upserted {len(rows)} rows into gpt_daily_stats")
    _log("=== gpt poll done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
