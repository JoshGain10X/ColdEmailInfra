#!/usr/bin/env python3
"""Backfill instantly_warmup_state for shards deployed before the post-deploy
Instantly push was added to load-to-bison (~2026-06-03).

For each target shard:
  1. Resolve workspace_id from bison_mailbox_dailies (we already snapshot this)
  2. Switch Bison context to that workspace; fetch its warmup_filter_phrase
  3. List senders for the shard (Bison API search=<root>)
  4. For each sender NOT already in instantly_warmup_state:
       - Build the create_account payload
       - In --dry-run: collect + count, don't write
       - In real run: call instantly_api.create_account
       - Insert row into instantly_warmup_state with status='pending_enable'
  5. After all created on the shard, batch enable_warmup with retries
  6. Update rows to status='initial_warmup' on success, 'enable_failed' otherwise

Usage:
    python backfill_instantly_for_shards.py --shard <domain> --dry-run
    python backfill_instantly_for_shards.py --shard <domain>
    python backfill_instantly_for_shards.py --all --dry-run
    python backfill_instantly_for_shards.py --all

The 14 known coverage-gap shards are listed in DEFAULT_SHARDS below.
"""
from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from datetime import timedelta
from supabase import Client, create_client
from lib import instantly_api
from lib.bison_api import BisonClient
from lib.generate import SHARED_MAILBOX_PASSWORD

# Snapshot of the 14 shards identified 2026-06-17 with zero rows in
# instantly_warmup_state but actively sending production volume.
DEFAULT_SHARDS = [
    "10x-business-leaders.com", "10xbusinessleaders.com", "10xmanagers.co.uk",
    "10xmanagersdevelopment.com", "become-a-10xmanager.com", "connect10xmanagers.com",
    "develop-10xmanagers.com", "developedwith10xmanagers.com", "email10xmanagers.com",
    "get10xmanagers.org", "my10xmanagers.com", "one10xmanagers.com",
    "try10xmanagers.com", "tryreachos.com",
]


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def _sb() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


BISON_BASE = os.environ.get("BISON_BASE_URL", "https://send.spamproofed.com")


def resolve_workspace(sb: Client, shard: str) -> tuple[int, str] | None:
    """Return (workspace_id, workspace_name) for the shard.

    Looks up the most recent measured_date in bison_mailbox_dailies where
    domain ends with .shard or equals shard.
    """
    rows = sb.from_("bison_mailbox_dailies").select(
        "workspace_id, workspace_name"
    ).filter("domain", "ilike", f"%.{shard}").limit(1).execute().data
    if not rows:
        rows = sb.from_("bison_mailbox_dailies").select(
            "workspace_id, workspace_name"
        ).filter("domain", "eq", shard).limit(1).execute().data
    if not rows:
        return None
    return rows[0]["workspace_id"], rows[0].get("workspace_name") or f"workspace-{rows[0]['workspace_id']}"


def switch_bison_workspace(superadmin_key: str, workspace_id: int) -> tuple[str, str] | None:
    """Switch the SuperAdmin token's active workspace + return
    (workspace_token, warmup_filter_phrase). Per memory:
    POST /api/workspaces/switch-workspace {"team_id": N} -> data.token + .warmup_filter_phrase
    """
    import requests
    r = requests.post(
        f"{BISON_BASE}/api/workspaces/switch-workspace",
        json={"team_id": workspace_id},
        headers={"Authorization": f"Bearer {superadmin_key}", "Accept": "application/json"},
        timeout=20,
    )
    if not r.ok:
        _log(f"  switch-workspace failed: {r.status_code} {r.text[:200]}")
        return None
    data = (r.json() or {}).get("data") or {}
    token = data.get("token") or superadmin_key  # some Bison builds reuse superadmin token
    phrase = data.get("warmup_filter_phrase") or "sointerested"
    return token, phrase


def list_already_in_instantly(sb: Client, shard: str) -> set[str]:
    rows = sb.from_("instantly_warmup_state").select("email").filter(
        "email", "ilike", f"%.{shard}"
    ).execute().data or []
    return {r["email"].lower() for r in rows}


