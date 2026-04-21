from __future__ import annotations

import os
import time
from typing import Any

import requests


class BisonClient:
    """Email Bison API client.

    Bison (https://github.com/emailbison) scopes every request to a single
    workspace via the bearer token. A per-workspace token sees only its
    own workspace from GET /api/workspaces/v1.1; a super-admin token sees
    all of them but cannot target a specific workspace for writes, so this
    client assumes a per-workspace token.
    """

    def __init__(self, token: str, base_url: str | None = None):
        self.token = token.strip()
        self.base_url = (base_url or os.environ.get(
            "BISON_API_BASE", "https://send.spamproofed.com"
        )).rstrip("/")

    def _headers(self) -> dict[str, str]:
        # User-Agent explicitly mimics curl: Bison's sender-create endpoint
        # was reliably 500ing on python-requests while identical curl calls
        # succeeded. Keeping UA, Connection: close, and fresh TCP per
        # request (no Session) brings behaviour in line with curl.
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "curl/8.6.0",
            "Connection": "close",
            "Expect": "",
        }

    def _request(
        self,
        method: str,
        path: str,
        retries: int = 3,
        backoff: float = 2.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Issue a request, retrying on 5xx with exponential backoff.

        No requests.Session: every call opens a fresh TCP/TLS connection,
        matching curl's behaviour. Pooled keep-alive triggered server-side
        500s on Bison's sender-create endpoint.
        """
        last_exc: requests.HTTPError | None = None
        for attempt in range(1, retries + 1):
            resp = requests.request(
                method,
                f"{self.base_url}{path}",
                headers={**self._headers(), **kwargs.pop("headers", {})},
                timeout=60,
                **kwargs,
            )
            if resp.status_code >= 500 and attempt < retries:
                time.sleep(backoff * attempt)
                continue
            if resp.status_code >= 400:
                last_exc = requests.HTTPError(
                    f"{resp.status_code} {resp.text}", response=resp
                )
                raise last_exc
            if not resp.content:
                return {}
            return resp.json()
        if last_exc is not None:
            raise last_exc
        return {}

    # ------------------------------------------------------------------
    # Workspaces
    # ------------------------------------------------------------------

    def get_workspaces(self) -> list[dict]:
        """Return the workspace(s) visible to this token.

        A per-workspace token returns exactly one entry; a super-admin
        token returns the full list. Callers use list length to detect.
        """
        body = self._request("GET", "/api/workspaces/v1.1")
        return body.get("data", [])

    # ------------------------------------------------------------------
    # Sender emails (IMAP/SMTP email accounts)
    # ------------------------------------------------------------------

    def list_sender_emails(
        self,
        search: str | None = None,
        page_delay: float = 0.3,
    ) -> list[dict]:
        """Return sender emails in the current workspace.

        Pass `search` to scope to a substring (e.g. the shard's domain)
        — critical on workspaces with thousands of unrelated senders,
        where unscoped pagination triggers 500s on subsequent writes.

        Bison paginates at 15/page by default; we request 100/page and
        walk `meta.last_page`. A small `page_delay` between fetches
        keeps rapid listing from poisoning Bison's internal rate limiter
        or IMAP validator state, which reliably causes the first
        subsequent POST to fail 500.
        """
        all_items: list[dict] = []
        page = 1
        while True:
            params = [f"page={page}", "per_page=100"]
            if search:
                params.append(f"search={search}")
            query = "&".join(params)
            body = self._request("GET", f"/api/sender-emails?{query}")
            all_items.extend(body.get("data", []))
            meta = body.get("meta") or {}
            last = meta.get("last_page", page)
            current = meta.get("current_page", page)
            if current >= last:
                break
            page += 1
            if page_delay > 0:
                time.sleep(page_delay)
        return all_items

    def create_sender_imap_smtp(self, payload: dict) -> dict:
        """Create a sender email via POST /api/sender-emails/imap-smtp.

        Required keys in payload: name, email, password, imap_server,
        imap_port, smtp_server, smtp_port. Optional: imap_secure,
        smtp_secure, email_signature.
        """
        body = self._request(
            "POST", "/api/sender-emails/imap-smtp", json=payload
        )
        return body.get("data") or {}

    # ------------------------------------------------------------------
    # Tags
    # ------------------------------------------------------------------

    def list_tags(self) -> list[dict]:
        body = self._request("GET", "/api/tags")
        return body.get("data", [])

    def create_tag(self, name: str) -> dict:
        body = self._request("POST", "/api/tags", json={"name": name})
        return body.get("data") or {}

    def find_or_create_tag(self, name: str) -> dict:
        for tag in self.list_tags():
            if tag.get("name") == name:
                return tag
        return self.create_tag(name)

    def attach_tag_to_senders(
        self, tag_id: int, sender_email_ids: list[int]
    ) -> None:
        if not sender_email_ids:
            return
        self._request(
            "POST",
            "/api/tags/attach-to-sender-emails",
            json={
                "tag_ids": [tag_id],
                "sender_email_ids": sender_email_ids,
                "skip_webhooks": True,
            },
        )
