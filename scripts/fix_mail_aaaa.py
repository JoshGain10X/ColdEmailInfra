#!/usr/bin/env python3
"""Remove broken mail-host AAAA records fleet-wide.

Shards run docker-mailserver, which does NOT serve IMAP/SMTP over IPv6 (Docker
v6 is not plumbed). Provisioning (api/jobs.py) nonetheless wrote an AAAA for
every mail.<sub> host pointing at the Webdock /64 base address (…::), which
nothing listens on. IPv6-capable clients (notably Bison's IMAP poller, and
IPv6 senders) prefer the AAAA, fail to connect, and silently stop ingesting
inbound. Fix = delete those AAAA records so everyone falls back to the working A.

SAFE FILTER: only deletes AAAA records whose name starts with "mail." AND whose
content is a /64-base address (ends with "::"). A properly-configured host AAAA
would not match, so this cannot nuke legit IPv6.

Usage:
  python3 scripts/fix_mail_aaaa.py --zone 10xleadersgroup.com          # one zone
  python3 scripts/fix_mail_aaaa.py --all                               # every zone
  add --apply to actually delete (default is dry-run)
"""
import argparse, json, os, sys, urllib.request, urllib.parse, urllib.error

TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN")
if not TOKEN:
    _envf = os.path.join(os.path.dirname(__file__), "..", ".env")
    if os.path.exists(_envf):
        for _line in open(_envf):
            if _line.startswith("CLOUDFLARE_API_TOKEN="):
                TOKEN = _line.split("=", 1)[1].strip()
if not TOKEN:
    sys.exit("no CLOUDFLARE_API_TOKEN (env var or .env)")

BASE = "https://api.cloudflare.com/client/v4"


def cf(path, method="GET", params=None, body=None):
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Authorization": f"Bearer {TOKEN}",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return json.load(e)


def is_broken_v6(content: str) -> bool:
    # /64 base address: host portion all zeros -> ends in "::" after normalising
    c = content.strip().lower()
    return c.endswith("::")


def list_zones():
    zones, page = [], 1
    while True:
        r = cf("/zones", params={"per_page": "50", "page": str(page)})
        res = r.get("result") or []
        zones += [(z["name"], z["id"]) for z in res]
        info = r.get("result_info") or {}
        if page >= (info.get("total_pages") or 1):
            break
        page += 1
    return zones


def fix_zone(name, zid, apply):
    r = cf(f"/zones/{zid}/dns_records", params={"type": "AAAA", "per_page": "200"})
    targets = [rec for rec in (r.get("result") or [])
               if rec["name"].startswith("mail.") and is_broken_v6(rec["content"])]
    if not targets:
        return 0
    print(f"\n[{name}] {len(targets)} broken mail AAAA record(s):")
    removed = 0
    for rec in targets:
        print(f"  {'DELETE' if apply else 'would delete'} AAAA {rec['name']} -> {rec['content']}")
        if apply:
            d = cf(f"/zones/{zid}/dns_records/{rec['id']}", method="DELETE")
            if d.get("success"):
                removed += 1
            else:
                print(f"    !! failed: {d.get('errors')}")
    return removed if apply else len(targets)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--zone")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()
    if a.all:
        zones = list_zones()
    elif a.zone:
        zid = (cf("/zones", params={"name": a.zone}).get("result") or [{}])[0].get("id")
        if not zid:
            sys.exit(f"zone not found: {a.zone}")
        zones = [(a.zone, zid)]
    else:
        sys.exit("pass --zone <domain> or --all")
    total = 0
    for name, zid in zones:
        total += fix_zone(name, zid, a.apply)
    print(f"\n{'DELETED' if a.apply else 'WOULD DELETE'} {total} record(s) across {len(zones)} zone(s)."
          + ("" if a.apply else "  Re-run with --apply to execute."))


if __name__ == "__main__":
    main()
