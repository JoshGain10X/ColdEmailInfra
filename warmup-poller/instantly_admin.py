"""Instantly v2 API wrapper for warmup operations.

Single shared workspace + Bearer-token auth. The same INSTANTLY_API_KEY is
already used by Commission Compass's email-verification edge function.

Surface:
  - create_account(...)       -> POST /api/v2/accounts (provider_code=2 custom SMTP)
  - enable_warmup(emails)     -> POST /api/v2/accounts/warmup/enable
  - disable_warmup(emails)    -> POST /api/v2/accounts/warmup/disable
  - delete_account(id)        -> DELETE /api/v2/accounts/{id}
  - warmup_analytics(emails)  -> POST /api/v2/accounts/warmup-analytics

Rate-limited at ~30 req/min (workspace cap is 6,000/min, we stay well under).
Retry-on-5xx with exponential backoff. All methods are sync; the warmup
poller will batch them.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Iterable

import requests

# Reuse env loader from bison_pull.py
sys.path.insert(0, str(Path(__file__).parent))
from bison_pull import ENV  # type: ignore

INSTANTLY_BASE_URL = "https://api.instantly.ai/api/v2"
INSTANTLY_API_KEY = ENV.get("INSTANTLY_API_KEY", "")

# Settings the user agreed in plan mode
WARMUP_DAILY_LIMIT = 10
WARMUP_INCREMENT = 1
WARMUP_REPLY_RATE = 0.30

# Per-workspace Bison warmup_filter_phrase. Instantly's warmup_custom_ftag MUST
# match this exactly per workspace; that's what Bison uses to recognise warmup
# mail in IMAP and exclude it from reply stats. See:
#   ~/.claude/projects/-home-dev/memory/reference-bison-workspace-warmup-codes.md
# Prefer dynamic lookup via switch_workspace() in new code; this map is the
# fallback for scripts that can't easily re-call Bison.
WORKSPACE_WARMUP_PHRASES: dict[int, str] = {
    2: "osxuxcl1",  # 10X B2B Prospects L&D & HR
    3: "hakqhgbw",  # ReachOS
    5: "rbcyygg4",  # 10X C-Suite
    6: "eolgl9ik",  # 10X B2C Manager Prospects
    8: "hs43naxs",  # Scouted Candidates
    9: "adpetzlv",  # Scouted Employers
}

# Legacy universal tag - kept only because the Sieve filters on shards already
# reference it. Operative mechanism now is per-workspace phrase via Bison's
# native warmup-filter logic. New code should NOT use this.
WARMUP_FILTER_TAG = "sointerested"

# Instantly provider codes (per api.instantly.ai/openapi/api_v2.json):
#   1 = Custom IMAP/SMTP   <- what we want
#   2 = Google (OAuth)
#   3 = Microsoft (OAuth)
#   4 = AWS
#   8 = AirMail
PROVIDER_CODE_CUSTOM_SMTP = 1

_MIN_INTERVAL_S = 2.0  # ~30/min, conservative under the 100/min ceiling
_last_call: float = 0.0


def _headers() -> dict:
    if not INSTANTLY_API_KEY:
        raise RuntimeError("INSTANTLY_API_KEY not set in ~/.claude/skills/blitz-leads/.env")
    return {
        "Authorization": f"Bearer {INSTANTLY_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _throttle() -> None:
    """Sleep to honour the pacing budget."""
    global _last_call
    now = time.time()
    delta = now - _last_call
    if delta < _MIN_INTERVAL_S:
        time.sleep(_MIN_INTERVAL_S - delta)
    _last_call = time.time()


def _request(method: str, path: str, *, max_attempts: int = 5, **kw) -> requests.Response:
    """HTTP call with retry-on-5xx + exponential backoff."""
    url = f"{INSTANTLY_BASE_URL}{path}"
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        _throttle()
        try:
            r = requests.request(method, url, headers=_headers(), timeout=60, **kw)
            if r.status_code < 500:
                if not r.ok:
                    # Surface 4xx with body for caller to handle
                    raise requests.HTTPError(
                        f"{r.status_code} on {method} {path}: {r.text[:400]}",
                        response=r,
                    )
                return r
            print(f"  Instantly {r.status_code} {path} attempt {attempt}/{max_attempts}; retrying", file=sys.stderr)
            last_exc = requests.HTTPError(f"{r.status_code}: {r.text[:300]}", response=r)
        except (requests.ConnectionError, requests.Timeout) as e:
            print(f"  network error {path} attempt {attempt}/{max_attempts}: {e}", file=sys.stderr)
            last_exc = e
        if attempt < max_attempts:
            time.sleep(2 ** attempt)
    assert last_exc is not None
    raise last_exc


def _default_warmup_settings(custom_ftag: str | None = None) -> dict:
    """Nested warmup config used on create + patch. Field names per Instantly
    OpenAPI spec (https://api.instantly.ai/openapi/api_v2.json):
      - warmup_custom_ftag : per-workspace tag that lets Bison's IMAP poller
                              recognise warmup mail and exclude it from reply
                              stats. MUST match the workspace's Bison
                              `warmup_filter_phrase` exactly.
      - limit              : daily warmup volume ceiling
      - increment          : ramp step per day
      - reply_rate         : target % of warmup emails that get peer replies

    If `custom_ftag` is omitted, falls back to the legacy universal tag for
    backwards-compat. Callers should always pass a workspace-specific phrase.
    """
    return {
        "warmup_custom_ftag": custom_ftag or WARMUP_FILTER_TAG,
        "limit": WARMUP_DAILY_LIMIT,
        "increment": WARMUP_INCREMENT,
        "reply_rate": WARMUP_REPLY_RATE,
    }


def warmup_phrase_for_workspace(workspace_id: int) -> str:
    """Look up the per-workspace Bison warmup_filter_phrase from the static
    map. Raises if the workspace isn't mapped — callers should add to
    WORKSPACE_WARMUP_PHRASES when new workspaces are onboarded.
    """
    phrase = WORKSPACE_WARMUP_PHRASES.get(int(workspace_id))
    if not phrase:
        raise RuntimeError(
            f"no warmup_filter_phrase mapped for workspace_id={workspace_id}; "
            f"add to WORKSPACE_WARMUP_PHRASES in instantly_admin.py"
        )
    return phrase


def create_account(
    *,
    email: str,
    imap_host: str,
    imap_port: int,
    smtp_host: str,
    smtp_port: int,
    username: str,
    password: str,
    first_name: str = "",
    last_name: str = "",
    warmup_custom_ftag: str | None = None,
) -> dict:
    """Create a custom IMAP/SMTP account in Instantly with warmup settings
    pre-configured (filter tag, daily limit, increment, reply rate). Warmup
    is still enabled via the separate /warmup/enable endpoint - this just
    sets the settings ahead of time so peer mail carries the filter tag from
    the very first warmup send."""
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
        "warmup": _default_warmup_settings(warmup_custom_ftag),
    }
    r = _request("POST", "/accounts", json=payload)
    body = r.json()
    return body.get("data") or body


def patch_warmup_settings(
    email: str,
    *,
    custom_ftag: str | None = None,
    settings: dict | None = None,
) -> dict:
    """PATCH an existing account to set/overwrite its warmup config.

    Two calling conventions:
      - patch_warmup_settings(email, custom_ftag="xyz")  - common case,
        uses default daily_limit/increment/reply_rate plus the given ftag.
      - patch_warmup_settings(email, settings={...})     - full override.
    """
    payload = {"warmup": settings or _default_warmup_settings(custom_ftag)}
    r = _request("PATCH", f"/accounts/{email}", json=payload)
    return r.json()


def enable_warmup(emails: Iterable[str]) -> dict:
    """Turn warmup ON for the given accounts (by email). Returns the job dict."""
    emails_list = list(emails)
    if not emails_list:
        return {"skipped": "no emails"}
    r = _request("POST", "/accounts/warmup/enable", json={"emails": emails_list})
    return r.json()


def disable_warmup(emails: Iterable[str]) -> dict:
    """Turn warmup OFF for the given accounts. Used when scheduling destruction."""
    emails_list = list(emails)
    if not emails_list:
        return {"skipped": "no emails"}
    r = _request("POST", "/accounts/warmup/disable", json={"emails": emails_list})
    return r.json()


def delete_account(email_or_id: str) -> dict:
    """DELETE an account from Instantly. Instantly identifies accounts by EMAIL.

    The endpoint is picky about Content-Type — it must be absent (Instantly
    enforces `body must be null` on this method). _request() sets Content-Type
    by default; we pass headers explicitly with no Content-Type here.
    """
    if not INSTANTLY_API_KEY:
        raise RuntimeError("INSTANTLY_API_KEY not set")
    url = f"{INSTANTLY_BASE_URL}/accounts/{email_or_id}"
    headers = {
        "Authorization": f"Bearer {INSTANTLY_API_KEY}",
        "Accept": "application/json",
        # NB: no Content-Type
    }
    _throttle()
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


def warmup_analytics(emails: Iterable[str]) -> dict | list:
    """Returns Instantly's raw warmup-analytics response. Shape (2026):
        {
          "email_date_data": {<email>: {<YYYY-MM-DD>: {...}}, ...},
          "aggregate_data":  {<email>: {...}, ...}
        }
    Caller (process_warmup_poller._normalise_analytics) handles parsing.
    Returns empty dict if no analytics yet (accounts newly created).
    Caller is responsible for batching (Instantly recommends <=100 emails per call).
    """
    emails_list = list(emails)
    if not emails_list:
        return {}
    r = _request("POST", "/accounts/warmup-analytics", json={"emails": emails_list})
    return r.json()
