from __future__ import annotations

import hashlib
import random
import secrets
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parents[2] / "data"

SUBDOMAINS = [
    "hello", "hi", "contact", "mail", "email",
    "team", "hey", "talk", "chat", "connect",
    "reach", "inbox", "support", "info", "message",
    "ping", "meet", "intro", "new", "join",
]
SUBDOMAINS_PER_SHARD = len(SUBDOMAINS)
MAILBOXES_PER_SUBDOMAIN = 5
MAILBOXES_PER_SHARD = SUBDOMAINS_PER_SHARD * MAILBOXES_PER_SUBDOMAIN

# Fixed password for every mailbox on every shard. The security tradeoff is
# deliberate: these mailboxes only exist for cold-email warmup/sending and
# a shared password makes manual login, CSV spot-checks, and tool imports
# much simpler. If it ever leaks, destroy+redeploy the affected shard to
# cycle every mailbox at once.
SHARED_MAILBOX_PASSWORD = "C0ldInfr412321!"


def _load_lines(name: str) -> list[str]:
    return [line.strip() for line in (DATA_DIR / name).read_text().splitlines() if line.strip()]


def _domain_seed(domain: str) -> int:
    return int.from_bytes(hashlib.sha256(domain.encode()).digest()[:8], "big")


def pick_subdomains(domain: str, seed: int | None = None) -> list[str]:
    """Return the fixed 20-subdomain set used on every shard."""
    return list(SUBDOMAINS)


def generate_mailboxes(
    root_domain: str,
    subdomain_labels: list[str],
    seed: int | None = None,
    local_parts: list[str] | None = None,
    display_first_name: str | None = None,
    display_last_name: str | None = None,
) -> list[dict]:
    """Generate MAILBOXES_PER_SUBDOMAIN mailboxes for each subdomain.

    Two modes:

    1. **Default (multi-persona)** — first/last drawn randomly from the bundled
       British name lists. Each mailbox is a different fictional person.
       Used for most agency clients.

    2. **Single-persona pool** — when `local_parts` is provided, every mailbox
       on the shard uses the same display name (display_first_name +
       display_last_name) and an email local-part picked from `local_parts`.
       Used for founder-led / one-person clients (e.g. ReachOS, where every
       mailbox is "Josh Gain" under different local-part aliases per
       subdomain). MAILBOXES_PER_SUBDOMAIN unique local-parts are picked
       per subdomain so the same alias never appears twice on one subdomain;
       across subdomains, the local-parts may repeat (different FQDN).
    """
    rng = random.Random(seed if seed is not None else secrets.randbits(64))
    mailboxes: list[dict] = []

    if local_parts:
        if not display_first_name:
            raise ValueError(
                "display_first_name required when local_parts is set "
                "(single-persona mode needs a fixed display name)"
            )
        if len(local_parts) < MAILBOXES_PER_SUBDOMAIN:
            raise ValueError(
                f"Need at least {MAILBOXES_PER_SUBDOMAIN} local_parts for "
                f"unique aliases per subdomain; got {len(local_parts)}"
            )

        last_name = display_last_name or ""
        for sub in subdomain_labels:
            fqdn = f"{sub}.{root_domain}"
            # Pick MAILBOXES_PER_SUBDOMAIN unique local-parts for this subdomain.
            # Different subdomains get different selections, so across the shard
            # all 10 pool entries show up multiple times — but every (fqdn,
            # local_part) pair is unique by construction.
            chosen = rng.sample(local_parts, MAILBOXES_PER_SUBDOMAIN)
            for local_part in chosen:
                mailboxes.append({
                    "first_name": display_first_name,
                    "last_name": last_name,
                    "subdomain": sub,
                    "fqdn": fqdn,
                    "local_part": local_part,
                    "email": f"{local_part}@{fqdn}",
                    "password": SHARED_MAILBOX_PASSWORD,
                })
        return mailboxes

    # Multi-persona mode (existing behaviour)
    first_names = _load_lines("british_female_names.txt")
    surnames = _load_lines("british_surnames.txt")
    used: set[tuple[str, str]] = set()

    for sub in subdomain_labels:
        fqdn = f"{sub}.{root_domain}"
        count = 0
        attempts = 0
        while count < MAILBOXES_PER_SUBDOMAIN:
            attempts += 1
            first = rng.choice(first_names)
            last = rng.choice(surnames)
            local_part = f"{first.lower()}.{last.lower()}"
            if (fqdn, local_part) in used:
                if attempts > 500:
                    raise RuntimeError(f"Could not generate unique mailboxes for {fqdn}")
                continue
            used.add((fqdn, local_part))
            mailboxes.append({
                "first_name": first,
                "last_name": last,
                "subdomain": sub,
                "fqdn": fqdn,
                "local_part": local_part,
                "email": f"{local_part}@{fqdn}",
                "password": SHARED_MAILBOX_PASSWORD,
            })
            count += 1

    return mailboxes
