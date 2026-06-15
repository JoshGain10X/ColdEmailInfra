#!/usr/bin/env python3
"""Fleet-wide retrofit: per-shard DMARC inbox + rewritten rua.

For every active/verified shard, this script:
  1. Pins state["dmarc_inbox"] to f"dmarc@{sorted(subdomains)[0]}.{domain}"
     if not already set.
  2. Creates that mailbox on the mail VPS via docker-mailserver (idempotent).
  3. Rewrites the apex `_dmarc.<root>` and per-subdomain `_dmarc.<sub>.<root>`
     TXT records on Cloudflare to use the shard-local rua.
  4. Marks state.steps["dmarc_inbox_retrofitted"] so reruns are no-ops.

Run on infraapi1 where the shard state files and SSH key live. The Supabase
env vars must be set (SUPABASE_URL, SUPABASE_SERVICE_KEY).

Usage:
    python scripts/retrofit_dmarc.py --dry-run
    python scripts/retrofit_dmarc.py --domain get10xleaders.com
    python scripts/retrofit_dmarc.py --client 10x-managers
    python scripts/retrofit_dmarc.py          # whole fleet
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
from lib.generate import SHARED_MAILBOX_PASSWORD
from lib.mailserver import MailserverClient
from lib.state import ShardState


RETROFIT_STEP = "dmarc_inbox_retrofitted"


def _compute_inbox(domain: str, subs: list[str]) -> str:
    if not subs:
        raise RuntimeError(f"shard {domain} has no subdomains in state — refusing to compute inbox")
    return f"dmarc@{sorted(subs)[0]}.{domain}"


def _dmarc_record(apex: bool, rua: str) -> str:
    # Match the exact shape used in api/jobs.py:328 (apex) and :358 (sub).
    sp = "; sp=quarantine" if apex else ""
    return f"v=DMARC1; p=quarantine{sp}; rua=mailto:{rua}; adkim=r; aspf=r"


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

    subs = state.get("subdomains") or []
    if not subs:
        return {"domain": domain, "error": "no_subdomains"}

    inbox = state.get("dmarc_inbox") or _compute_inbox(domain, subs)
    vps_state = state.get("vps") or {}
    vps_ip = vps_state.get("ip") or shard_row.get("vps_ip")
    if not vps_ip:
        return {"domain": domain, "error": "no_vps_ip"}

    click.echo(f"\n=== {domain} ===")
    click.echo(f"  client      : {client_slug}")
    click.echo(f"  vps_ip      : {vps_ip}")
    click.echo(f"  dmarc_inbox : {inbox}")
    click.echo(f"  subdomains  : {subs}")

    if dry_run:
        click.echo("  DRY-RUN: would create mailbox + rewrite DMARC TXT on apex + each sub")
        return {"domain": domain, "dry_run": True, "inbox": inbox}

    # 1. Persist state field early so a partial failure still records intent
    state.set("dmarc_inbox", inbox)

    # 2. Resolve client context for SSH key + Cloudflare creds
    ctx = load_client_context_by_slug(client_slug)
    cf = ctx.cloudflare
    zone_id = state.get("cloudflare_zone_id") or cf.get_zone_id(domain)

    # 3. Create the inbox via mailserver SSH
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ssh_user = _ssh_user(state)
    ms = MailserverClient(vps_ip, ssh_key, user=ssh_user)
    ms.connect()
    try:
        ms.wait_for_mailserver_ready()
        ms.add_mailbox(inbox, SHARED_MAILBOX_PASSWORD)
    finally:
        ms.close()
    click.echo("  ✓ mailbox created")

    # 4. Rewrite DMARC TXT records — apex + each subdomain
    cf.upsert_record(zone_id, "TXT", f"_dmarc.{domain}", _dmarc_record(apex=True, rua=inbox))
    for sub in subs:
        cf.upsert_record(zone_id, "TXT", f"_dmarc.{sub}.{domain}", _dmarc_record(apex=False, rua=inbox))
    click.echo(f"  ✓ DMARC rewritten on {1 + len(subs)} records")

    state.mark_step_done(RETROFIT_STEP)
    return {"domain": domain, "ok": True, "inbox": inbox, "records": 1 + len(subs)}


@click.command()
@click.option("--domain", default=None, help="Scope to one shard root.")
@click.option("--client", "client_slug", default=None, help="Scope to one client slug (e.g. 10x-managers).")
@click.option("--dry-run", is_flag=True, help="Print intent only; touch nothing.")
def main(domain: str | None, client_slug: str | None, dry_run: bool) -> None:
    load_dotenv()
    sb = _supabase()

    q = (
        sb.table("infra_shards")
        .select("domain,vps_ip,status,client_id,clients!inner(slug)")
        .in_("status", ["active", "verified"])
    )
    if domain:
        q = q.eq("domain", domain)
    if client_slug:
        q = q.eq("clients.slug", client_slug)

    shards = q.execute().data or []
    if not shards:
        click.echo("No shards matched.")
        return

    click.echo(f"Retrofitting {len(shards)} shard(s) (dry_run={dry_run})")
    results: list[dict] = []
    for row in shards:
        try:
            results.append(_retrofit_one(row, dry_run=dry_run))
        except Exception as exc:
            click.echo(f"  ✗ ERROR: {exc}")
            results.append({"domain": row["domain"], "error": str(exc)[:200]})
        time.sleep(1)  # gentle throttle on Cloudflare + SSH

    click.echo("\n=== SUMMARY ===")
    ok = [r for r in results if r.get("ok")]
    skipped = [r for r in results if r.get("skipped")]
    dry = [r for r in results if r.get("dry_run")]
    errs = [r for r in results if r.get("error")]
    click.echo(f"  ok       : {len(ok)}")
    click.echo(f"  dry-run  : {len(dry)}")
    click.echo(f"  skipped  : {len(skipped)}  (already retrofitted or no state)")
    click.echo(f"  errors   : {len(errs)}")
    for r in errs:
        click.echo(f"    {r['domain']}: {r['error']}")


if __name__ == "__main__":
    main()
