#!/usr/bin/env python3
"""Fleet reconciliation sweep: detect and safely repair cross-system drift.

Teardown is a multi-leg process (Webdock VPS destroy + Bison sender removal +
Instantly warmup-seat removal + Supabase status/flag updates). When the legs do
not all complete, the systems fall out of sync and leave orphans / zombies /
stale flags. This script is BOTH the one-off cleanup tool for the drift the
2026-07 audit found AND the ongoing guard: run it --dry-run on a schedule to
detect regression.

It reconciles six drift classes:

  1. ORPHAN-BILLED VPS     Webdock server running but shard marked destroyed.
  2. STALE bison_loaded    shard flagged bison_loaded=true but torn down.
  3. ZOMBIE Instantly seats Instantly seats live for a destroyed/retired shard.
  4. UNWARMED live senders  live shard with no Instantly rows OR all enable_failed.
  5. STUCK teardown        shard wedged in destroy_incomplete/destruction_failed.
  6. root_domain drift     instantly_warmup_state.root_domain truncated (co.uk).

Repair policy:

  SAFE-FIX (applied under --fix; flag flips, never destructive spend/deletes we
  cannot reverse in spirit):
    * clear stale bison_loaded=true on destroyed / torn-down / no-server shards
    * mark zombie Instantly seats 'removed' (disable warmup + delete account)
    * mark parked 'paused' Instantly seats on destroyed shards 'removed'
    * fix truncated root_domain values in instantly_warmup_state
    * reconcile shard status flags to match reality

  REQUIRES-APPROVAL (reported only, NEVER auto-applied; destructive spend or
  side-effecting):
    * destroy an orphan Webdock VPS  (spend / irreversible)
    * retry Instantly enable on enable_failed roots (side-effecting)
    * push unwarmed live senders into warmup (side-effecting)
    * force-complete a stuck teardown (needs an operator to confirm VPS gone)

Every SAFE-FIX is idempotent: re-running is a no-op once reality matches.

Credentials (all from the environment / vault; nothing hardcoded):
    SUPABASE_URL, SUPABASE_SERVICE_KEY  - shard + warmup state
    INSTANTLY_API_KEY                   - Instantly seat removal
    EB_SUPERADMIN_KEY                   - Bison workspace switch + sender delete
    per-client Webdock tokens           - via ClientContext (Supabase Vault)

Usage:
    python scripts/reconcile_fleet.py --dry-run                 # report everything
    python scripts/reconcile_fleet.py --dry-run --client reachos
    python scripts/reconcile_fleet.py --fix                     # apply SAFE fixes
    python scripts/reconcile_fleet.py --fix --only stale-bison-loaded,zombie-instantly
    python scripts/reconcile_fleet.py --dry-run --only root-domain

Run inside the coldemail-api-v2 container where the env + Vault access live.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import click
import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.client_context import load_client_context_by_id, _supabase  # type: ignore
from lib.teardown import (  # type: ignore
    registrable_root_domain,
    remove_bison_senders,
    remove_instantly_seats,
    webdock_find_server_by_ip,
)

BISON_BASE = os.environ.get("BISON_BASE_URL", "https://send.spamproofed.com")

# Shard statuses that mean "this shard is no longer sending / should be gone".
DEAD_STATUSES = {"destroyed", "torn_down", "retired", "destroy_incomplete", "destruction_failed"}

# The drift-class keys usable with --only.
ALL_CLASSES = [
    "orphan-vps",
    "stale-bison-loaded",
    "zombie-instantly",
    "unwarmed-senders",
    "stuck-teardown",
    "root-domain",
]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _echo_header(title: str) -> None:
    click.echo("")
    click.echo("=" * 68)
    click.echo(title)
    click.echo("=" * 68)


def _all_warmup_rows(sb) -> list[dict]:
    """Fetch every instantly_warmup_state row (paginated past PostgREST's 1000)."""
    out: list[dict] = []
    page_size = 1000
    start = 0
    while True:
        rows = (
            sb.table("instantly_warmup_state")
            .select("id, email, domain, root_domain, status, workspace_id, bison_sender_email_id")
            .range(start, start + page_size - 1)
            .execute()
            .data
        ) or []
        out.extend(rows)
        if len(rows) < page_size:
            break
        start += page_size
    return out


def _shards(sb, client_id: str | None) -> list[dict]:
    q = sb.table("infra_shards").select(
        "domain, status, bison_loaded, vps_ip, client_id, clients(slug)"
    )
    if client_id:
        q = q.eq("client_id", client_id)
    return q.execute().data or []


def _resolve_client_id(sb, client_slug: str | None) -> str | None:
    if not client_slug:
        return None
    row = sb.table("clients").select("id").eq("slug", client_slug).limit(1).execute().data
    if not row:
        raise click.ClickException(f"No client with slug {client_slug!r}")
    return row[0]["id"]


def _switch_bison(superadmin_key: str, workspace_id) -> tuple[str, str] | None:
    """Switch SuperAdmin token into a workspace; return (token, base_url)."""
    try:
        r = requests.post(
            f"{BISON_BASE}/api/workspaces/switch-workspace",
            json={"team_id": int(workspace_id)},
            headers={"Authorization": f"Bearer {superadmin_key}", "Accept": "application/json"},
            timeout=20,
        )
    except Exception:  # noqa: BLE001
        return None
    if not r.ok:
        return None
    data = (r.json() or {}).get("data") or {}
    return data.get("token") or superadmin_key, BISON_BASE


# ---------------------------------------------------------------------------
# Drift-class reconcilers. Each returns a summary dict and appends to
# `approvals` any REQUIRES-APPROVAL items (never auto-applied).
# ---------------------------------------------------------------------------

def reconcile_stale_bison_loaded(sb, shards: list[dict], fix: bool) -> dict:
    """SAFE-FIX: bison_loaded=true on a shard whose status is dead."""
    _echo_header("CLASS 2: STALE bison_loaded=true")
    drift = [s for s in shards if s.get("bison_loaded") and (s.get("status") in DEAD_STATUSES)]
    click.echo(f"  {len(drift)} shard(s) flagged bison_loaded=true but status is dead:")
    for s in drift:
        click.echo(f"    - {s['domain']:<34} status={s.get('status')}")
    fixed = 0
    if fix and drift:
        for s in drift:
            sb.table("infra_shards").update({"bison_loaded": False}).eq(
                "domain", s["domain"]
            ).eq("client_id", s["client_id"]).execute()
            fixed += 1
        click.echo(f"  FIXED: cleared bison_loaded on {fixed} shard(s)")
    return {"class": "stale-bison-loaded", "drift": len(drift), "fixed": fixed}


def reconcile_zombie_instantly(
    sb, shards: list[dict], warmup_rows: list[dict], fix: bool, superadmin_key: str
) -> dict:
    """SAFE-FIX: Instantly seats live for a dead shard; also 'paused' seats on
    dead shards. Disable warmup + delete account + flip row to 'removed', and
    delete the matching Bison senders."""
    _echo_header("CLASS 3: ZOMBIE Instantly seats (+ orphan Bison senders)")
    from lib import instantly_api  # local import: env-dependent module

    dead_roots = {registrable_root_domain(s["domain"]) for s in shards if s.get("status") in DEAD_STATUSES}

    # Reality-based detection: a seat is a zombie if the shard is dead AND the
    # account STILL EXISTS in Instantly - regardless of what our DB status says.
    # This is deliberate: trusting the DB `status` field let an earlier run mark
    # rows 'removed' while the Instantly delete had actually rate-limited and
    # failed, hiding live seats. We now ask Instantly what really exists so the
    # sweep self-heals rows that were mis-marked. Fall back to DB-status detection
    # only if the Instantly list call fails.
    terminal = {"removed", "deleted"}
    rows_by_email = {}
    for r in warmup_rows:
        em = (r.get("email") or "").strip().lower()
        if em:
            rows_by_email[em] = r
    zombies = []
    try:
        for root in sorted(dead_roots):
            for acct in instantly_api.list_accounts(search=root):
                em = (acct.get("email") or "").strip().lower()
                if not em or registrable_root_domain(em.split("@")[-1]) not in dead_roots:
                    continue
                # Attach the DB row if we have one; synthesise a minimal row if not
                # (a live Instantly account with no DB row is still a real zombie).
                zombies.append(rows_by_email.get(em) or {"email": em, "root_domain": root, "id": None})
    except Exception as exc:  # noqa: BLE001 - Instantly list failed, fall back to DB view
        click.echo(f"  (Instantly reality check failed: {str(exc)[:120]}; falling back to DB status)")
        zombies = [
            r for r in warmup_rows
            if (r.get("root_domain") in dead_roots or registrable_root_domain(r.get("domain") or "") in dead_roots)
            and (r.get("status") not in terminal)
        ]
    # Group by root for reporting + by workspace for Bison removal.
    by_root: dict[str, list[dict]] = {}
    for r in zombies:
        by_root.setdefault(r.get("root_domain") or registrable_root_domain(r.get("domain") or ""), []).append(r)
    click.echo(f"  {len(zombies)} zombie Instantly seat(s) across {len(by_root)} dead root(s):")
    for root, rows in sorted(by_root.items()):
        statuses = {}
        for r in rows:
            statuses[r.get("status")] = statuses.get(r.get("status"), 0) + 1
        click.echo(f"    - {root:<28} {len(rows)} seats {dict(statuses)}")

    fixed_seats = 0
    fixed_senders = 0
    if fix and zombies:
        # Instantly: delete with rate-limit backoff; capture WHICH emails were
        # actually removed so we only mark those rows terminal.
        emails = [r["email"] for r in zombies if r.get("email")]
        inst = remove_instantly_seats(emails)
        removed = inst["removed_emails"]  # set of confirmed-gone emails
        fixed_seats = len(removed)
        click.echo(
            f"  Instantly: confirmed-removed {fixed_seats}/{len(emails)}"
            + (f", {len(inst['errors'])} still failing (left for re-run)" if inst["errors"] else "")
        )
        # Bison senders: only for rows whose Instantly seat is confirmed gone.
        by_ws: dict[str, list[int]] = {}
        for r in zombies:
            if (r.get("email") or "").strip().lower() not in removed:
                continue
            ws = r.get("workspace_id")
            sid = r.get("bison_sender_email_id")
            if ws is not None and sid is not None:
                by_ws.setdefault(str(ws), []).append(int(sid))
        for ws_id, sids in by_ws.items():
            sw = _switch_bison(superadmin_key, ws_id)
            if not sw:
                click.echo(f"  Bison ws {ws_id}: switch failed, leaving {len(sids)} senders")
                continue
            token, base = sw
            bres = remove_bison_senders(token, base, sids)
            fixed_senders += bres["deleted"] + bres["already_gone"]
            click.echo(
                f"  Bison ws {ws_id}: deleted {bres['deleted']}, already-gone {bres['already_gone']}"
                + (f", {len(bres['errors'])} errors" if bres["errors"] else "")
            )
        # Flip terminal ONLY for confirmed-removed emails with a DB row id.
        ids = [
            r["id"] for r in zombies
            if r.get("id") is not None and (r.get("email") or "").strip().lower() in removed
        ]
        for i in range(0, len(ids), 500):
            sb.table("instantly_warmup_state").update(
                {"status": "removed", "last_error": None}
            ).in_("id", ids[i : i + 500]).execute()
        click.echo(f"  FIXED: {fixed_seats} Instantly seats, {fixed_senders} Bison senders, {len(ids)} rows marked removed")
        if inst["errors"]:
            click.echo(f"  {len(inst['errors'])} seat(s) still live (rate-limited/error) - re-run to finish.")
    return {"class": "zombie-instantly", "drift": len(zombies), "fixed": fixed_seats}


def reconcile_root_domain(sb, warmup_rows: list[dict], fix: bool) -> dict:
    """SAFE-FIX: recompute root_domain from domain and correct truncated values."""
    _echo_header("CLASS 6: root_domain truncation (co.uk etc.)")
    drift = []
    for r in warmup_rows:
        dom = (r.get("domain") or "").lower()
        if not dom:
            continue
        correct = registrable_root_domain(dom)
        if (r.get("root_domain") or "").lower() != correct:
            drift.append((r, correct))
    click.echo(f"  {len(drift)} row(s) with a wrong root_domain:")
    for r, correct in drift[:20]:
        click.echo(f"    - {r.get('email'):<40} {r.get('root_domain')!r} -> {correct!r}")
    if len(drift) > 20:
        click.echo(f"    ... and {len(drift) - 20} more")
    fixed = 0
    if fix and drift:
        for r, correct in drift:
            sb.table("instantly_warmup_state").update({"root_domain": correct}).eq(
                "id", r["id"]
            ).execute()
            fixed += 1
        click.echo(f"  FIXED: corrected root_domain on {fixed} row(s)")
    return {"class": "root-domain", "drift": len(drift), "fixed": fixed}


def reconcile_orphan_vps(sb, shards: list[dict], client_id: str | None, approvals: list[str]) -> dict:
    """REPORT-ONLY: shard marked dead but Webdock still has a running server.

    Destroying a VPS is spend + irreversible, so this is never auto-applied. We
    verify against Webdock per client and list the exact destroy command.
    """
    _echo_header("CLASS 1: ORPHAN-BILLED VPS (report only)")
    dead_with_ip = [s for s in shards if s.get("status") in DEAD_STATUSES and s.get("vps_ip")]
    if not dead_with_ip:
        click.echo("  No dead shards with a recorded vps_ip to check.")
        return {"class": "orphan-vps", "drift": 0, "fixed": 0}

    # Load client contexts lazily (Webdock token per client).
    ctx_cache: dict[str, object] = {}
    orphans = []
    for s in dead_with_ip:
        cid = s["client_id"]
        if cid not in ctx_cache:
            try:
                ctx_cache[cid] = load_client_context_by_id(cid)
            except Exception as exc:  # noqa: BLE001
                click.echo(f"  {s['domain']}: cannot load client context ({str(exc)[:80]}) - skip")
                ctx_cache[cid] = None
        ctx = ctx_cache[cid]
        wd = getattr(ctx, "webdock", None) if ctx else None
        if wd is None:
            continue
        srv = webdock_find_server_by_ip(wd, s.get("vps_ip"))
        if srv:
            slug = srv.get("slug") or srv.get("id")
            orphans.append((s, slug))
            click.echo(f"    ORPHAN {s['domain']:<30} ip={s['vps_ip']} slug={slug} status={s.get('status')}")
    if not orphans:
        click.echo("  No orphan-billed VPSes found (all dead shards' servers confirmed gone).")
    else:
        for s, slug in orphans:
            approvals.append(
                f"[orphan-vps] Webdock server {slug} ({s['vps_ip']}, {s['domain']}) still running "
                f"for a dead shard. Destroy with: python scripts/destroy_shard.py --domain {s['domain']} "
                f"(or delete slug {slug} in the Webdock dashboard). SPEND/IRREVERSIBLE."
            )
    return {"class": "orphan-vps", "drift": len(orphans), "fixed": 0}


def reconcile_unwarmed_senders(sb, shards: list[dict], warmup_rows: list[dict], approvals: list[str]) -> dict:
    """REPORT-ONLY: live shard with no Instantly coverage, or all enable_failed."""
    _echo_header("CLASS 4: UNWARMED live senders (report only)")
    live = [s for s in shards if s.get("bison_loaded") and s.get("status") not in DEAD_STATUSES]
    rows_by_root: dict[str, list[dict]] = {}
    for r in warmup_rows:
        root = r.get("root_domain") or registrable_root_domain(r.get("domain") or "")
        rows_by_root.setdefault(root, []).append(r)

    never_pushed = []
    all_failed = []
    for s in live:
        root = registrable_root_domain(s["domain"])
        rows = rows_by_root.get(root, [])
        if not rows:
            never_pushed.append(s)
        else:
            active = [r for r in rows if r.get("status") in ("initial_warmup", "maintenance_warmup", "pending_enable")]
            if not active and all(r.get("status") == "enable_failed" for r in rows):
                all_failed.append((s, len(rows)))
    click.echo(f"  {len(never_pushed)} live shard(s) with ZERO Instantly rows (never pushed):")
    for s in never_pushed:
        click.echo(f"    - {s['domain']}")
        approvals.append(
            f"[unwarmed-senders] {s['domain']} is live (bison_loaded) but has no Instantly warmup "
            f"rows. Push to warmup with: python scripts/backfill_instantly_for_shards.py "
            f"--shard {s['domain']}  (SIDE-EFFECTING: creates Instantly accounts)."
        )
    click.echo(f"  {len(all_failed)} live shard(s) with ALL rows in enable_failed:")
    for s, n in all_failed:
        click.echo(f"    - {s['domain']} ({n} enable_failed rows)")
        approvals.append(
            f"[unwarmed-senders] {s['domain']} has {n} Instantly rows all in enable_failed "
            f"(e.g. IMAP connection failed). Retry with: python scripts/retry_warmup_enable.py "
            f"--shard {s['domain']}  (SIDE-EFFECTING)."
        )
    return {"class": "unwarmed-senders", "drift": len(never_pushed) + len(all_failed), "fixed": 0}


def reconcile_stuck_teardown(sb, shards: list[dict], approvals: list[str]) -> dict:
    """REPORT-ONLY: shard wedged in destroy_incomplete / destruction_failed."""
    _echo_header("CLASS 5: STUCK teardown (report only)")
    stuck = [s for s in shards if s.get("status") in ("destroy_incomplete", "destruction_failed")]
    click.echo(f"  {len(stuck)} shard(s) wedged mid-teardown:")
    for s in stuck:
        click.echo(f"    - {s['domain']:<34} status={s.get('status')} vps_ip={s.get('vps_ip')}")
        approvals.append(
            f"[stuck-teardown] {s['domain']} is {s.get('status')}. Confirm the Webdock VPS is gone "
            f"(reconcile orphan-vps class), then re-run: python scripts/destroy_shard.py "
            f"--domain {s['domain']} --yes  (run_destroy now force-completes DNS-only wedges when "
            f"the VPS is confirmed gone)."
        )
    return {"class": "stuck-teardown", "drift": len(stuck), "fixed": 0}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

@click.command()
@click.option("--dry-run", is_flag=True, default=False, help="Report only; touch nothing (default-safe).")
@click.option("--fix", is_flag=True, default=False, help="Apply SAFE repairs (flag flips + seat removal).")
@click.option("--client", "client_slug", default=None, help="Scope to one client slug.")
@click.option("--only", "only", default=None,
              help=f"Comma-separated subset of classes to run. Choices: {', '.join(ALL_CLASSES)}")
def main(dry_run: bool, fix: bool, client_slug: str | None, only: str | None) -> None:
    load_dotenv()
    if fix and dry_run:
        raise click.ClickException("Pass either --dry-run or --fix, not both.")
    # Default-safe: no flag => report only.
    apply_fixes = fix and not dry_run
    mode = "FIX (applying SAFE repairs)" if apply_fixes else "DRY-RUN (report only)"

    classes = ALL_CLASSES
    if only:
        classes = [c.strip() for c in only.split(",") if c.strip()]
        bad = [c for c in classes if c not in ALL_CLASSES]
        if bad:
            raise click.ClickException(f"Unknown class(es): {bad}. Choices: {ALL_CLASSES}")

    sb = _supabase()
    superadmin_key = os.environ.get("EB_SUPERADMIN_KEY", "")
    client_id = _resolve_client_id(sb, client_slug)

    click.echo(f"Fleet reconciliation - mode: {mode}")
    click.echo(f"  scope: {'client=' + client_slug if client_slug else 'ALL clients'}")
    click.echo(f"  classes: {', '.join(classes)}")

    shards = _shards(sb, client_id)
    warmup_rows = _all_warmup_rows(sb)
    if client_id:
        # Scope warmup rows to this client's shard roots.
        client_roots = {registrable_root_domain(s["domain"]) for s in shards}
        warmup_rows = [
            r for r in warmup_rows
            if (r.get("root_domain") in client_roots
                or registrable_root_domain(r.get("domain") or "") in client_roots)
        ]
    click.echo(f"  loaded {len(shards)} shard(s), {len(warmup_rows)} warmup row(s)")

    approvals: list[str] = []
    summaries: list[dict] = []

    if "stale-bison-loaded" in classes:
        summaries.append(reconcile_stale_bison_loaded(sb, shards, apply_fixes))
    if "zombie-instantly" in classes:
        if apply_fixes and not superadmin_key:
            click.echo("  WARNING: EB_SUPERADMIN_KEY not set - Bison sender removal will be skipped.")
        summaries.append(reconcile_zombie_instantly(sb, shards, warmup_rows, apply_fixes, superadmin_key))
    if "root-domain" in classes:
        summaries.append(reconcile_root_domain(sb, warmup_rows, apply_fixes))
    if "orphan-vps" in classes:
        summaries.append(reconcile_orphan_vps(sb, shards, client_id, approvals))
    if "unwarmed-senders" in classes:
        summaries.append(reconcile_unwarmed_senders(sb, shards, warmup_rows, approvals))
    if "stuck-teardown" in classes:
        summaries.append(reconcile_stuck_teardown(sb, shards, approvals))

    _echo_header("SUMMARY (before -> after)")
    for s in summaries:
        remaining = s["drift"] - s["fixed"]
        click.echo(f"  {s['class']:<22} drift={s['drift']:<4} fixed={s['fixed']:<4} remaining={remaining}")

    _echo_header("REQUIRES APPROVAL (never auto-applied)")
    if not approvals:
        click.echo("  (none)")
    else:
        for a in approvals:
            click.echo(f"  * {a}")

    if not apply_fixes:
        click.echo("\nDRY-RUN: nothing was changed. Re-run with --fix to apply SAFE repairs.")


if __name__ == "__main__":
    main()