def process_shard(sb: Client, shard: str, superadmin_key: str, dry_run: bool = False) -> dict:
    _log(f"=== shard: {shard} (dry_run={dry_run}) ===")
    ctx = resolve_workspace(sb, shard)
    if not ctx:
        _log(f"  could not resolve workspace for {shard} - SKIP")
        return {"shard": shard, "skipped": True, "reason": "workspace-unresolved"}
    ws_id, ws_name = ctx
    _log(f"  workspace: id={ws_id} ({ws_name})")

    swr = switch_bison_workspace(superadmin_key, ws_id)
    if not swr:
        return {"shard": shard, "skipped": True, "reason": "switch-workspace-failed"}
    api_key, phrase = swr
    _log(f"  warmup_filter_phrase: {phrase}")

    skip_emails = list_already_in_instantly(sb, shard)
    _log(f"  already in instantly_warmup_state for this shard: {len(skip_emails)}")

    bison = BisonClient(token=api_key, base_url=BISON_BASE)
    senders = bison.list_sender_emails(search=shard)
    _log(f"  Bison senders matching '{shard}': {len(senders)}")

    # Filter to senders whose email actually belongs to this root
    senders = [s for s in senders if s.get("email", "").lower().endswith(f".{shard}")
               or s.get("email", "").lower().endswith(f"@{shard}")]
    _log(f"  after exact-match filter: {len(senders)}")

    to_create: list[dict] = []
    for s in senders:
        email = (s.get("email") or "").lower().strip()
        if not email or email in skip_emails:
            continue
        to_create.append({
            "sender_id": s.get("id"),
            "email": email,
            "first_name": s.get("first_name") or "",
            "last_name": s.get("last_name") or "",
            "imap_host": s.get("imap_server") or f"mail.{email.split('@', 1)[1]}",
            "imap_port": int(s.get("imap_port") or 993),
            "smtp_host": s.get("smtp_server") or f"mail.{email.split('@', 1)[1]}",
            "smtp_port": int(s.get("smtp_port") or 465),
            # Bison doesn't return passwords via /api/sender-emails (security).
            # All Custom-SMTP shard mailboxes share the same password baked into
            # lib/generate.py:SHARED_MAILBOX_PASSWORD - same value Instantly was
            # given at original deploy time by api/jobs.py, so IMAP-verify works.
            "password": SHARED_MAILBOX_PASSWORD,
        })

    _log(f"  candidates to backfill: {len(to_create)}")
    if to_create[:3]:
        for r in to_create[:3]:
            _log(f"    sample: {r['email']} (imap {r['imap_host']}:{r['imap_port']})")

    if dry_run or not to_create:
        return {"shard": shard, "workspace_id": ws_id, "candidates": len(to_create),
                "skipped": False, "dry_run": dry_run}

    # ------------ Real run starts here ------------
    created: list[tuple[str, int]] = []  # (email, bison_sender_id)
    failed = 0
    for i, r in enumerate(to_create, 1):
        domain = r["email"].split("@", 1)[1]
        parts = domain.split(".")
        root = ".".join(parts[-2:]) if len(parts) >= 2 else domain
        # Mirror api/jobs.py _row shape exactly. The NOT NULL instantly_account_id
        # constraint takes the email as a placeholder (Instantly's real account
        # UUID isn't queryable by us anyway - they index by email everywhere).
        row_payload = {
            "workspace_id": ws_id,
            "workspace_name": ws_name,
            "bison_sender_email_id": r["sender_id"],
            "email": r["email"],
            "domain": domain,
            "root_domain": root,
            "instantly_account_id": r["email"],
            "initial_warmup_ends_at": (datetime.now(timezone.utc) + timedelta(days=14)).isoformat(),
        }
        # Retry IMAP-failed creates up to 3 attempts with 15s backoff (same
        # treatment api/jobs.py applies; Instantly's verifier is racy especially
        # when many accounts are being created in quick succession).
        ic_exc: Exception | None = None
        for attempt in range(3):
            try:
                instantly_api.create_account(
                    email=r["email"],
                    imap_host=r["imap_host"], imap_port=r["imap_port"],
                    smtp_host=r["smtp_host"], smtp_port=r["smtp_port"],
                    username=r["email"], password=r["password"],
                    first_name=r["first_name"], last_name=r["last_name"],
                    warmup_custom_ftag=phrase,
                )
                ic_exc = None
                break
            except Exception as e:
                ic_exc = e
                if "IMAP connection failed" in str(e) and attempt < 2:
                    time.sleep(15)
                    continue
                break

        if ic_exc is None:
            row_payload["status"] = "pending_enable"
            try:
                sb.from_("instantly_warmup_state").insert(row_payload).execute()
                created.append((r["email"], r["sender_id"]))
            except Exception as db_exc:
                _log(f"    [{i}/{len(to_create)}] DB insert failed for {r['email']}: {str(db_exc)[:200]}")
        else:
            failed += 1
            err_msg = str(ic_exc)[:500]
            _log(f"    [{i}/{len(to_create)}] FAIL {r['email']}: {err_msg[:200]}")
            row_payload["status"] = "enable_failed"
            row_payload["last_error"] = err_msg
            try:
                sb.from_("instantly_warmup_state").insert(row_payload).execute()
            except Exception as db_exc:
                _log(f"      DB insert also failed: {str(db_exc)[:200]}")
        if i % 10 == 0:
            _log(f"    {i}/{len(to_create)} created, {failed} failed")
        time.sleep(0.3)

    _log(f"  Instantly account creation: {len(created)} OK, {failed} failed")

    if not created:
        return {"shard": shard, "created": 0, "failed": failed}

    # Batch enable warmup with the same retry budget the load-to-bison flow uses.
    _log(f"  batching enable_warmup for {len(created)} accounts")
    enabled = False
    last_err: str | None = None
    for attempt in range(1, 31):
        try:
            instantly_api.enable_warmup([e for e, _ in created])
            enabled = True
            break
        except Exception as exc:
            last_err = str(exc)[:300]
            if "409" in last_err and attempt < 30:
                if attempt == 1 or attempt % 5 == 0:
                    _log(f"    enable_warmup 409 (attempt {attempt}/30) - sleep 60s")
                time.sleep(60)
                continue
            break
    if enabled:
        sb.from_("instantly_warmup_state").update({
            "status": "initial_warmup",
            "warmup_enabled_at": datetime.now(timezone.utc).isoformat(),
            "last_error": None,
        }).in_("bison_sender_email_id", [sid for _, sid in created]).execute()
        _log(f"  enable_warmup OK on {len(created)}")
    else:
        sb.from_("instantly_warmup_state").update({
            "status": "enable_failed",
            "last_error": last_err,
        }).in_("bison_sender_email_id", [sid for _, sid in created]).execute()
        _log(f"  enable_warmup failed: {last_err}")

    return {
        "shard": shard, "workspace_id": ws_id,
        "candidates": len(to_create), "created": len(created),
        "failed": failed, "warmup_enabled": enabled,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--shard", help="Single shard domain to backfill")
    g.add_argument("--all", action="store_true", help=f"Backfill all {len(DEFAULT_SHARDS)} known-missing shards")
    p.add_argument("--dry-run", action="store_true", help="Print plan + counts; no writes to Instantly or Supabase")
    p.add_argument("--shard-delay", type=int, default=300,
                   help="Seconds between shards in --all mode (default 300, helps Instantly lock contention)")
    args = p.parse_args()

    sb = _sb()
    superadmin_key = os.environ.get("EB_SUPERADMIN_KEY", "")
    if not superadmin_key:
        _log("ERROR: EB_SUPERADMIN_KEY env var not set. Cannot switch workspaces.")
        return 2
    targets = [args.shard] if args.shard else DEFAULT_SHARDS
    _log(f"backfill start: {len(targets)} shard(s), dry_run={args.dry_run}")

    summary = []
    for i, shard in enumerate(targets, 1):
        try:
            res = process_shard(sb, shard, superadmin_key, dry_run=args.dry_run)
            summary.append(res)
        except Exception:
            _log(f"  {shard} FAILED with exception:")
            traceback.print_exc()
        if i < len(targets) and not args.dry_run:
            _log(f"  sleeping {args.shard_delay}s before next shard")
            time.sleep(args.shard_delay)

    _log("=== SUMMARY ===")
    for s in summary:
        _log(f"  {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
