from __future__ import annotations

import dns.resolver


# Spamhaus Zen aggregates SBL, XBL, PBL, CSS — the primary list Gmail/Microsoft consult.
# Barracuda and SpamCop are secondary but widely honoured.
DNSBLS = [
    "zen.spamhaus.org",
    "b.barracudacentral.org",
    "bl.spamcop.net",
]


def check_ip(ip: str) -> list[str]:
    """Return DNSBL zones that have this IP listed. Empty list = clean."""
    reversed_ip = ".".join(reversed(ip.split(".")))
    listed: list[str] = []
    for zone in DNSBLS:
        try:
            dns.resolver.resolve(f"{reversed_ip}.{zone}", "A", lifetime=5)
            listed.append(zone)
        except Exception:
            pass  # NXDOMAIN / timeout / no answer all = not listed
    return listed
