#!/usr/bin/env python3
"""Fleet-wide retrofit: Postfix per-destination rate cap.

For every live shard, this script SSHes in and applies the per-destination
rate cap (a deliverability safety net) both persistently and live:

  1. Writes/refreshes <workdir>/docker-data/dms/config/postfix-main.cf so the
     settings survive container restarts (docker-mailserver merges this file
     into main.cf at every start).
  2. Applies the settings live via `postconf -e` + `postfix reload` inside the
     container - graceful, never a restart.
  3. Verifies via `postconf default_destination_rate_delay` that the live value
     is what we expect.
  4. Marks state.steps["postfix_rate_cap_applied"] so reruns are no-ops.

The cap is default_destination_rate_delay=1s + smtp_destination_concurrency
_limit=5 (see lib.mailserver.POSTFIX_RATE_CAP_SETTINGS). At our ~150/day/shard
volumes it is non-binding; it only bites if a Bison misconfiguration tries to
burst-hammer a single receiving domain.

"Live" means bison_loaded=true and status <> 'destroyed'. bison_loaded is the
authoritative is-it-sending signal; status can be stale. Passing --domain skips
the bison_loaded filter (explicit targeting), but never touches destroyed
shards.

Run on the control VPS where the shard state files and SSH key live. The
Supabase env vars must be set (SUPABASE_URL, SUPABASE_SERVICE_KEY).

Usage:
    python scripts/retrofit_postfix_rate_cap.py --dry-run
    python scripts/retrofit_postfix_rate_cap.py --domain get10xleaders.com
    python scripts/retrofit_postfix_rate_cap.py --client 10x-managers
    python scripts/retrofit_postfix_rate_cap.py          # whole live fleet
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.client_context import load_client_context_by_slug, _supabase  # type: ignore
from lib.mailserver import MailserverClient, POSTFIX_RATE_CAP_SETTINGS
from lib.state import ShardState


RETROFIT_STEP = "postfix_rate_cap_applied"
PAUSE_BETWEEN_DOMAINS_S = 2.0


def _ssh_user(state: ShardState) -> str:
    vps = state.get("vps") or {}
    return vps.get("ssh_user") or os.environ.get("SSH_USER", "admin")


def _retrofit_one(shard_row: dict, dry_run: bool) -> dict:
    domain = shard_row["domain"]
    client_slug = shard_row["clients"]["slug"]

    state = ShardState(domain)
    if not state.path.exists():
        return {"domain": domain, "skipped": "no_state_file"}
    if state.is_step_done(RETROFIT_STEP):
        return {"domain": domain, "skipped": "already_retrofitted"}

    vps_state = state.get("vps") or {}
    vps_ip = vps_state.get("ip") or shard_row.get("vps_ip")
    if not vps_ip:
        return {"domain": domain, "error": "no_vps_ip"}

    click.echo(f"\n=== {domain} ===")
    click.echo(f"  client : {client_slug}")
    click.echo(f"  vps_ip : {vps_ip}")

    if dry_run:
        click.echo("  DRY-RUN: would write postfix-main.cf + postconf -e + postfix reload")
        return {"domain": domain, "dry_run": True}

    # SSH in, apply the cap, verify.
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ms = MailserverClient(vps_ip, ssh_key, user=_ssh_user(state))
    ms.connect()
    try:
        ms.apply_postfix_rate_cap()
        live = ms.verify_postfix_rate_cap()
    finally:
        ms.close()

    # Confirm the live values match what we intended.
    mismatches = {
        key: live.get(key)
        for key, expected in POSTFIX_RATE_CAP_SETTINGS.items()
        if live.get(key) != expected
    }
    if mismatches:
        return {
            "domain": domain,
            "error": f"verify_failed: live={live} expected={dict(POSTFIX_RATE_CAP_SETTINGS)}",
        }
    click.echo(f"  ok  verified live: {live}")

    state.mark_step_done(RETROFIT_STEP)
    click.echo("  ok  flag set (state.postfix_rate_cap_applied)")
    return {"domain": domain, "ok": True}


@click.command()
@click.option("--domain", default=None, help="Scope to one shard root (skips the bison_loaded filter).")
@click.option("--client", "client_slug", default=None, help="Scope to one client slug (e.g. 10x-managers).")
@click.option("--dry-run", is_flag=True, help="Print intent only; touch nothing.")
def main(domain: str | None, client_slug: str | None, dry_run: bool) -> None:
    load_dotenv()
    sb = _supabase()

    q = (
        sb.table("infra_shards")
        .select("domain,vps_ip,status,client_id,clients!inner(slug)")
        .neq("status", "destroyed")
    )
    if domain:
        q = q.eq("domain", domain)
    else:
        # bison_loaded is the authoritative live signal (status can be stale)
        q = q.eq("bison_loaded", True)
    if client_slug:
        q = q.eq("clients.slug", client_slug)

    shards = q.execute().data or []
    if not shards:
        click.echo("No shards matched.")
        return

    click.echo(f"Retrofitting {len(shards)} shard(s) (dry_run={dry_run})")
    results: list[dict] = []
    for i, row in enumerate(shards):
        try:
            results.append(_retrofit_one(row, dry_run=dry_run))
        except Exception as exc:
            click.echo(f"  x ERROR: {exc}")
            results.append({"domain": row["domain"], "error": str(exc)[:200]})
        if i < len(shards) - 1:
            time.sleep(PAUSE_BETWEEN_DOMAINS_S)

    click.echo("\n=== SUMMARY ===")
    ok = [r for r in results if r.get("ok")]
    skipped = [r for r in results if r.get("skipped")]
    dry = [r for r in results if r.get("dry_run")]
    errs = [r for r in results if r.get("error")]
    click.echo(f"  ok       : {len(ok)}")
    click.echo(f"  dry-run  : {len(dry)}")
    click.echo(f"  skipped  : {len(skipped)}  (already retrofitted or no state)")
    click.echo(f"  errors   : {len(errs)}")
    for r in ok:
        click.echo(f"    OK   {r['domain']}")
    for r in errs:
        click.echo(f"    FAIL {r['domain']}: {r['error']}")


if __name__ == "__main__":
    main()
