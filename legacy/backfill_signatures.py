#!/usr/bin/env python3
"""Backfill HTML email signatures for all Bison senders missing them.

Iterates all workspaces from the bison_workspaces table, pages through
every sender, and PATCHes a unique HTML signature on any sender with
email_signature == null.

Run on the API VPS which has .env with SUPABASE_URL/SUPABASE_SERVICE_KEY.
"""
import os
import random
import time
import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv(override=True)

BISON_BASE = os.environ.get("BISON_API_BASE", "https://send.spamproofed.com").rstrip("/")
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

COMPANY = "10X Managers"


def generate_signature(name: str, email: str) -> str:
    """Generate a randomised HTML email signature."""
    parts = name.strip().split(" ", 1)
    first = parts[0] if parts else "Team"
    last = parts[1] if len(parts) > 1 else ""

    templates = [
        f"<p>{first} {last}</p>",
        f"<p><strong>{first} {last}</strong> | {COMPANY}</p>",
        f"<p>{first} {last}<br>{COMPANY}</p>",
        f"<p>{first} {last}<br>{email}</p>",
        f"<p><strong>{first} {last}</strong> | {COMPANY}<br>{email}</p>",
        f"<p>{first} {last}<br>{COMPANY}<br>{email}</p>",
        f"<p>{first} {last} - {COMPANY}</p>",
        f"<p>{first} {last}, {COMPANY}</p>",
        f"<p>{first}<br>{COMPANY}</p>",
        f"<p>{first} {last} | {email}</p>",
        f"<p>{first} from {COMPANY}</p>",
        f"<p>Best,<br>{first} {last}</p>",
        f"<p>Thanks,<br>{first}</p>",
        f"<p>Cheers,<br>{first} {last}<br>{COMPANY}</p>",
        f"<p>{first} {last}<br>{COMPANY} Team</p>",
    ]
    sig = random.choice(templates)

    if random.random() < 0.10:
        mobile_tags = ["Sent from my iPhone", "Sent from my mobile", "Sent from mobile"]
        sig += f'<p style="font-size:12px;color:#888;">{random.choice(mobile_tags)}</p>'

    return sig


def process_workspace(ws_name: str, api_key: str) -> dict:
    """Process all senders in a workspace, return counts."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    updated = 0
    skipped = 0
    failed = 0
    page = 1

    while True:
        resp = requests.get(
            f"{BISON_BASE}/api/sender-emails",
            headers=headers,
            params={"page": page, "per_page": 100},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"    ERROR listing page {page}: {resp.status_code}")
            break

        data = resp.json()
        senders = data.get("data", [])
        if not senders:
            break

        meta = data.get("meta", {})
        total = meta.get("total", "?")
        last_page = meta.get("last_page", page)

        for sender in senders:
            sid = sender["id"]
            name = sender.get("name", "")
            email = sender.get("email", "")
            existing_sig = sender.get("email_signature")

            if existing_sig:
                skipped += 1
                continue

            sig = generate_signature(name, email)
            patch_resp = requests.patch(
                f"{BISON_BASE}/api/sender-emails/{sid}",
                headers=headers,
                json={"email_signature": sig},
                timeout=15,
            )
            if patch_resp.status_code == 200:
                updated += 1
            else:
                failed += 1
                if failed <= 3:
                    print(f"    FAIL sender {sid} ({email}): {patch_resp.status_code} {patch_resp.text[:100]}")

            # Small delay to avoid rate limiting
            time.sleep(0.15)

        print(f"    Page {page}/{last_page} ({total} total) — updated: {updated}, skipped: {skipped}, failed: {failed}")

        if page >= last_page:
            break
        page += 1

    return {"updated": updated, "skipped": skipped, "failed": failed}


def main():
    # Get all workspace API keys
    result = sb.table("bison_workspaces").select("workspace_name, api_key").order("created_at").execute()
    workspaces = result.data

    print(f"Found {len(workspaces)} workspaces\n")

    grand_total = {"updated": 0, "skipped": 0, "failed": 0}

    for ws in workspaces:
        name = ws["workspace_name"]
        key = ws["api_key"]
        print(f"{'='*60}")
        print(f"  Workspace: {name}")
        print(f"{'='*60}")

        counts = process_workspace(name, key)
        for k in grand_total:
            grand_total[k] += counts[k]

        print(f"  Done: {counts}\n")

    print(f"\n{'='*60}")
    print(f"GRAND TOTAL: {grand_total}")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
