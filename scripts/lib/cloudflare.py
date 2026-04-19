from __future__ import annotations

import os
import time
from typing import Any

import requests


API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareClient:
    def __init__(self, token: str | None = None, account_id: str | None = None):
        raw_token = token or os.environ["CLOUDFLARE_API_TOKEN"]
        self.token = raw_token.strip()
        self.account_id = (account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID") or "").strip() or None
        self._validate_token(raw_token, self.token)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })

    @staticmethod
    def _validate_token(raw: str, cleaned: str) -> None:
        if not cleaned:
            raise RuntimeError("CLOUDFLARE_API_TOKEN is empty. Check .env is in the current directory and has the token set.")
        if cleaned.startswith("<") or cleaned.endswith(">"):
            raise RuntimeError(f"CLOUDFLARE_API_TOKEN looks like a placeholder ({cleaned[:12]}...). Replace with the actual token from dash.cloudflare.com/profile/api-tokens.")
        if any(c in cleaned for c in " \t\n\r"):
            raise RuntimeError(
                f"CLOUDFLARE_API_TOKEN contains whitespace inside the value (len={len(cleaned)}). "
                "Open .env, delete the value, re-paste from Cloudflare without wrapping quotes."
            )
        if not all(c.isascii() and (c.isalnum() or c in "-_") for c in cleaned):
            bad = [repr(c) for c in cleaned if not (c.isascii() and (c.isalnum() or c in "-_"))]
            raise RuntimeError(
                f"CLOUDFLARE_API_TOKEN contains unexpected characters: {', '.join(bad[:5])}. "
                "Tokens are alphanumeric + dashes/underscores only. Re-copy from the Cloudflare dashboard."
            )
        if len(cleaned) < 30 or len(cleaned) > 60:
            raise RuntimeError(
                f"CLOUDFLARE_API_TOKEN has unexpected length (got {len(cleaned)}, "
                "expected ~40 characters). You've likely pasted the wrong value — "
                "go to https://dash.cloudflare.com/profile/api-tokens, click your "
                "token's row, and copy ONLY the 40-character token string "
                "(no 'Bearer ', no quotes, no surrounding text, not the Account ID)."
            )

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        resp = self.session.request(method, f"{API_BASE}{path}", timeout=30, **kwargs)
        try:
            body = resp.json()
        except ValueError:
            raise RuntimeError(
                f"Cloudflare HTTP {resp.status_code} on {method} {path}: {resp.text[:500]}"
            )
        if resp.status_code >= 400 or not body.get("success", True):
            errors = body.get("errors") or body.get("messages") or body
            raise RuntimeError(
                f"Cloudflare API {resp.status_code} on {method} {path}: {errors}"
            )
        return body

    # ------------------------------------------------------------------
    # Zone discovery / creation
    # ------------------------------------------------------------------

    def get_zone_id(self, domain: str) -> str | None:
        body = self._request("GET", "/zones", params={"name": domain})
        results = body.get("result") or []
        return results[0]["id"] if results else None

    def wait_for_zone(self, domain: str, timeout: int = 300) -> str:
        deadline = time.time() + timeout
        while time.time() < deadline:
            zone_id = self.get_zone_id(domain)
            if zone_id:
                return zone_id
            time.sleep(10)
        raise TimeoutError(f"Zone for {domain} did not appear within {timeout}s")

    # ------------------------------------------------------------------
    # Registrar (purchase new domains via Cloudflare Registrar API)
    # ------------------------------------------------------------------

    def _registrar_path(self, suffix: str = "") -> str:
        if not self.account_id:
            raise RuntimeError("CLOUDFLARE_ACCOUNT_ID is required for Registrar API calls")
        return f"/accounts/{self.account_id}/registrar{suffix}"

    def registrar_domain_info(self, domain: str) -> dict | None:
        """Return existing domain record if we already own it, else None."""
        try:
            body = self._request("GET", self._registrar_path(f"/domains/{domain}"))
            return body.get("result")
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code == 404:
                return None
            raise

    def registrar_check_availability(self, domain: str) -> dict:
        """Return availability + pricing. Endpoint shape follows the 2026 Registrar API beta."""
        body = self._request("GET", self._registrar_path(f"/domains/{domain}/availability"))
        return body.get("result", {})

    def registrar_register(self, domain: str, years: int = 1, privacy: bool = True) -> dict:
        """Register a new domain via Cloudflare Registrar. Blocks until registration is final."""
        payload = {
            "name": domain,
            "period": years,
            "privacy": privacy,
        }
        body = self._request("POST", self._registrar_path("/registrations"), json=payload)
        result = body.get("result", {})

        deadline = time.time() + 600
        while time.time() < deadline:
            info = self.registrar_domain_info(domain)
            if info and info.get("status") in ("active", "ok", "registered"):
                return info
            time.sleep(10)
        raise TimeoutError(f"Domain {domain} did not become active within 10 minutes (last result: {result})")

    # ------------------------------------------------------------------
    # DNS records
    # ------------------------------------------------------------------

    def list_records(self, zone_id: str, name: str | None = None, type: str | None = None) -> list[dict]:
        params: dict[str, Any] = {"per_page": 100}
        if name:
            params["name"] = name
        if type:
            params["type"] = type
        results: list[dict] = []
        page = 1
        while True:
            params["page"] = page
            body = self._request("GET", f"/zones/{zone_id}/dns_records", params=params)
            results.extend(body["result"])
            info = body.get("result_info", {})
            if page >= info.get("total_pages", 1):
                break
            page += 1
        return results

    def upsert_record(self, zone_id: str, type: str, name: str, content: str, **extra) -> dict:
        """Create or update a DNS record. Matches existing records by (type, name)."""
        existing = self.list_records(zone_id, name=name, type=type)
        payload = {"type": type, "name": name, "content": content, "ttl": 1, **extra}
        if existing:
            record = existing[0]
            body = self._request("PUT", f"/zones/{zone_id}/dns_records/{record['id']}", json=payload)
            return body["result"]
        body = self._request("POST", f"/zones/{zone_id}/dns_records", json=payload)
        return body["result"]

    def delete_records_matching(self, zone_id: str, suffix: str) -> int:
        """Delete every DNS record whose name ends with `suffix`. Returns count."""
        records = self.list_records(zone_id)
        removed = 0
        for r in records:
            if r["name"] == suffix or r["name"].endswith(f".{suffix}"):
                self._request("DELETE", f"/zones/{zone_id}/dns_records/{r['id']}")
                removed += 1
        return removed

    def ensure_redirect_rule(self, zone_id: str, source_domain: str, target: str) -> None:
        """Create a Cloudflare Single Redirect rule: source_domain/* -> target/$1 (301).

        Uses the Rulesets API (zone-level http_request_dynamic_redirect phase).
        """
        phase = "http_request_dynamic_redirect"
        body = self._request("GET", f"/zones/{zone_id}/rulesets/phases/{phase}/entrypoint")
        ruleset = body.get("result") or {}
        ruleset_id = ruleset.get("id")
        existing_rules = ruleset.get("rules") or []

        new_rule = {
            "description": f"Redirect {source_domain} to {target}",
            "expression": f'(http.host eq "{source_domain}") or (http.host eq "www.{source_domain}")',
            "action": "redirect",
            "action_parameters": {
                "from_value": {
                    "status_code": 301,
                    "target_url": {
                        "expression": f'concat("{target}", http.request.uri.path)'
                    },
                    "preserve_query_string": True,
                }
            },
        }

        filtered = [
            r for r in existing_rules
            if r.get("description") != new_rule["description"]
        ]
        filtered.append(new_rule)

        if ruleset_id:
            self._request(
                "PUT",
                f"/zones/{zone_id}/rulesets/{ruleset_id}",
                json={"rules": filtered},
            )
        else:
            self._request(
                "POST",
                f"/zones/{zone_id}/rulesets",
                json={
                    "name": "default",
                    "kind": "zone",
                    "phase": phase,
                    "rules": [new_rule],
                },
            )
