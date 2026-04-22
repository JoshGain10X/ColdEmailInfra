#!/usr/bin/env python3
"""Bulk-upload a shard's mailboxes into a Bison workspace.

Reads shards/<domain>_bison.csv (produced by deploy_shard.py step 8),
lets the user pick a workspace from BISON_API_TOKENS, uploads the CSV
to POST /api/sender-emails/bulk in one shot, then attaches the
"Custom SMTP" tag (or --tag) to every created sender.
"""
from __future__ import annotations

import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import click
from dotenv import load_dotenv


SHARDS_DIR = Path(__file__).resolve().parents[1] / "shards"
_CURL_SENTINEL = "<<<BISON_HTTP_CODE>>>"


def _curl(method: str, base_url: str, path: str, token: str, body: dict | None = None, form: list[str] | None = None) -> tuple[int, dict]:
    """Shell out to curl for a Bison API call. Returns (status_code, body_dict).

    `body` is JSON-encoded (compact) and sent as Content-Type: application/json.
    `form` is a list of `-F` args (e.g. ["csv=@path/to/file.csv"]) sent as
    multipart/form-data. Mutually exclusive; pass at most one.
    """
    cmd = [
        "curl", "-s",
        "-X", method, f"{base_url}{path}",
        "-H", f"Authorization: Bearer {token}",
        "-H", "Accept: application/json",
        "-w", f"\n{_CURL_SENTINEL}%{{http_code}}",
    ]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json",
                "-d", json.dumps(body, separators=(",", ":"))]
    if form is not None:
        for f in form:
            cmd += ["-F", f]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        return 0, {"error": "curl timed out after 300s"}

    output = result.stdout or ""
    if _CURL_SENTINEL not in output:
        return 0, {"error": f"curl produced no sentinel; raw={output[:400]} stderr={result.stderr[:200]}"}
    body_str, code_str = output.rsplit(_CURL_SENTINEL, 1)
    try:
        status = int(code_str.strip())
    except ValueError:
        status = 0
    body_str = body_str.strip()
    try:
        parsed = json.loads(body_str) if body_str else {}
    except json.JSONDecodeError:
        parsed = {"raw": body_str[:500]}
    return status, parsed


def _discover_workspaces(base_url: str, tokens: list[str]) -> list[tuple[str, dict]]:
    """Return [(token, workspace_dict)] for per-workspace tokens."""
    usable: list[tuple[str, dict]] = []
    for idx, token in enumerate(tokens, 1):
        status, body = _curl("GET", base_url, "/api/workspaces/v1.1", token)
        if status != 200:
            click.echo(f"  Token {idx}: HTTP {status} — {json.dumps(body)[:200]}")
            continue
        workspaces = body.get("data") or []
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


def _resolve_tag(base_url: str, token: str, name: str) -> int | None:
    status, body = _curl("GET", base_url, "/api/tags", token)
    if status == 200:
        for tag in body.get("data") or []:
            if tag.get("name") == name:
                return tag.get("id")
    status, body = _curl("POST", base_url, "/api/tags", token, body={"name": name})
    if status in (200, 201):
        return (body.get("data") or {}).get("id")
    return None


@click.command()
@click.option("--domain", required=True, help="Shard root domain (matches shards/<domain>_bison.csv).")
@click.option("--workspace", default=None, help="Workspace name to target (skips interactive picker).")
@click.option("--tag", default="Custom SMTP", show_default=True, help="Tag to attach to every imported sender.")
@click.option("--csv-path", default=None, help="Override CSV path (default shards/<domain>_bison.csv).")
@click.option("--yes", is_flag=True, help="Skip the 'about to upload' confirmation.")
def main(
    domain: str,
    workspace: str | None,
    tag: str,
    csv_path: str | None,
    yes: bool,
) -> None:
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
    base_url = os.environ.get("BISON_API_BASE", "https://send.spamproofed.com").rstrip("/")

    click.echo("Discovering workspaces from BISON_API_TOKENS...")
    usable = _discover_workspaces(base_url, tokens)
    if not usable:
        raise click.ClickException("No usable per-workspace tokens in BISON_API_TOKENS.")

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

    # Count rows for the confirmation prompt (excluding header).
    with csv_file.open(newline="") as fh:
        row_count = sum(1 for _ in csv.DictReader(fh))
    click.echo(f"CSV: {csv_file} ({row_count} senders)")

    if not yes:
        click.confirm(
            f"Bulk-upload {row_count} senders to {chosen_ws.get('name')!r} and tag each with {tag!r}?",
            abort=True,
        )

    click.echo(f"\nUploading CSV to POST /api/sender-emails/bulk ...")
    status, body = _curl(
        "POST", base_url, "/api/sender-emails/bulk", chosen_token,
        form=[f"csv=@{csv_file}"],
    )

    if status not in (200, 201):
        click.echo(f"  FAILED: HTTP {status}: {json.dumps(body)[:800]}")
        sys.exit(1)

    created = body.get("data") or []
    created_ids = [s.get("id") for s in created if s.get("id")]
    click.echo(f"  Bulk upload accepted: {len(created_ids)} senders created")

    if not created_ids:
        click.echo("No new sender IDs returned — nothing to tag.")
        return

    # Attach tag to all created senders.
    click.echo(f"\nResolving tag {tag!r}...")
    tag_id = _resolve_tag(base_url, chosen_token, tag)
    if tag_id is None:
        click.echo(f"  Tag resolve FAILED — senders created but not tagged")
        sys.exit(1)
    click.echo(f"  Tag id={tag_id}. Attaching to {len(created_ids)} senders...")
    status, body = _curl(
        "POST", base_url, "/api/tags/attach-to-sender-emails", chosen_token,
        body={"tag_ids": [tag_id], "sender_email_ids": created_ids, "skip_webhooks": True},
    )
    if status == 200:
        click.echo("  Done.")
    else:
        click.echo(f"  Tag attach FAILED: HTTP {status}: {json.dumps(body)[:300]}")
        sys.exit(1)

    click.echo(f"\nSummary: {len(created_ids)} senders uploaded and tagged in {chosen_ws.get('name')!r}")


if __name__ == "__main__":
    main()
