import os
from typing import Any

import requests


API_BASE = "https://api.cloudflare.com/client/v4"


class CloudflareClient:
    def __init__(self, token: str | None = None):
        self.token = token or os.environ["CLOUDFLARE_API_TOKEN"]
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        resp = self.session.request(method, f"{API_BASE}{path}", timeout=30, **kwargs)
        resp.raise_for_status()
        body = resp.json()
        if not body.get("success", True):
            raise RuntimeError(f"Cloudflare API error: {body.get('errors')}")
        return body

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
