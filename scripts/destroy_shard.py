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
@click.option("--cloudflare-zone-id", required=True, envvar="CLOUDFLARE_ZONE_ID")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt")
def main(domain: str, cloudflare_zone_id: str, yes: bool) -> None:
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

    click.echo(f"Removing DNS records for {domain}")
    removed = CloudflareClient().delete_records_matching(cloudflare_zone_id, domain)
    click.echo(f"  Removed {removed} records")

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
