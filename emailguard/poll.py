#!/usr/bin/env python3
"""EmailGuard inbox-placement-test poller — one tick of the */5 cron.

Lists every test on the EmailGuard workspace via their HTTP API, upserts the
header into `emailguard_placement_tests`, and the per-seed rows into
`emailguard_placement_emails`. Idempotent via `test_uuid` UNIQUE on the parent
table and `(test_id, seed_email)` UNIQUE on the children.

Tests whose `name` field starts with `AUTO|<shard>|<sender>|<unix_ts>` get
their `shard_domain` + `sender_email` columns parsed out automatically.
Tests created outside our flow (manual web-UI / CLI) get shard_domain=NULL
and surface in any "unmatched tests" admin views later.

Run inside the coldemail-emailguard-ingest container; supercronic invokes
this every 5 min. Logs go to stdout → docker json log driver.
"""
from __future__ import annotations

import os
import sys
import traceback
from datetime import datetime, timezone
from typing import Any

import requests

sys.path.insert(0, "/app/scripts")
from supabase import Client, create_client  # noqa: E402


EMAILGUARD_BASE = os.environ.get("EMAILGUARD_BASE", "https://app.emailguard.io")
EMAILGUARD_KEY = os.environ["EMAILGUARD_API_KEY"]


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def _sb() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def _parse_auto_name(name: str) -> tuple[str | None, str | None]:
    """Parse 'AUTO|<shard_domain>|<sender_email>|<ts>' → (shard_domain, sender_email).
    Returns (None, None) if the name doesn't match the AUTO prefix or splits
    fewer than 4 parts."""
    if not name or not name.startswith("AUTO|"):
        return None, None
    parts = name.split("|", 3)
    if len(parts) < 4:
        return None, None
    shard = parts[1].strip() or None
    sender = parts[2].strip() or None
    return shard, sender


def _client_slug_for_shard(sb: Client, shard_domain: str | None) -> str | None:
    if not shard_domain:
        return None
    res = (
        sb.table("infra_shards")
        .select("client_id,clients!inner(slug)")
        .eq("domain", shard_domain)
        .limit(1)
        .execute()
    )
    rows = res.data or []
    if not rows:
        return None
    return (rows[0].get("clients") or {}).get("slug")


def list_tests() -> list[dict[str, Any]]:
    """GET /api/inbox-placement-tests — returns ALL tests on the workspace.
    EmailGuard's API doesn't expose pagination via the CLI but the underlying
    endpoint does. Single page request first; if we ever hit >250 we'll add
    pagination."""
    url = f"{EMAILGUARD_BASE}/api/v1/inbox-placement-tests"
    headers = {"Authorization": f"Bearer {EMAILGUARD_KEY}"}
    r = requests.get(url, headers=headers, timeout=30, params={"per_page": 250})
    r.raise_for_status()
    body = r.json()
    return body.get("data", []) if isinstance(body, dict) else []


def get_test(test_uuid: str) -> dict[str, Any] | None:
    """GET /api/inbox-placement-tests/{uuid} — full per-seed detail."""
    url = f"{EMAILGUARD_BASE}/api/v1/inbox-placement-tests/{test_uuid}"
    headers = {"Authorization": f"Bearer {EMAILGUARD_KEY}"}
    r = requests.get(url, headers=headers, timeout=30)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    body = r.json()
    return body.get("data") if isinstance(body, dict) else None


def upsert_test(sb: Client, t: dict[str, Any]) -> int:
    """Upsert one test header. Returns the local PK id."""
    shard, sender = _parse_auto_name(t.get("name", ""))
    client_slug = _client_slug_for_shard(sb, shard)
    payload = {
        "test_uuid": t["uuid"],
        "name": t.get("name") or "",
        "shard_domain": shard,
        "client_slug": client_slug,
        "sender_email": sender,
        "status": t.get("status") or "unknown",
        "overall_score": t.get("overall_score"),
        "google_workspace_emails_count": t.get("google_workspace_emails_count"),
        "microsoft_professional_emails_count": t.get("microsoft_professional_emails_count"),
        "filter_phrase": t.get("filter_phrase"),
        "created_at": t.get("created_at"),
        "completed_at": t.get("completed_at"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    res = (
        sb.table("emailguard_placement_tests")
        .upsert(payload, on_conflict="test_uuid")
        .execute()
    )
    return res.data[0]["id"] if res.data else None  # type: ignore[return-value]


def upsert_emails(sb: Client, test_pk: int, emails: list[dict[str, Any]]) -> int:
    if not emails:
        return 0
    rows = [
        {
            "test_id": test_pk,
            "seed_email": e.get("email"),
            "provider": e.get("provider"),
            "status": e.get("status"),
            "folder": e.get("folder"),
        }
        for e in emails
        if e.get("email")
    ]
    if not rows:
        return 0
    sb.table("emailguard_placement_emails").upsert(rows, on_conflict="test_id,seed_email").execute()
    return len(rows)


def main() -> int:
    sb = _sb()
    try:
        tests = list_tests()
    except Exception as exc:
        _log(f"FATAL listing tests: {type(exc).__name__}: {exc}")
        return 1

    _log(f"listed {len(tests)} tests on workspace")
    tests_upserted = 0
    emails_upserted = 0
    in_progress_uuids: list[str] = []

    for t in tests:
        try:
            pk = upsert_test(sb, t)
            tests_upserted += 1
            # The list endpoint includes inbox_placement_test_emails inline
            # when present; if not, we'll need to GET the detail.
            emails = t.get("inbox_placement_test_emails") or []
            if not emails and t.get("status") == "complete":
                detail = get_test(t["uuid"])
                emails = (detail or {}).get("inbox_placement_test_emails") or []
            emails_upserted += upsert_emails(sb, pk, emails)
            if t.get("status") in ("in_progress", "created"):
                in_progress_uuids.append(t["uuid"])
        except Exception as exc:
            _log(f"  ERR test {t.get('uuid','?')}: {type(exc).__name__}: {exc}")

    # For in-progress tests we want fresh per-seed status so the CRM sees
    # placement landing live. Re-fetch detail for those.
    for uuid in in_progress_uuids:
        try:
            detail = get_test(uuid)
            if not detail:
                continue
            pk_row = (
                sb.table("emailguard_placement_tests")
                .select("id")
                .eq("test_uuid", uuid)
                .limit(1)
                .execute()
                .data
            )
            if not pk_row:
                continue
            emails_upserted += upsert_emails(sb, pk_row[0]["id"], detail.get("inbox_placement_test_emails") or [])
        except Exception as exc:
            _log(f"  ERR detail {uuid}: {type(exc).__name__}: {exc}")

    _log(f"done tests_upserted={tests_upserted} emails_upserted={emails_upserted} in_progress={len(in_progress_uuids)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
