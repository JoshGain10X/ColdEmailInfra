from __future__ import annotations

import os
from typing import Optional

import requests


def notify_check_ip(ip: str, webhook_url: Optional[str] = None) -> None:
    """Fire-and-forget blocklist check via external webhook.

    Multi-tenant: callers pass the client's configured webhook
    (ctx.blocklist_webhook_url). Without one, falls back to the
    BLOCKLIST_WEBHOOK_URL env var (used historically by 10X). If neither
    is set, skips silently — no default URL, so a new client without
    monitoring configured doesn't accidentally ping someone else's webhook.

    The webhook receives `{"domain": "<ip>"}` and is expected to run an
    async workflow that notifies an operator if the IP is on a blocklist.
    The deploy continues regardless — if a problem is found the operator
    will be notified and can manually intervene.
    """
    target = webhook_url or os.environ.get("BLOCKLIST_WEBHOOK_URL")
    if not target:
        return
    try:
        requests.post(
            target,
            json={"domain": ip},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
    except Exception:
        pass  # Best-effort; don't block deploy on webhook failure
