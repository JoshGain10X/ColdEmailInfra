"""Daily Instantly warmup state poller.

For every row in `instantly_warmup_state` whose status is `initial_warmup`
or `maintenance_warmup`:
  1. Batch query Instantly /accounts/warmup-analytics (chunks of 100 emails)
  2. Update health_score, days_warming, emails_sent_today, last_polled_at
  3. Auto-transition initial_warmup -> maintenance_warmup once
     now() >= initial_warmup_ends_at

Runs daily 06:30 UTC via cron, well-separated from the snapshot (07:00)
and intent-processor (every :30) crons.
"""

from __future__ import annotations

import sys
import json
import datetime as dt
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent))
from bison_pull import ENV  # type: ignore
from instantly_admin import warmup_analytics  # type: ignore

SB_URL = (
    ENV.get("SUPABASE_COLD_EMAIL_URL")
    or ENV.get("SUPABASE_URL", "")
).rstrip("/")
SB_SERVICE_KEY = (
    ENV.get("SUPABASE_COLD_EMAIL_SERVICE_KEY")
    or ENV.get("SUPABASE_COLD_EMAIL_ANON_KEY")
    or ENV.get("SUPABASE_SERVICE_KEY", "")
)

BATCH_SIZE = 100  # Instantly recommends <= 100 emails per warmup-analytics call


