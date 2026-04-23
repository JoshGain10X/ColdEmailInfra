#!/usr/bin/env python3
"""One-off script: proxy subdomain A records, fix SPF, add wildcard redirect rules.

Changes per domain:
1. Subdomain A records: proxied=False -> proxied=True  (mail.* stays unproxied)
2. SPF TXT records: "v=spf1 a mx -all" -> "v=spf1 ip4:<vps_ip> mx -all"
3. Redirect rule: add subdomain wildcard match alongside root domain match
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.cloudflare import CloudflareClient

load_dotenv(override=True)

DOMAINS = [
    {
        "domain": "10xmanagers.co.uk",
        "zone_id": "5a3be81bff7371430cf9c4d7e7038608",
        "vps_ip": "193.181.210.51",
    },
    {
        "domain": "10xmanagersdevelopment.com",
        "zone_id": "b339cc56fc48ace4423f0c1998412d23",
        "vps_ip": "193.181.210.36",
    },
    {
        "domain": "connect10xmanagers.com",
        "zone_id": "74c5cf819d7caabd6c2ba8dafe43df44",
        "vps_ip": "193.181.210.54",
    },
    {
        "domain": "become-a-10xmanager.com",
        "zone_id": "0909b81cd477e1f2a78e2916a5611cf5",
        "vps_ip": "193.181.210.52",
    },
]

SUBDOMAINS = [
    "hello", "hi", "contact", "mail", "email", "team", "hey", "talk",
    "chat", "connect", "reach", "inbox", "support", "info", "message",
    "ping", "meet", "intro", "new", "join",
]

REDIRECT_TARGET = os.environ.get("REDIRECT_TARGET", "https://10xmanagers.com")


def fix_domain(cf: CloudflareClient, domain: str, zone_id: str, vps_ip: str) -> None:
    print(f"\n{'='*60}")
    print(f"Fixing {domain} (zone {zone_id}, VPS {vps_ip})")
    print(f"{'='*60}")

    new_spf = f"v=spf1 ip4:{vps_ip} mx -all"

    for sub in SUBDOMAINS:
        fqdn = f"{sub}.{domain}"

        if sub == "mail":
            # mail.domain stays unproxied — it handles actual mail traffic
            print(f"  [skip] {fqdn} — mail subdomain, keeping unproxied")
            continue

        # 1. Proxy the subdomain A record
        print(f"  [proxy] {fqdn} -> proxied=True")
        cf.upsert_record(zone_id, "A", fqdn, vps_ip, proxied=True)

        # 2. Update SPF to use ip4 instead of 'a'
        print(f"  [spf]   {fqdn} -> {new_spf}")
        cf.upsert_record(zone_id, "TXT", fqdn, new_spf)

    # 3. Update redirect rule to also match subdomains
    print(f"\n  [redirect] Updating redirect rule to cover *.{domain}")
    _ensure_wildcard_redirect(cf, zone_id, domain, REDIRECT_TARGET)
    print(f"  Done with {domain}")


def _ensure_wildcard_redirect(cf: CloudflareClient, zone_id: str, domain: str, target: str) -> None:
    """Create/update a redirect rule that covers root + all subdomains."""
    phase = "http_request_dynamic_redirect"

    resp = cf.session.get(
        f"https://api.cloudflare.com/client/v4/zones/{zone_id}/rulesets/phases/{phase}/entrypoint",
        timeout=30,
    )
    if resp.status_code == 404:
        ruleset_id = None
        existing_rules = []
    elif resp.status_code >= 400:
        body = resp.json()
        raise RuntimeError(f"Cloudflare API {resp.status_code}: {body.get('errors', body)}")
    else:
        body = resp.json()
        ruleset = body.get("result") or {}
        ruleset_id = ruleset.get("id")
        existing_rules = ruleset.get("rules") or []

    # New rule that matches root, www, and all subdomains
    new_rule = {
        "description": f"Redirect {domain} to {target}",
        "expression": (
            f'(http.host eq "{domain}") or '
            f'(http.host eq "www.{domain}") or '
            f'(http.host contains ".{domain}")'
        ),
        "action": "redirect",
        "action_parameters": {
            "from_value": {
                "status_code": 301,
                "target_url": {
                    "expression": f'concat("{target}", http.request.uri.path)',
                },
                "preserve_query_string": True,
            }
        },
    }

    # Remove old rule with same description, add new one
    filtered = [r for r in existing_rules if r.get("description") != new_rule["description"]]
    filtered.append(new_rule)

    if ruleset_id:
        cf._request("PUT", f"/zones/{zone_id}/rulesets/{ruleset_id}", json={"rules": filtered})
    else:
        cf._request("POST", f"/zones/{zone_id}/rulesets", json={
            "name": "default",
            "kind": "zone",
            "phase": phase,
            "rules": [new_rule],
        })


def main():
    cf = CloudflareClient()
    for d in DOMAINS:
        fix_domain(cf, d["domain"], d["zone_id"], d["vps_ip"])
    print("\n\nAll domains updated. Subdomains will now redirect via Cloudflare.")


if __name__ == "__main__":
    main()
