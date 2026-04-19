#!/usr/bin/env python3
"""List currently-available Contabo VPS products so you can pick a productId for .env.

Usage:
  ./scripts/list_contabo_products.py                 # all regions
  ./scripts/list_contabo_products.py --region EU     # filter to EU
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.contabo import ContaboClient


def _fmt(value) -> str:
    if value is None:
        return "-"
    return str(value)


@click.command()
@click.option("--region", default=None, help="Filter by region, e.g. EU, US-central, US-east, US-west, SIN.")
@click.option("--raw", is_flag=True, help="Print the full raw JSON from Contabo (for debugging).")
def main(region: str | None, raw: bool) -> None:
    load_dotenv(override=True)
    cb = ContaboClient()
    items = cb.list_product_availability(region=region)

    if not items:
        click.echo("No available products returned by Contabo. Check your API credentials and region filter.")
        sys.exit(1)

    if raw:
        click.echo(json.dumps(items, indent=2))
        return

    # Flatten into rows. Contabo's availability response groups availabilities by dataCenter.
    rows: list[tuple[str, str, str, str]] = []
    for item in items:
        pid = item.get("productId") or "?"
        name = item.get("displayName") or item.get("name") or "-"
        # Availability may be an inner list of {dataCenter, region, ...}
        availabilities = item.get("availabilities") or item.get("regions") or [item]
        for a in availabilities:
            a_region = a.get("region") or a.get("dataCenter") or "-"
            status = a.get("availability") or a.get("status") or "available"
            rows.append((pid, name, a_region, _fmt(status)))

    click.echo(f"{'productId':<10}  {'name':<32}  {'region':<16}  status")
    click.echo("-" * 76)
    for row in sorted(rows):
        click.echo(f"{row[0]:<10}  {row[1]:<32}  {row[2]:<16}  {row[3]}")

    click.echo("")
    click.echo("Pick the cheapest productId that meets your needs (>=2GB RAM) and set CONTABO_PRODUCT_ID=<id> in .env.")


if __name__ == "__main__":
    main()
