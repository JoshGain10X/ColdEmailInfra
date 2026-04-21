#!/usr/bin/env python3
"""Load a shard's mailboxes into a Bison workspace as IMAP/SMTP senders.

Reads shards/<domain>_bison.csv (produced by deploy_shard.py step 8),
discovers available workspaces from BISON_API_TOKENS (comma-separated
per-workspace tokens in .env), lets the user pick one, then creates
each sender via POST /api/sender-emails/imap-smtp and tags every
sender with "Custom SMTP" (or --tag).

Idempotent: re-running against the same CSV + workspace skips any
senders whose email already exists. The tag is still re-applied to
existing senders so they pick it up on re-runs.
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

import click
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.bison_api import BisonClient  # noqa: E402


SHARDS_DIR = Path(__file__).resolve().parents[1] / "shards"


def _discover_workspaces(tokens: list[str]) -> list[tuple[str, dict]]:
    """Probe each token, return [(token, workspace_dict)] for usable entries.

    Skips tokens that error out or return >1 workspace (super-admin tokens
    can't target a specific workspace for writes, so they aren't usable
    for bulk sender creation).
    """
    usable: list[tuple[str, dict]] = []
    for idx, token in enumerate(tokens, 1):
        client = BisonClient(token)
        try:
            workspaces = client.get_workspaces()
        except Exception as exc:
            click.echo(f"  Token {idx}: ERROR — {exc}")
            continue
        if not workspaces:
            click.echo(f"  Token {idx}: returned no workspaces, skipping")
            continue
        if len(workspaces) > 1:
            names = ", ".join(w.get("name", "?") for w in workspaces)
            click.echo(
                f"  Token {idx}: super-admin scope ({len(workspaces)} workspaces: "
                f"{names}) — skipping; add a per-workspace token instead"
            )
            continue
        ws = workspaces[0]
        usable.append((token, ws))
        click.echo(f"  Token {idx}: {ws.get('name')!r} (id={ws.get('id')})")
    return usable


@click.command()
@click.option("--domain", required=True, help="Shard root domain (matches shards/<domain>_bison.csv).")
@click.option("--workspace", default=None, help="Workspace name to target (skips interactive picker).")
@click.option("--tag", default="Custom SMTP", show_default=True, help="Tag to attach to every imported sender.")
@click.option("--csv-path", default=None, help="Override CSV path (default shards/<domain>_bison.csv).")
@click.option("--yes", is_flag=True, help="Skip the 'about to create N senders' confirmation.")
def main(domain: str, workspace: str | None, tag: str, csv_path: str | None, yes: bool) -> None:
    load_dotenv()

    csv_file = Path(csv_path) if csv_path else SHARDS_DIR / f"{domain}_bison.csv"
    if not csv_file.exists():
        raise click.ClickException(f"CSV not found at {csv_file}. Run deploy_shard first.")

    tokens_raw = os.environ.get("BISON_API_TOKENS", "").strip()
    if not tokens_raw:
        raise click.ClickException(
            "BISON_API_TOKENS not set in .env. Add a comma-separated list of "
            "per-workspace Bison tokens."
        )
    tokens = [t.strip() for t in tokens_raw.split(",") if t.strip()]

    click.echo("Discovering workspaces from BISON_API_TOKENS...")
    usable = _discover_workspaces(tokens)
    if not usable:
        raise click.ClickException("No usable per-workspace tokens in BISON_API_TOKENS.")

    # Pick workspace — by flag or interactive.
    if workspace:
        matches = [(tok, ws) for tok, ws in usable if ws.get("name") == workspace]
        if not matches:
            raise click.ClickException(
                f"Workspace {workspace!r} not found. Available: "
                f"{[ws.get('name') for _, ws in usable]}"
            )
        chosen_token, chosen_ws = matches[0]
    elif len(usable) == 1:
        chosen_token, chosen_ws = usable[0]
    else:
        click.echo("\nAvailable workspaces:")
        for i, (_, ws) in enumerate(usable, 1):
            click.echo(f"  {i}. {ws.get('name')} (id={ws.get('id')})")
        idx = click.prompt("Pick workspace", type=click.IntRange(1, len(usable)))
        chosen_token, chosen_ws = usable[idx - 1]

    click.echo(f"\nTarget workspace: {chosen_ws.get('name')} (id={chosen_ws.get('id')})")
    client = BisonClient(chosen_token)

    # Load CSV
    with csv_file.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    click.echo(f"Loaded {len(rows)} senders from {csv_file.name}")

    if not yes:
        click.confirm(
            f"Create up to {len(rows)} senders in {chosen_ws.get('name')!r} and tag each with {tag!r}?",
            abort=True,
        )

    # Existing senders in this workspace — used to skip duplicates.
    click.echo("Fetching existing senders in workspace...")
    existing = client.list_sender_emails()
    existing_by_email = {s["email"]: s for s in existing if s.get("email")}
    click.echo(f"  {len(existing)} senders already in workspace")

    # Find-or-create the tag.
    click.echo(f"Resolving tag {tag!r}...")
    tag_record = client.find_or_create_tag(tag)
    tag_id = tag_record.get("id")
    if not tag_id:
        raise click.ClickException(f"Could not resolve or create tag {tag!r}")
    click.echo(f"  Tag id={tag_id}")

    # Create senders.
    created_ids: list[int] = []
    skipped_ids: list[int] = []
    failed: list[tuple[str, str]] = []

    for i, row in enumerate(rows, 1):
        email = row.get("Email", "").strip()
        if not email:
            click.echo(f"  [{i:3}/{len(rows)}] row missing Email, skipping")
            continue

        if email in existing_by_email:
            sid = existing_by_email[email].get("id")
            if sid:
                skipped_ids.append(sid)
            click.echo(f"  [{i:3}/{len(rows)}] {email} — already exists (id={sid})")
            continue

        payload = {
            "name": row.get("Name", "").strip(),
            "email": email,
            "password": row.get("Password", ""),
            "imap_server": row.get("IMAP Server", "").strip(),
            "imap_port": int(row.get("IMAP Port", 993)),
            "smtp_server": row.get("SMTP Server", "").strip(),
            "smtp_port": int(row.get("SMTP Port", 465)),
            "smtp_secure": str(row.get("SMTP Secure", "TRUE")).strip().upper() == "TRUE",
            "imap_secure": str(row.get("IMAP Secure", "TRUE")).strip().upper() == "TRUE",
        }
        try:
            sender = client.create_sender_imap_smtp(payload)
            sid = sender.get("id")
            if sid:
                created_ids.append(sid)
            click.echo(f"  [{i:3}/{len(rows)}] {email} — created (id={sid})")
        except Exception as exc:
            failed.append((email, str(exc)))
            click.echo(f"  [{i:3}/{len(rows)}] {email} — FAILED: {exc}")

    # Attach tag to both newly created and already-existing senders, so
    # re-runs converge on a fully-tagged workspace.
    all_ids = created_ids + skipped_ids
    if all_ids:
        click.echo(f"\nAttaching tag {tag!r} to {len(all_ids)} senders...")
        try:
            client.attach_tag_to_senders(tag_id, all_ids)
            click.echo("  Done.")
        except Exception as exc:
            click.echo(f"  Tag attach FAILED: {exc}")

    click.echo(
        f"\nSummary: {len(created_ids)} created, "
        f"{len(skipped_ids)} already existed, {len(failed)} failed"
    )
    if failed:
        click.echo("Failures:")
        for email, err in failed:
            click.echo(f"  {email}: {err}")
        sys.exit(1)


if __name__ == "__main__":
    main()