def _sb_headers() -> dict:
    if not SB_URL or not SB_SERVICE_KEY:
        raise RuntimeError("Supabase env not set")
    return {
        "apikey": SB_SERVICE_KEY,
        "Authorization": f"Bearer {SB_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def _ts() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + "Z"


def _fetch_active_rows() -> list[dict]:
    """Pull all rows with status in (initial_warmup, maintenance_warmup).

    PostgREST caps a single response at 1000 rows. We paginate via Range header.
    """
    out: list[dict] = []
    page_size = 1000
    offset = 0
    while True:
        r = requests.get(
            f"{SB_URL}/rest/v1/instantly_warmup_state",
            headers={**_sb_headers(), "Range": f"{offset}-{offset + page_size - 1}"},
            params={
                "select": "id,email,status,initial_warmup_ends_at",
                "status": "in.(initial_warmup,maintenance_warmup)",
                "order": "id.asc",
            },
            timeout=30,
        )
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_size:
            break
        offset += page_size
    return out


def _patch_row(row_id: int, payload: dict) -> None:
    r = requests.patch(
        f"{SB_URL}/rest/v1/instantly_warmup_state",
        headers={**_sb_headers(), "Prefer": "return=minimal"},
        params={"id": f"eq.{row_id}"},
        json=payload,
        timeout=30,
    )
    r.raise_for_status()


def _bulk_patch(rows: list[dict], updates_by_email: dict) -> tuple[int, int]:
    """For each row, look up its email in updates_by_email and PATCH if matched."""
    matched = 0
    transitioned = 0
    now = dt.datetime.now(dt.timezone.utc)
    for row in rows:
        update = updates_by_email.get(row["email"].lower())
        ends_at_raw = row.get("initial_warmup_ends_at")
        should_transition = False
        if row["status"] == "initial_warmup" and ends_at_raw:
            try:
                ends_at = dt.datetime.fromisoformat(ends_at_raw.replace("Z", "+00:00"))
                if now >= ends_at:
                    should_transition = True
            except ValueError:
                pass

        patch: dict = {"last_polled_at": _ts()}
        if update:
            matched += 1
            if "health_score" in update:
                patch["health_score"] = update["health_score"]
            if "days_warming" in update:
                patch["days_warming"] = update["days_warming"]
            if "emails_sent_today" in update:
                patch["emails_sent_today"] = update["emails_sent_today"]
            patch["last_error"] = None
        if should_transition:
            patch["status"] = "maintenance_warmup"
            transitioned += 1

        try:
            _patch_row(row["id"], patch)
        except Exception as e:
            print(f"  ERROR patching row#{row['id']}: {e}", file=sys.stderr)
    return matched, transitioned


def _normalise_analytics(response: dict | list) -> dict[str, dict]:
    """Map email -> {health_score, days_warming, emails_sent_today}.

    Instantly's actual response shape:
        {
          "email_date_data": {<email>: {<YYYY-MM-DD>: {sent, received, landed_inbox, ...}}, ...},
          "aggregate_data":  {<email>: {sent_count, landed_inbox_count, health_score, ...}, ...}
        }
    We use aggregate_data for the snapshot fields. health_score may live there
    or in a nested object — handle either.
    """
    out: dict[str, dict] = {}
    if isinstance(response, list):
        # Legacy / alternate shape; keep for resilience.
        for r in response:
            if not isinstance(r, dict):
                continue
            email = (r.get("email") or "").lower()
            if email:
                out[email] = {
                    "health_score": r.get("health_score") or r.get("score"),
                    "days_warming": r.get("days_warming") or r.get("warmup_days"),
                    "emails_sent_today": r.get("emails_sent_today") or r.get("today"),
                }
        return out
    if not isinstance(response, dict):
        return out
    agg = response.get("aggregate_data") or {}
    date_data = response.get("email_date_data") or {}
    for email, stats in agg.items():
        if not isinstance(stats, dict):
            continue
        # Pull whatever Instantly exposes; field names vary across versions
        health = stats.get("health_score") or stats.get("score") or stats.get("warmup_score")

        # days_warming - DERIVED, not returned by the API any more (since at least
        # 2026-06). The aggregate_data section no longer carries it; instead we
        # count distinct YYYY-MM-DD keys in email_date_data[email] which represent
        # the days Instantly has activity for this mailbox. That's our proxy for
        # warming age and the only signal we have for the "is the 100% score yet
        # meaningful" gate.
        days = stats.get("days_warming") or stats.get("warmup_days")
        if days is None and isinstance(date_data.get(email), dict):
            days_keys = [k for k in date_data[email].keys() if isinstance(k, str) and len(k) == 10 and k[4] == '-']
            days = len(days_keys) if days_keys else None

        # emails_sent_today: take today's row from email_date_data
        today = stats.get("emails_sent_today") or stats.get("today") or stats.get("sent_today")
        if today is None and isinstance(date_data.get(email), dict):
            from datetime import date as _date
            todays_key = _date.today().isoformat()
            today_row = date_data[email].get(todays_key)
            if isinstance(today_row, dict):
                today = today_row.get("sent") or today_row.get("emails_sent_count")
        out[email.lower()] = {
            "health_score": health,
            "days_warming": days,
            "emails_sent_today": today,
        }
    return out


def main() -> int:
    print(f"=== warmup poller start {_ts()} ===", file=sys.stderr)
    rows = _fetch_active_rows()
    print(f"  {len(rows)} active warmup rows to poll", file=sys.stderr)
    if not rows:
        print("  nothing to do", file=sys.stderr)
        return 0

    # Batch query Instantly
    total_matched = 0
    total_transitioned = 0
    failures = 0
    for i in range(0, len(rows), BATCH_SIZE):
        chunk = rows[i : i + BATCH_SIZE]
        emails = [r["email"] for r in chunk]
        print(f"  batch {i // BATCH_SIZE + 1}: {len(emails)} emails", file=sys.stderr)
        try:
            analytics = warmup_analytics(emails)
        except Exception as e:
            failures += 1
            print(f"    ERROR fetching analytics: {e}", file=sys.stderr)
            continue
        updates = _normalise_analytics(analytics)
        matched, transitioned = _bulk_patch(chunk, updates)
        total_matched += matched
        total_transitioned += transitioned

    print(
        f"=== done {_ts()} | matched={total_matched} transitioned={total_transitioned} failures={failures} ===",
        file=sys.stderr,
    )
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
