#!/usr/bin/env python3
"""Retry Instantly warmup enable for senders stuck in status='enable_failed'.

Use when a load-to-bison job exhausted its 30-min retry budget against
Instantly's update-warmup-accounts lock and rows landed as enable_failed
with a 409 last_error. Their Instantly accounts already exist; we just
need to flip the warmup-enabled bit.

Usage:
  python3 retry_warmup_enable.py <root_domain>
  python3 retry_warmup_enable.py <root_domain> --workspace-id 2

Behaviour:
  - SELECTs all enable_failed rows for the given root_domain (and optional
    workspace_id) whose last_error matches a 409 pattern. The IMAP-failure
    variant is handled by recover_imap_failures.py instead.
  - Calls POST /accounts/warmup/enable ONCE with the full email list
    (the endpoint accepts a batch).
  - Retries on 409 with 60s backoff up to 30 attempts (~30 min ceiling),
    matching the in-job batch retry budget.
  - On success, batch-UPDATEs rows to status='initial_warmup', sets
    warmup_enabled_at, clears last_error.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import datetime as dt
from pathlib import Path

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent))
from lib.instantly_api import enable_warmup  # type: ignore


def _sb_env() -> tuple[str, str]:
    load_dotenv()
    # Prefer v2 API env (SUPABASE_URL/SERVICE_KEY) - what .env.v2 sets on
    # the control VPS. Fall back to the bison-deliverability skill names
    # so the script also runs from a local checkout.
    url = (
        os.environ.get("SUPABASE_URL")
        or os.environ.get("SUPABASE_COLD_EMAIL_URL", "")
    ).rstrip("/")
    key = (
        os.environ.get("SUPABASE_SERVICE_KEY")
        or os.environ.get("SUPABASE_COLD_EMAIL_SERVICE_KEY")
        or os.environ.get("SUPABASE_COLD_EMAIL_ANON_KEY", "")
    )
    if not url or not key:
        raise SystemExit(
            "SUPABASE_URL / SUPABASE_SERVICE_KEY not set (or SUPABASE_COLD_EMAIL_* fallbacks)"
        )
    return url, key


def _headers(key: str, *, prefer_minimal: bool = False) -> dict:
    h = {"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    if prefer_minimal:
        h["Prefer"] = "return=minimal"
    return h


def fetch_failed(sb_url: str, sb_key: str, root_domain: str, workspace_id: int | None) -> list[dict]:
    params = {
        "select": "id,email,workspace_id,last_error",
        "root_domain": f"eq.{root_domain}",
        "status": "eq.enable_failed",
        "last_error": "like.*409*",
        "limit": "10000",
    }
    if workspace_id is not None:
        params["workspace_id"] = f"eq.{workspace_id}"
    r = requests.get(
        f"{sb_url}/rest/v1/instantly_warmup_state",
        headers=_headers(sb_key),
        params=params,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def mark_enabled(sb_url: str, sb_key: str, ids: list[int]) -> None:
    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.patch(
        f"{sb_url}/rest/v1/instantly_warmup_state",
        headers=_headers(sb_key, prefer_minimal=True),
        params={"id": f"in.({','.join(str(i) for i in ids)})"},
        json={"status": "initial_warmup", "warmup_enabled_at": now, "last_error": None},
        timeout=60,
    )
    r.raise_for_status()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root_domain", help="e.g. 10x-management-center.com")
    ap.add_argument("--workspace-id", type=int, default=None,
                    help="optional - only retry rows for this workspace_id")
    ap.add_argument("--max-attempts", type=int, default=30,
                    help="default 30 attempts at 60s = 30min ceiling")
    args = ap.parse_args()

    sb_url, sb_key = _sb_env()
    rows = fetch_failed(sb_url, sb_key, args.root_domain, args.workspace_id)
    if not rows:
        print(f"No 409-failed rows for root_domain={args.root_domain}; nothing to do.")
        return 0
    print(f"Retrying {len(rows)} senders for {args.root_domain}")
    emails = [r["email"] for r in rows]
    ids = [r["id"] for r in rows]
    for attempt in range(1, args.max_attempts + 1):
        try:
            enable_warmup(emails)
            print(f"  Instantly accepted batch on attempt {attempt}")
            mark_enabled(sb_url, sb_key, ids)
            print(f"  Supabase: marked {len(ids)} rows initial_warmup")
            return 0
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            body = (getattr(e.response, "text", "") or str(e))[:200]
            if status == 409 and attempt < args.max_attempts:
                if attempt == 1 or attempt % 5 == 0:
                    print(f"  attempt {attempt}/{args.max_attempts}: 409 lock; sleeping 60s")
                time.sleep(60)
                continue
            print(f"  attempt {attempt} fatal: {status} {body}")
            return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
