"""Shared, idempotent teardown legs used by every destroy path.

Historically teardown lived in two divergent places:

  * api/jobs.py:run_destroy (the operator-triggered / API destroy path)
  * scripts/destroy_shard.py (the legacy state-file CLI)

Neither removed the Instantly warmup seats or the Bison senders, and neither
cleared infra_shards.bison_loaded. The only place that flipped warmup rows to a
terminal state was, in practice, ad-hoc manual work. That divergence is the
root cause of the fleet drift the reconciliation audit found:

  * STALE bison_loaded=true  -> run_destroy only set status + destroyed_at
  * ZOMBIE Instantly seats   -> no destroy path removed Instantly accounts
  * ORPHAN Bison senders     -> no destroy path removed Bison senders

This module collects those legs behind small, idempotent helpers so every
destroy path (run_destroy, destroy_shard.py, reconcile_fleet.py --fix) runs the
SAME cleanup. Each helper is safe to re-run: it treats "already gone" (404 /
not found) as success and never raises on a missing target.

None of these helpers hardcode credentials. Instantly uses INSTANTLY_API_KEY
from the environment (via scripts.lib.instantly_api); Bison receives an explicit
per-workspace token from the caller; Webdock receives an explicit WebdockClient.
"""
from __future__ import annotations

from typing import Any, Iterable

import requests

# ---------------------------------------------------------------------------
# Registrable root-domain derivation
# ---------------------------------------------------------------------------
#
# The old derivation was `".".join(domain.split(".")[-2:])`, i.e. "last two
# labels". That is correct for single-label TLDs (foo.com -> foo.com) but wrong
# for multi-label public suffixes: it turns 10xmanagers.co.uk into "co.uk",
# which breaks every join on instantly_warmup_state.root_domain.
#
# We keep a small, explicit set of the multi-label public suffixes we actually
# register under (British and a few common ccTLD SLDs) rather than pulling in a
# full Public Suffix List dependency the container does not ship. If a domain
# ends in one of these, the registrable root is the suffix plus ONE more label;
# otherwise it is the last two labels.

# Multi-label public suffixes we register cold-email domains under. Extend this
# set (not the algorithm) when a new multi-label TLD is adopted.
MULTI_LABEL_SUFFIXES: frozenset[str] = frozenset(
    {
        "co.uk",
        "org.uk",
        "me.uk",
        "ltd.uk",
        "plc.uk",
        "net.uk",
        "sch.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.nz",
        "org.nz",
        "co.za",
        "com.br",
        "co.in",
    }
)


def registrable_root_domain(hostname: str) -> str:
    """Return the registrable root ("eTLD+1") for a hostname.

    Strips subdomains down to the registered domain, keeping the correct number
    of labels for multi-label public suffixes.

        registrable_root_domain("mail.foo.com")            -> "foo.com"
        registrable_root_domain("foo.com")                 -> "foo.com"
        registrable_root_domain("mail.sub.10xmanagers.co.uk") -> "10xmanagers.co.uk"
        registrable_root_domain("10xmanagers.co.uk")       -> "10xmanagers.co.uk"
        registrable_root_domain("foo.org")                 -> "foo.org"
        registrable_root_domain("localhost")               -> "localhost"

    An input that is already just a TLD-less token, or a bare TLD, is returned
    unchanged rather than raising.
    """
    host = (hostname or "").strip().strip(".").lower()
    if not host:
        return host
    labels = host.split(".")
    if len(labels) < 2:
        return host

    last_two = ".".join(labels[-2:])
    if last_two in MULTI_LABEL_SUFFIXES:
        # Registrable root = suffix + one more label to the left (if present).
        if len(labels) >= 3:
            return ".".join(labels[-3:])
        # Input IS the bare suffix (e.g. "co.uk"); nothing left to keep.
        return last_two
    return last_two


# ---------------------------------------------------------------------------
# Instantly warmup-seat removal (matches the retirement state-machine surface)
# ---------------------------------------------------------------------------


