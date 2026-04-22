#!/usr/bin/env python3
"""Load a shard's mailboxes into a Bison workspace as IMAP/SMTP senders.

Bison's sender-create endpoint (POST /api/sender-emails/imap-smtp)
intermittently 500s when preceded by other API traffic from the same
process/IP. Everything in this script that happens *before* the first
create is therefore either a curl subprocess (cold TCP, no Python
state carried over) or gated behind a cool-off pause. Tag resolution
is deferred until after all creates so it never sits on the critical
path.
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


def _curl(method: str, base_url: str, path: str, token: str, body: dict | None = None) -> tuple[int, dict]:
    """Shell out to curl for a Bison API call. Returns (status_code, body_dict)."""
    cmd = [
        "curl", "-s",
        "-X", method, f"{base_url}{path}",
        "-H", f"Authorization: Bearer {token}",
        "-H", "Accept: application/json",
        "-w", f"\n{_CURL_SENTINEL}%{{http_code}}",
    ]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        return 0, {"error": "curl timed out after 60s"}

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
    """Via curl, probe each token and return [(token, workspace_dict)] for usable entries.

    Skips tokens that error out or return >1 workspace (super-admin tokens
    can't target a specific workspace for writes, so they aren't usable
    for bulk sender creation).
    """
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


def _already_taken(body: dict) -> bool:
    """Detect Bison's 'email already taken' 422 validation shape.

    Bison nests the validation envelope under a `data` key:
      {"data": {"success": false, "message": "The email has already...",
                "errors": {"email": ["The email has already..."]}}}
    Walk both top-level and nested `data` for robustness.
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


def _row_to_payload(row: dict) -> dict:
    return {
        "name": row.get("Name", "").strip(),
        "email": row.get("Email", "").strip(),
        "password": row.get("Password", ""),
        "imap_server": row.get("IMAP Server", "").strip(),
        "imap_port": int(row.get("IMAP Port", 993)),
        "smtp_server": row.get("SMTP Server", "").strip(),
        "smtp_port": int(row.get("SMTP Port", 465)),
        "smtp_secure": str(row.get("SMTP Secure", "TRUE")).strip().upper() == "TRUE",
        "imap_secure": str(row.get("IMAP Secure", "TRUE")).strip().upper() == "TRUE",
    }


@click.command()
@click.option("--domain", required=True, help="Shard root domain (matches shards/<domain>_bison.csv).")
@click.option("--workspace", default=None, help="Workspace name to target (skips interactive picker).")
@click.option("--tag", default="Custom SMTP", show_default=True, help="Tag to attach to every imported sender.")
@click.option("--csv-path", default=None, help="Override CSV path (default shards/<domain>_bison.csv).")
@click.option("--yes", is_flag=True, help="Skip the 'about to create N senders' confirmation.")
@click.option("--throttle", default=1.5, show_default=True, help="Seconds between sender creates.")
@click.option("--preflight", default=15.0, show_default=True,
              help="Seconds to sleep after workspace discovery and before the first create. Bison's sender-create 500s when preceded by recent API traffic; a cool-off here is the most reliable mitigation.")
def main(
    domain: str,
    workspace: str | None,
    tag: str,
    csv_path: str | None,
    yes: bool,
    throttle: float,
    preflight: float,
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

    with csv_file.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    click.echo(f"Loaded {len(rows)} senders from {csv_file.name}")

    if not yes:
        click.confirm(
            f"Create up to {len(rows)} senders in {chosen_ws.get('name')!r} and tag each with {tag!r}?",
            abort=True,
        )

    # Cool-off before first create. Nothing else between discovery and
    # the loop — tag resolution is deferred to after creates so it
    # doesn't sit on the critical path.
    if preflight > 0:
        click.echo(f"Waiting {preflight:.0f}s for Bison state to drain before first create...")
        time.sleep(preflight)

    created_ids: list[int] = []
    skipped: list[str] = []
    failed: list[tuple[str, str]] = []

    # Per-row retry plan: Bison's sender-create 500s intermittently when
    # its IMAP/SMTP validator pool is loaded. Waiting N seconds then
    # retrying almost always works. Schedule favours patience: total
    # worst-case ~3 min per stubborn row, which is a better user
    # experience than scripting around Bison's bug.
    retry_delays = [5, 10, 20, 40, 60]

    for i, row in enumerate(rows, 1):
        email = row.get("Email", "").strip()
        if not email:
            click.echo(f"  [{i:3}/{len(rows)}] row missing Email, skipping")
            continue
        payload = _row_to_payload(row)

        for attempt in range(len(retry_delays) + 1):
            status, body = _curl(
                "POST", base_url, "/api/sender-emails/imap-smtp",
                chosen_token, body=payload,
            )
            if status == 201:
                sid = (body.get("data") or {}).get("id")
                if sid:
                    created_ids.append(sid)
                suffix = f" (attempt {attempt + 1})" if attempt else ""
                click.echo(f"  [{i:3}/{len(rows)}] {email} — created (id={sid}){suffix}")
                break
            if status == 422 and _already_taken(body):
                skipped.append(email)
                click.echo(f"  [{i:3}/{len(rows)}] {email} — already exists, skipping")
                break
            # 500 or other transient error — wait and retry if we have budget.
            if attempt < len(retry_delays):
                delay = retry_delays[attempt]
                click.echo(
                    f"  [{i:3}/{len(rows)}] {email} — HTTP {status}, retrying in {delay}s "
                    f"(attempt {attempt + 1}/{len(retry_delays) + 1})"
                )
                time.sleep(delay)
                continue
            # Exhausted retries.
            err = f"HTTP {status}: {json.dumps(body)[:300]}"
            failed.append((email, err))
            click.echo(f"  [{i:3}/{len(rows)}] {email} — FAILED after {len(retry_delays) + 1} attempts: {err}")
            break

        if throttle > 0:
            time.sleep(throttle)

    # Resolve tag now (after creates, not before, so the tag GET/POST
    # doesn't sit on the critical path for the first create).
    if created_ids:
        click.echo(f"\nResolving tag {tag!r}...")
        tag_id = _resolve_tag(base_url, chosen_token, tag)
        if tag_id is None:
            click.echo(f"  Tag resolve FAILED — skipping tag attach")
        else:
            click.echo(f"  Tag id={tag_id}. Attaching to {len(created_ids)} new senders...")
            status, body = _curl(
                "POST", base_url, "/api/tags/attach-to-sender-emails", chosen_token,
                body={"tag_ids": [tag_id], "sender_email_ids": created_ids, "skip_webhooks": True},
            )
            if status == 200:
                click.echo("  Done.")
            else:
                click.echo(f"  Tag attach FAILED: HTTP {status}: {json.dumps(body)[:200]}")

    click.echo(
        f"\nSummary: {len(created_ids)} created, "
        f"{len(skipped)} already existed, {len(failed)} failed"
    )
    if failed:
        click.echo("Failures:")
        for email, err in failed:
            click.echo(f"  {email}: {err}")
        sys.exit(1)


def _resolve_tag(base_url: str, token: str, name: str) -> int | None:
    """Find an existing tag by name, or create it. Returns tag_id or None on error."""
    status, body = _curl("GET", base_url, "/api/tags", token)
    if status == 200:
        for tag in body.get("data") or []:
            if tag.get("name") == name:
                return tag.get("id")
    status, body = _curl("POST", base_url, "/api/tags", token, body={"name": name})
    if status in (200, 201):
        return (body.get("data") or {}).get("id")
    return None


if __name__ == "__main__":
    main()
