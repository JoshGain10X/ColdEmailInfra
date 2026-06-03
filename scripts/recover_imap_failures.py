#!/usr/bin/env python3
"""Recover senders that hit Instantly's IMAP-verify flake at load time.

Use when a load-to-bison job left rows in status='enable_failed' with
last_error containing "IMAP connection failed". This typically happens
when docker-mailserver hasn't fully registered the user by the time
Instantly does its live IMAP login on POST /accounts. The loader since
commit 7a817fb retries this in-job, so this script is the manual
fallback for the rare case where in-job retries still don't clear.

Per row:
  1. Look up the Bison sender record (for current IMAP/SMTP creds)
  2. Re-call Instantly create_account (mailbox should be alive by now)
  3. After all retries done, batch enable_warmup
  4. Update Supabase to status='initial_warmup'

Usage:
  python3 recover_imap_failures.py <root_domain>
  python3 recover_imap_failures.py <root_domain> --workspace-id 2
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
from lib.instantly_api import create_account, enable_warmup  # type: ignore
from lib.generate import SHARED_MAILBOX_PASSWORD  # type: ignore
from lib.client_context import load_client_context_for_shard  # type: ignore


def _sb_env() -> tuple[str, str]:
    load_dotenv()
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
        "select": "id,email,bison_sender_email_id,workspace_id",
        "root_domain": f"eq.{root_domain}",
        "status": "eq.enable_failed",
        "last_error": "like.*IMAP connection failed*",
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("root_domain", help="e.g. 10xdirector.com")
    ap.add_argument("--workspace-id", type=int, default=None)
    args = ap.parse_args()

    sb_url, sb_key = _sb_env()
    rows = fetch_failed(sb_url, sb_key, args.root_domain, args.workspace_id)
    if not rows:
        print(f"No IMAP-failed rows for {args.root_domain}; nothing to do.")
        return 0
    print(f"Recovering {len(rows)} IMAP-failed senders for {args.root_domain}")

    # Build workspace_id -> (BisonClient, warmup_phrase) cache so we don't
    # re-load ClientContext per row. The shard determines the client; the
    # row.workspace_id tells us which of that client's workspaces.
    ctx = load_client_context_for_shard(args.root_domain)
    ws_cache: dict[int, tuple[object, str]] = {}
    for ws in ctx.workspaces:
        if not ws.workspace_id:
            continue
        bison = ws.client()
        # warmup_filter_phrase comes from Bison's /workspaces endpoint, not
        # the local DB. Mirror api/jobs.py's lookup.
        # IMPORTANT: fail loud if we can't resolve the per-workspace phrase
        # — the previous "sointerested" fallback silently broke Sieve
        # filtering for recovered senders (warmup mail landed in inbox).
        phrase: str | None = None
        try:
            for w in bison.get_workspaces() or []:
                if str(w.get("id")) == str(ws.workspace_id):
                    phrase = (w.get("warmup_filter_phrase") or "").strip() or None
                    break
        except Exception as e:
            raise SystemExit(
                f"FATAL: cannot fetch workspaces from Bison for ws={ws.workspace_id}: {e}. "
                f"Refusing to fall back to universal 'sointerested' tag — that breaks the Sieve filter."
            )
        if not phrase:
            raise SystemExit(
                f"FATAL: Bison workspace {ws.workspace_id} has no warmup_filter_phrase. "
                f"Refusing to push to Instantly with wrong tag — fix the workspace first."
            )
        ws_cache[int(ws.workspace_id)] = (bison, phrase)

    succeeded_ids: list[int] = []
    succeeded_emails: list[str] = []
    still_failed: list[tuple[int, str, str]] = []

    for i, row in enumerate(rows, 1):
        bsid = row["bison_sender_email_id"]
        email = row["email"]
        ws_id = int(row["workspace_id"] or 0)
        if ws_id not in ws_cache:
            still_failed.append((row["id"], email, f"workspace_id {ws_id} not in client context"))
            continue
        bison, phrase = ws_cache[ws_id]
        # Pull current sender record so IMAP/SMTP host comes from the
        # source of truth (operator may have edited it since load).
        b_resp = requests.get(
            f"{bison.base_url.rstrip('/')}/api/sender-emails/{int(bsid)}",
            headers={"Authorization": f"Bearer {bison.token}", "Accept": "application/json"},
            timeout=15,
        )
        if not b_resp.ok:
            still_failed.append((row["id"], email, f"bison fetch {b_resp.status_code}"))
            continue
        b = (b_resp.json() or {}).get("data") or {}
        name = (b.get("name") or "").strip().split(" ", 1)
        first = name[0] if name else ""
        last = name[1] if len(name) > 1 else ""
        try:
            create_account(
                email=email,
                imap_host=b["imap_server"], imap_port=int(b.get("imap_port") or 993),
                smtp_host=b["smtp_server"], smtp_port=int(b.get("smtp_port") or 465),
                username=email, password=SHARED_MAILBOX_PASSWORD,
                first_name=first, last_name=last,
                warmup_custom_ftag=phrase,
            )
            succeeded_ids.append(row["id"])
            succeeded_emails.append(email)
        except Exception as e:
            still_failed.append((row["id"], email, str(e)[:200]))
        if i % 5 == 0 or i == len(rows):
            print(f"  recreated {i}/{len(rows)} ({len(succeeded_ids)} ok, {len(still_failed)} still failing)")
        time.sleep(0.5)

    if not succeeded_emails:
        print(f"\nNo accounts recreated. {len(still_failed)} still failing.")
        for sid, e, err in still_failed[:5]:
            print(f"  {e}: {err}")
        return 1

    print(f"\n{len(succeeded_emails)} accounts recreated on Instantly. Batch-enabling warmup...")
    for attempt in range(1, 31):
        try:
            enable_warmup(succeeded_emails)
            print(f"  Instantly accepted warmup batch on attempt {attempt}")
            break
        except requests.HTTPError as e:
            status = getattr(e.response, "status_code", None)
            if status == 409 and attempt < 30:
                if attempt == 1 or attempt % 5 == 0:
                    print(f"  attempt {attempt}: 409 lock; sleeping 60s")
                time.sleep(60)
                continue
            print(f"  fatal: {status} {e}")
            return 1

    now = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    r = requests.patch(
        f"{sb_url}/rest/v1/instantly_warmup_state",
        headers=_headers(sb_key, prefer_minimal=True),
        params={"id": f"in.({','.join(str(i) for i in succeeded_ids)})"},
        json={"status": "initial_warmup", "warmup_enabled_at": now, "last_error": None},
        timeout=60,
    )
    r.raise_for_status()
    print(f"Supabase: marked {len(succeeded_ids)} rows initial_warmup")
    if still_failed:
        print(f"\n{len(still_failed)} still failing - left as enable_failed:")
        for sid, e, err in still_failed[:5]:
            print(f"  {e}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
