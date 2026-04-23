from __future__ import annotations

import dns.resolver


# Spamhaus Zen aggregates SBL, XBL, PBL, CSS — the primary list Gmail/Microsoft consult.
# Barracuda and SpamCop are secondary but widely honoured.
DNSBLS = [
    "zen.spamhaus.org",
    "b.barracudacentral.org",
    "bl.spamcop.net",
]

# Spamhaus Zen return codes we should IGNORE:
#   127.0.0.10 = PBL (ISP policy block — normal for VPS IPs)
#   127.0.0.11 = PBL (ISP maintained)
# These are not spam listings; they just mean "this IP range is meant for
# end-user dynamic/VPS use." Proper SPF/DKIM/DMARC overrides PBL concerns.
_SPAMHAUS_PBL = {"127.0.0.10", "127.0.0.11"}


def check_ip(ip: str) -> list[str]:
    """Return DNSBL zones that have this IP listed. Empty list = clean.

    Spamhaus PBL (Policy Block List) results are ignored — VPS IPs are
    commonly in the PBL, which is expected and doesn't indicate spam activity.
    """
    reversed_ip = ".".join(reversed(ip.split(".")))
    listed: list[str] = []
    for zone in DNSBLS:
        try:
            answers = dns.resolver.resolve(f"{reversed_ip}.{zone}", "A", lifetime=5)
            if zone == "zen.spamhaus.org":
                # Filter out PBL-only results
                codes = {rdata.address for rdata in answers}
                if codes - _SPAMHAUS_PBL:
                    # Has non-PBL listings (SBL/XBL/CSS) — genuinely blocklisted
                    listed.append(zone)
                # else: PBL-only, ignore
            else:
                listed.append(zone)
        except Exception:
            pass  # NXDOMAIN / timeout / no answer all = not listed
    return listed
