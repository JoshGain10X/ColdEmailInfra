"""Minimal Instantly v2 API wrapper used by run_load_to_bison.

Mirrors the surface of ~/.claude/skills/bison-deliverability/scripts/instantly_admin.py
so the two stay aligned. Only the methods needed at deploy time are exposed
here; the cron-side wrapper has the full surface.

Auth: Bearer INSTANTLY_API_KEY (same secret as Commission Compass).
"""

from __future__ import annotations

import os
import time
from typing import Iterable

import requests

INSTANTLY_BASE_URL = "https://api.instantly.ai/api/v2"
# Instantly provider codes (per api.instantly.ai/openapi/api_v2.json):
#   1 = Custom IMAP/SMTP   <- what we want
#   2 = Google (OAuth)
#   3 = Microsoft (OAuth)
PROVIDER_CODE_CUSTOM_SMTP = 1

# Warmup defaults applied at create time. Bison-deliverability skill's
# instantly_admin.py uses the same values.
DEFAULT_WARMUP_DAILY_LIMIT = 10
DEFAULT_WARMUP_INCREMENT = 1
DEFAULT_WARMUP_REPLY_RATE = 0.30
WARMUP_FILTER_TAG = "sointerested"


def _api_key() -> str:
    key = os.environ.get("INSTANTLY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("INSTANTLY_API_KEY env var not set on infra-api-v2 container")
    return key


def _headers(content_type: bool = True) -> dict:
    h = {"Authorization": f"Bearer {_api_key()}", "Accept": "application/json"}
    if content_type:
        h["Content-Type"] = "application/json"
    return h


def create_account(
    *,
    email: str,
    imap_host: str, imap_port: int,
    smtp_host: str, smtp_port: int,
    username: str, password: str,
    first_name: str = "", last_name: str = "",
    warmup_custom_ftag: str | None = None,
) -> dict:
    """Create a custom IMAP/SMTP account. `warmup_custom_ftag` should be the
    workspace's Bison warmup_filter_phrase so Bison recognises peer warmup
    mail and excludes it from reply stats. Falls back to the legacy universal
    tag for backwards compat (still works as a no-op against Bison's filter)."""
    payload = {
        "email": email,
        "first_name": first_name,
        "last_name": last_name,
        "provider_code": PROVIDER_CODE_CUSTOM_SMTP,
        "imap_username": username,
        "imap_password": password,
        "imap_host": imap_host,
        "imap_port": int(imap_port),
        "smtp_username": username,
        "smtp_password": password,
        "smtp_host": smtp_host,
        "smtp_port": int(smtp_port),
        # Warmup settings inline. warmup_custom_ftag must match the workspace's
        # Bison warmup_filter_phrase so Bison's IMAP poller recognises peer
        # warmup mail natively and excludes it from reply stats.
        "warmup": {
            "warmup_custom_ftag": warmup_custom_ftag or WARMUP_FILTER_TAG,
            "limit": DEFAULT_WARMUP_DAILY_LIMIT,
            "increment": DEFAULT_WARMUP_INCREMENT,
            "reply_rate": DEFAULT_WARMUP_REPLY_RATE,
        },
    }
    r = requests.post(
        f"{INSTANTLY_BASE_URL}/accounts",
        headers=_headers(),
        json=payload,
        timeout=60,
    )
    if not r.ok:
        raise requests.HTTPError(
            f"{r.status_code} on POST /accounts: {r.text[:400]}",
            response=r,
        )
    return r.json()


def enable_warmup(emails: Iterable[str]) -> dict:
    emails_list = list(emails)
    if not emails_list:
        return {"skipped": "no emails"}
    r = requests.post(
        f"{INSTANTLY_BASE_URL}/accounts/warmup/enable",
        headers=_headers(),
        json={"emails": emails_list},
        timeout=60,
    )
    if not r.ok:
        raise requests.HTTPError(
            f"{r.status_code} on POST /accounts/warmup/enable: {r.text[:400]}",
            response=r,
        )
    return r.json()


def disable_warmup(emails: Iterable[str]) -> dict:
    """Turn warmup OFF for the given accounts. Used when tearing a shard down —
    stops further peer warmup mail before the account is deleted. Mirrors the
    cron-side wrapper (warmup-poller/instantly_admin.py:disable_warmup)."""
    emails_list = list(emails)
    if not emails_list:
        return {"skipped": "no emails"}
    r = requests.post(
        f"{INSTANTLY_BASE_URL}/accounts/warmup/disable",
        headers=_headers(),
        json={"emails": emails_list},
        timeout=60,
    )
    if not r.ok:
        raise requests.HTTPError(
            f"{r.status_code} on POST /accounts/warmup/disable: {r.text[:400]}",
            response=r,
        )
    return r.json()


def delete_account(email_or_id: str) -> dict:
    """DELETE an account from Instantly (identified by EMAIL). Idempotent from
    the caller's side: a 404 raises requests.HTTPError which teardown treats as
    'already gone'. Content-Type must be ABSENT on this endpoint (Instantly
    enforces 'body must be null'); mirrors the cron-side wrapper."""
    url = f"{INSTANTLY_BASE_URL}/accounts/{email_or_id}"
    headers = {"Authorization": f"Bearer {_api_key()}", "Accept": "application/json"}
    r = requests.delete(url, headers=headers, timeout=60)
    if not r.ok:
        raise requests.HTTPError(
            f"{r.status_code} on DELETE /accounts/{email_or_id}: {r.text[:400]}",
            response=r,
        )
    try:
        return r.json()
    except ValueError:
        return {"status": "deleted", "email": email_or_id}
