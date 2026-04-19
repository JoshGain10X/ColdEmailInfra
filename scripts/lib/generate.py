from __future__ import annotations

import hashlib
import random
import secrets
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parents[2] / "data"

SUBDOMAINS_PER_SHARD = 20
MAILBOXES_PER_SUBDOMAIN = 5
MAILBOXES_PER_SHARD = SUBDOMAINS_PER_SHARD * MAILBOXES_PER_SUBDOMAIN


def _load_lines(name: str) -> list[str]:
    return [line.strip() for line in (DATA_DIR / name).read_text().splitlines() if line.strip()]


def _domain_seed(domain: str) -> int:
    return int.from_bytes(hashlib.sha256(domain.encode()).digest()[:8], "big")


def pick_subdomains(domain: str, seed: int | None = None) -> list[str]:
    """Return SUBDOMAINS_PER_SHARD subdomain words (labels only, no FQDN)."""
    words = _load_lines("subdomain_words.txt")
    rng = random.Random(seed if seed is not None else _domain_seed(domain))
    return rng.sample(words, SUBDOMAINS_PER_SHARD)


def generate_mailboxes(root_domain: str, subdomain_labels: list[str], seed: int | None = None) -> list[dict]:
    """Generate MAILBOXES_PER_SUBDOMAIN mailboxes for each subdomain.

    Each mailbox has first/last drawn randomly from the bundled lists, unique within the shard.
    """
    first_names = _load_lines("british_female_names.txt")
    surnames = _load_lines("british_surnames.txt")
    rng = random.Random(seed if seed is not None else secrets.randbits(64))

    used: set[tuple[str, str]] = set()
    mailboxes: list[dict] = []

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
                "password": secrets.token_urlsafe(16),
            })
            count += 1

    return mailboxes
