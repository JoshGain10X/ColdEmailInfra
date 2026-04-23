from __future__ import annotations

import requests

_BLOCKLIST_WEBHOOK = (
    "https://n8n.10xmanagers.com/webhook/"
    "5876c957-402a-4e87-a2c5-66de112640af"
)


def notify_check_ip(ip: str) -> None:
    """Fire-and-forget blocklist check via external n8n webhook.

    The webhook triggers an async workflow that sends a notification if the
    IP is blocklisted. The deploy continues regardless — if a problem is
    found the operator will be notified and can manually intervene.
    """
    try:
        requests.post(
            _BLOCKLIST_WEBHOOK,
            json={"domain": ip},
            headers={"Content-Type": "application/json"},
            timeout=10,
        )
    except Exception:
        pass  # Best-effort; don't block deploy on webhook failure
