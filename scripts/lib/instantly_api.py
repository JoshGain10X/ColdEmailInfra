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
PROVIDER_CODE_CUSTOM_SMTP = 2

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
) -> dict:
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
