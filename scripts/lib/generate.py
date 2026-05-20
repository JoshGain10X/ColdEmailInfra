from __future__ import annotations

import hashlib
import random
import secrets
from pathlib import Path


DATA_DIR = Path(__file__).resolve().parents[2] / "data"

# Legacy default subdomain list. Only used when a client_settings row has
# no subdomain_pool set (shouldn't happen for new onboardings — the
# onboarding flow drafts a per-client pool based on the business).
LEGACY_SUBDOMAINS = [
    "hello", "hi", "contact", "mail", "email",
    "team", "hey", "talk", "chat", "connect",
    "reach", "inbox", "support", "info", "message",
    "ping", "meet", "intro", "new", "join",
]

# Per-shard subdomain count is now random within this range, picked at
# generation time from the client's subdomain_pool. Picking 6-8 (vs the
# old fixed 20) reduces the Smartlead/Instantly fingerprint and varies
# per shard so two shards from the same client never look identical to
# a receiver doing pattern detection across our domains.
DEFAULT_SUBDOMAIN_COUNT_MIN = 6
DEFAULT_SUBDOMAIN_COUNT_MAX = 8

# Per-subdomain mailbox count is now also varied. The historical fixed
# 5/sub × 20 subs = 100 layout is a fingerprint too. Now: 5-15 mailboxes
# per sub, weighted-random distribution that sums exactly to the client's
# target mailbox_count (default 100).
DEFAULT_MAILBOXES_PER_SUBDOMAIN_MIN = 5
DEFAULT_MAILBOXES_PER_SUBDOMAIN_MAX = 15

# Fixed password for every mailbox on every shard. The security tradeoff
# is deliberate: these mailboxes only exist for cold-email warmup/sending
# and a shared password makes manual login, CSV spot-checks, and tool
# imports much simpler. If it ever leaks, destroy+redeploy the affected
# shard to cycle every mailbox at once.
SHARED_MAILBOX_PASSWORD = "C0ldInfr412321!"


def _load_lines(name: str) -> list[str]:
    return [line.strip() for line in (DATA_DIR / name).read_text().splitlines() if line.strip()]


def _domain_seed(domain: str) -> int:
    return int.from_bytes(hashlib.sha256(domain.encode()).digest()[:8], "big")


def pick_subdomains(
    domain: str,
    seed: int | None = None,
    pool: list[str] | None = None,
    count_min: int = DEFAULT_SUBDOMAIN_COUNT_MIN,
    count_max: int = DEFAULT_SUBDOMAIN_COUNT_MAX,
) -> list[str]:
    """Pick a random subset of subdomains from a per-client pool.

    The count itself is random within [count_min, count_max] so two shards
    from the same client never have an identical layout — kills the
    20-subdomain Smartlead/Instantly fingerprint. Without a pool, falls
    back to the legacy list (with the new variable count still applied).
    """
    rng = random.Random(seed if seed is not None else secrets.randbits(64))
    candidates = list(pool) if pool else list(LEGACY_SUBDOMAINS)
    n = rng.randint(count_min, count_max)
    n = min(n, len(candidates))
    if n < count_min:
        # Pool is unusually small — return what we have without raising
        return rng.sample(candidates, len(candidates))
    return rng.sample(candidates, n)


def _distribute_mailboxes(
    total: int,
    n_subs: int,
    rng: random.Random,
    min_per: int = DEFAULT_MAILBOXES_PER_SUBDOMAIN_MIN,
    max_per: int = DEFAULT_MAILBOXES_PER_SUBDOMAIN_MAX,
) -> list[int]:
    """Distribute `total` mailboxes across `n_subs` subdomains.

    Each subdomain gets between min_per and max_per (auto-relaxed if
    arithmetic can't satisfy that for the chosen total/n_subs). Uses
    weighted random distribution that sums exactly to `total` after
    integer rounding fixups.
    """
    # Auto-relax bounds if total can't fit
    if n_subs * max_per < total:
        max_per = max(max_per, (total // n_subs) + 5)
    if n_subs * min_per > total:
        min_per = max(1, (total // n_subs) - 5)

    # Random weights → normalize to total → round
    weights = [rng.uniform(0.6, 1.4) for _ in range(n_subs)]
    s = sum(weights)
    raw = [w / s * total for w in weights]
    counts = [max(min_per, min(max_per, round(r))) for r in raw]

    # Fix to hit exact total — nudge random subdomains up/down until sum matches
    safety = 0
    while sum(counts) != total and safety < 1000:
        safety += 1
        diff = total - sum(counts)
        idx = rng.randint(0, n_subs - 1)
        if diff > 0 and counts[idx] < max_per:
            counts[idx] += 1
        elif diff < 0 and counts[idx] > min_per:
            counts[idx] -= 1
    return counts


def generate_mailboxes(
    root_domain: str,
    subdomain_labels: list[str],
    seed: int | None = None,
    local_parts: list[str] | None = None,
    display_first_name: str | None = None,
    display_last_name: str | None = None,
    mailbox_count_total: int = 100,
) -> list[dict]:
    """Generate mailboxes distributed across the given subdomains.

    Per-subdomain mailbox count is now random (5-15) instead of fixed 5,
    summing to mailbox_count_total. This means a shard with 7 subdomains
    might have, say, 14/12/13/15/15/16/15 mailboxes per subdomain rather
    than the old uniform 5/5/5/5/5/5/5/5/5/5/5/5/5/5/5/5/5/5/5/5 layout.
    Pattern detection across our domains gets harder.

    Two persona modes:

    1. **Default (multi-persona)** — first/last drawn randomly from the
       bundled British name lists. Each mailbox is a different fictional
       person.

    2. **Single-persona pool** — when `local_parts` is provided, every
       mailbox on the shard uses the same display name (display_first_name
       + display_last_name) and an email local-part picked from
       `local_parts`. Used for founder-led / one-person clients (e.g.
       ReachOS). Per-subdomain mailbox count is capped at len(local_parts)
       so aliases stay unique within a subdomain.
    """
    rng = random.Random(seed if seed is not None else secrets.randbits(64))
    n_subs = len(subdomain_labels)
    if n_subs == 0:
        raise ValueError("No subdomains provided")

    # In single-persona mode, per-sub count is capped by pool size
    # (can't have more unique aliases per sub than there are pool entries).
    max_per = DEFAULT_MAILBOXES_PER_SUBDOMAIN_MAX
    if local_parts:
        max_per = min(max_per, len(local_parts))

    per_sub_counts = _distribute_mailboxes(
        mailbox_count_total, n_subs, rng, max_per=max_per
    )

    mailboxes: list[dict] = []

    if local_parts:
        if not display_first_name:
            raise ValueError(
                "display_first_name required when local_parts is set "
                "(single-persona mode needs a fixed display name)"
            )
        last_name = display_last_name or ""
        for sub, want in zip(subdomain_labels, per_sub_counts):
            fqdn = f"{sub}.{root_domain}"
            # Pick `want` unique local-parts from the pool for this subdomain.
            chosen = rng.sample(local_parts, want)
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

    # Multi-persona mode
    first_names = _load_lines("british_female_names.txt")
    surnames = _load_lines("british_surnames.txt")
    used: set[tuple[str, str]] = set()

    for sub, want in zip(subdomain_labels, per_sub_counts):
        fqdn = f"{sub}.{root_domain}"
        count = 0
        attempts = 0
        while count < want:
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