def remove_instantly_seats(emails: Iterable[str]) -> dict[str, Any]:
    """Take a batch of shard mailbox emails out of Instantly warmup.

    Mirrors how the retirement/scheduled-destruction flow retires an account:
    disable warmup first (so no further peer mail is scheduled), then delete the
    account outright. Instantly indexes accounts by email.

    Idempotent: a 404 / "not found" on delete means the account is already gone
    and is counted as removed. Returns a summary dict; never raises for a single
    missing account so a partial fleet sweep can continue.
    """
    from lib import instantly_api  # local import: env-dependent module

    email_list = [e.strip().lower() for e in emails if e and e.strip()]
    result: dict[str, Any] = {
        "requested": len(email_list),
        "disabled": 0,
        "deleted": 0,
        "already_gone": 0,
        "errors": [],
    }
    if not email_list:
        return result

    # Disable warmup as a batch first (best-effort; deletion is what removes it).
    try:
        instantly_api.disable_warmup(email_list)
        result["disabled"] = len(email_list)
    except Exception as exc:  # noqa: BLE001 - best-effort, deletion still runs
        result["errors"].append(f"disable_warmup: {str(exc)[:200]}")

    for email in email_list:
        try:
            instantly_api.delete_account(email)
            result["deleted"] += 1
        except requests.HTTPError as exc:
            body = (str(exc) or "").lower()
            status = getattr(getattr(exc, "response", None), "status_code", None)
            if status == 404 or "404" in body or "not found" in body:
                result["already_gone"] += 1
            else:
                result["errors"].append(f"{email}: {str(exc)[:150]}")
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"{email}: {str(exc)[:150]}")
    return result


# ---------------------------------------------------------------------------
# Bison sender removal
# ---------------------------------------------------------------------------
#
# There was NO Bison delete-sender path anywhere in the repo before this. Senders
# for a torn-down shard were simply left connected in the workspace, which is why
# zombie shards still showed up as Bison senders. Bison exposes
# DELETE /api/sender-emails/{id}; we call it directly with the workspace token.


def remove_bison_senders(bison_token: str, base_url: str, sender_ids: Iterable[int]) -> dict[str, Any]:
    """Delete Bison sender-email records by id in the given workspace.

    `bison_token` must be a token scoped to (or switched into) the workspace that
    owns these senders. Idempotent: a 404 means the sender is already gone and is
    counted as removed. Never raises for a single missing sender.
    """
    ids = [int(s) for s in sender_ids if s is not None]
    result: dict[str, Any] = {
        "requested": len(ids),
        "deleted": 0,
        "already_gone": 0,
        "errors": [],
    }
    if not ids:
        return result

    headers = {
        "Authorization": f"Bearer {bison_token.strip()}",
        "Accept": "application/json",
        "User-Agent": "curl/8.6.0",
        "Connection": "close",
    }
    base = base_url.rstrip("/")
    for sid in ids:
        try:
            resp = requests.delete(
                f"{base}/api/sender-emails/{sid}", headers=headers, timeout=60
            )
            if resp.status_code == 404:
                result["already_gone"] += 1
            elif resp.status_code >= 400:
                result["errors"].append(f"{sid}: {resp.status_code} {resp.text[:120]}")
            else:
                result["deleted"] += 1
        except Exception as exc:  # noqa: BLE001
            result["errors"].append(f"{sid}: {str(exc)[:150]}")
    return result


# ---------------------------------------------------------------------------
# Webdock confirmation
# ---------------------------------------------------------------------------


def webdock_server_exists(webdock_client, slug: str | None) -> bool | None:
    """Return True/False if we can determine whether a Webdock server exists.

    Returns None when we cannot tell (no client, no slug, or a transient API
    error) so callers can distinguish "confirmed gone" from "unknown" and avoid
    marking a shard destroyed on an inconclusive read.
    """
    if webdock_client is None or not slug:
        return None
    try:
        inst = webdock_client.get_instance(slug)
        return bool(inst and inst.get("id"))
    except Exception as exc:  # noqa: BLE001
        if "404" in str(exc) or "not found" in str(exc).lower():
            return False
        return None


def webdock_find_server_by_ip(webdock_client, ip: str | None) -> dict | None:
    """Best-effort lookup of a running Webdock server by its IPv4.

    The destroy path stores the slug in state, but reconciliation often only has
    the IP from infra_shards.vps_ip. Uses the SDK's generic list endpoint via
    make_request so we do not depend on a wrapper method that may not exist.
    Returns the raw server dict if found, else None. Never raises.
    """
    if webdock_client is None or not ip:
        return None
    try:
        sdk = getattr(webdock_client, "_sdk", None)
        if sdk is None:
            return None
        resp = sdk.make_request("servers", requestType="GET")
        data = resp.get("data") if isinstance(resp, dict) else resp
        for srv in data or []:
            for key in ("ipv4", "ipv4Address", "ip", "mainIp", "ipAddress"):
                if str(srv.get(key) or "") == ip:
                    return srv
    except Exception:  # noqa: BLE001 - reconciliation must not crash on this
        return None
    return None
