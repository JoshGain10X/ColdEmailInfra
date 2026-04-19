#!/usr/bin/env python3
"""Destroy a shard: delete VPS, remove DNS records, archive state file."""
from __future__ import annotations

import shutil
import sys
import time
from pathlib import Path

import click
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.cloudflare import CloudflareClient
from lib.mailcheap import MailcheapClient
from lib.state import ShardState, SHARDS_DIR


@click.command()
@click.option("--domain", required=True)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
def main(domain: str, yes: bool) -> None:
    load_dotenv()
    state = ShardState(domain)
    if not state.path.exists():
        raise click.ClickException(f"No state for {domain}")

    if not yes:
        click.confirm(f"Destroy shard {domain} (VPS + DNS records)?", abort=True)

    vps = state.get("vps")
    if vps and vps.get("id"):
        click.echo(f"Destroying VPS {vps['id']}")
        try:
            MailcheapClient().destroy_vps(vps["id"])
        except Exception as exc:
            click.echo(f"  VPS destroy warning: {exc}")

    cf = CloudflareClient()
    zone_id = state.get("cloudflare_zone_id") or cf.get_zone_id(domain)
    if zone_id:
        click.echo(f"Removing DNS records for {domain} (zone {zone_id})")
        removed = cf.delete_records_matching(zone_id, domain)
        click.echo(f"  Removed {removed} records")
    else:
        click.echo(f"  No Cloudflare zone found for {domain}; skipping DNS cleanup")

    archive_dir = SHARDS_DIR / "archived"
    archive_dir.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%dT%H%M%S")
    target = archive_dir / f"{domain}-{timestamp}.json"
    shutil.move(str(state.path), target)

    bison_csv = SHARDS_DIR / f"{domain}_bison.csv"
    if bison_csv.exists():
        shutil.move(str(bison_csv), archive_dir / f"{domain}-{timestamp}_bison.csv")

    click.echo(f"Shard archived: {target}")


if __name__ == "__main__":
    main()
