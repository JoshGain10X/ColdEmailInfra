#!/usr/bin/env python3
"""List Webdock locations, profiles, and Ubuntu images so you can populate
WEBDOCK_LOCATION_ID / WEBDOCK_PROFILE_SLUG / WEBDOCK_IMAGE_SLUG in .env.

Usage:
    python3 scripts/webdock_discover.py             # all locations + their profiles, all Ubuntu images
    python3 scripts/webdock_discover.py --location fi  # profiles for just one location
"""
from __future__ import annotations

import sys
from pathlib import Path

import click
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.webdock import WebdockClient


@click.command()
@click.option("--location", default=None, help="Limit profile listing to one locationId")
def main(location: str | None) -> None:
    load_dotenv()
    c = WebdockClient()

    locations = c._unwrap(c._sdk.get_locations()) or []
    click.echo("Locations:")
    for loc in locations:
        click.echo(f"  {loc.get('id'):<10} {loc.get('name')} ({loc.get('city')}, {loc.get('country')})")

    target_locs = [location] if location else [loc["id"] for loc in locations]
    for loc_id in target_locs:
        click.echo(f"\nProfiles @ {loc_id}:")
        profiles = c._unwrap(c._sdk.get_profiles(loc_id)) or []
        for p in profiles:
            ram = p.get("ram")
            cpu = (p.get("cpu") or {}).get("cores")
            disk = p.get("disk")
            price = (p.get("price") or {}).get("amount")
            currency = (p.get("price") or {}).get("currency") or ""
            click.echo(
                f"  {p.get('slug'):<28} {p.get('name'):<24} "
                f"{cpu}vCPU / {ram}MB RAM / {disk}GB  {price} {currency}/mo"
            )

    click.echo("\nImages (Ubuntu only):")
    images = c._unwrap(c._sdk.get_images()) or []
    for img in images:
        slug = img.get("slug") or ""
        if "ubuntu" not in slug.lower() and "ubuntu" not in (img.get("name") or "").lower():
            continue
        click.echo(f"  {slug:<32} {img.get('name')}")


if __name__ == "__main__":
    main()
