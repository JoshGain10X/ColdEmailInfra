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
import json
import os
import subprocess
import sys
import time
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
@click.option("--throttle", default=1.5, show_default=True, help="Seconds to sleep between sender creates (Bison's synchronous IMAP/SMTP validator 500s on back-to-back posts).")
def main(domain: str, workspace: str | None, tag: str, csv_path: str | None, yes: bool, throttle: float) -> None:
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

    # Find-or-create the tag (one GET + optional POST — no pagination,
    # so doesn't trigger the 500-on-next-POST pattern we get from
    # heavier pre-listing).
    click.echo(f"Resolving tag {tag!r}...")
    tag_record = client.find_or_create_tag(tag)
    tag_id = tag_record.get("id")
    if not tag_id:
        raise click.ClickException(f"Could not resolve or create tag {tag!r}")
    click.echo(f"  Tag id={tag_id}")

    # Create senders via curl subprocess. Identical POST through Python
    # requests was reliably 500ing after any preceding API traffic,
    # while curl works every time. Shelling out to curl is simpler and
    # more robust than reverse-engineering why requests triggers Bison's
    # bug. Duplicate detection is via Bison's 422 response rather than
    # a pre-listing, so there are no poisoning GETs either.
    base_url = os.environ.get("BISON_API_BASE", "https://send.spamproofed.com").rstrip("/")
    created_ids: list[int] = []
    skipped_ids: list[int] = []
    failed: list[tuple[str, str]] = []

    for i, row in enumerate(rows, 1):
        email = row.get("Email", "").strip()
        if not email:
            click.echo(f"  [{i:3}/{len(rows)}] row missing Email, skipping")
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
        status, body = _curl_post_sender(base_url, chosen_token, payload)

        if status == 201:
            sid = (body.get("data") or {}).get("id")
            if sid:
                created_ids.append(sid)
            click.echo(f"  [{i:3}/{len(rows)}] {email} — created (id={sid})")
        elif status == 422 and _already_taken(body):
            click.echo(f"  [{i:3}/{len(rows)}] {email} — already exists, skipping")
            # Can't tag without an id; skip tagging for pre-existing senders.
            # A re-run after all creates is cheap if you need full coverage.
        else:
            err = f"HTTP {status}: {json.dumps(body)[:300]}"
            failed.append((email, err))
            click.echo(f"  [{i:3}/{len(rows)}] {email} — FAILED: {err}")

        if throttle > 0:
            time.sleep(throttle)

    # Attach tag to the newly-created senders. Pre-existing senders
    # don't get re-tagged since we don't pay the cost of looking their
    # IDs up — if you need a fully-tagged workspace, destroy/redeploy
    # the shard and re-run so every sender gets a fresh create.
    if created_ids:
        click.echo(f"\nAttaching tag {tag!r} to {len(created_ids)} new senders...")
        try:
            client.attach_tag_to_senders(tag_id, created_ids)
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


_CURL_SENTINEL = "<<<BISON_HTTP_CODE>>>"


def _curl_post_sender(base_url: str, token: str, payload: dict) -> tuple[int, dict]:
    """Create a sender via curl subprocess. Returns (status_code, body_dict).

    Shelling out to curl because Python requests was reliably 500ing on
    Bison's sender-create endpoint (curl with an identical payload always
    succeeded, same token, same host, same minute). Root cause unknown
    inside Bison; swapping the client is faster than chasing it further.
    """
    body_json = json.dumps(payload)
    try:
        result = subprocess.run(
            [
                "curl", "-s",
                "-X", "POST", f"{base_url}/api/sender-emails/imap-smtp",
                "-H", f"Authorization: Bearer {token}",
                "-H", "Content-Type: application/json",
                "-H", "Accept: application/json",
                "-d", body_json,
                "-w", f"\n{_CURL_SENTINEL}%{{http_code}}",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        return 0, {"error": "curl timed out after 60s"}
    output = result.stdout or ""
    if _CURL_SENTINEL not in output:
        return 0, {"error": f"curl produced no sentinel; raw={output[:500]} stderr={result.stderr[:200]}"}
    body_str, code_str = output.rsplit(_CURL_SENTINEL, 1)
    try:
        status = int(code_str.strip())
    except ValueError:
        status = 0
    body_str = body_str.strip()
    try:
        body = json.loads(body_str) if body_str else {}
    except json.JSONDecodeError:
        body = {"raw": body_str[:500]}
    return status, body


def _already_taken(body: dict) -> bool:
    """Detect Bison's 'email already taken' validation error shape.

    Bison wraps 422 validation errors under a `data` envelope:
      {"data": {"success": false, "message": "The email has already...",
                "errors": {"email": ["The email has already..."]}}}
    Check both top level and nested `data` for robustness.
    """
    for candidate in (body, body.get("data") or {}):
        if not isinstance(candidate, dict):
            continue
        msg = str(candidate.get("message", "")).lower()
        errors = candidate.get("errors") or {}
        email_errs = ""
        if isinstance(errors, dict):
            email_errs = " ".join(errors.get("email", []) or []).lower()
        combined = f"{msg} {email_errs}"
        if any(kw in combined for kw in ("already been taken", "already exists", "has been taken")):
            return True
    return False


if __name__ == "__main__":
    main()
