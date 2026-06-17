"""Composite deliverability scoring + verdict classification.

Pure functions. No I/O. Easy to unit-test and retune.

Thresholds anchored at industry-standard 2% bounce / 0.1% complaint lines
(Litemail 2026, Instantly Benchmark Report 2026, Smartlead, Mailgun).
See reference/scoring-rubric.md for sources and rationale.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Second-level public suffixes we encounter. Conservative list; extend if needed.
_PUBLIC_SUFFIX_2 = {
    "co.uk", "co.nz", "com.au", "co.za", "com.br", "co.jp",
    "ac.uk", "gov.uk", "org.uk", "net.au", "co.in", "com.mx",
}


def root_domain(hostname: str) -> str:
    """Return the registrable root domain for a hostname.

    `intro.emailreachos.com` -> `emailreachos.com`
    `news.example.co.uk`     -> `example.co.uk`
    Empty input or single-label hosts return as-is.
    """
    if not hostname:
        return ""
    parts = hostname.lower().strip(".").split(".")
    if len(parts) < 2:
        return hostname.lower()
    last_two = ".".join(parts[-2:])
    if last_two in _PUBLIC_SUFFIX_2 and len(parts) >= 3:
        return ".".join(parts[-3:])
    return last_two


MIN_VOLUME_FOR_VERDICT = 100   # in-window sends required before we score a root domain

W_BOUNCE_RATE = 15.0          # points per 1% bounce rate. 2% bounce -> 30 pts (PAUSE band).
W_REP_NOTIF_RATE = 6.0        # points per 1% rep-class DSN rate. 10% -> 60 pts (RETIRE).
W_REPUTATION_PCT = 0.3        # points per 1% of classified bounces being reputation. 100% -> 30 pts.
W_REPLY_DECAY = 0.3           # points per 1% week-over-week reply decay. 30% decay -> 9 pts.
W_AUTH_PCT = 0.1              # points per 1% auth-failure share of bounces. 100% -> 10 pts.

RETIRE_THRESHOLD = 60.0
PAUSE_THRESHOLD = 40.0
WARM_THRESHOLD = 20.0

BOUNCE_RATE_RETIRE = 0.02
BOUNCE_RATE_PAUSE = 0.015
REPUTATION_PCT_RETIRE = 0.20
REP_NOTIF_RATE_RETIRE = 0.05   # >5% of sends produce a reputation-class DSN → retire

NEW_MAILBOX_DAYS = 14


@dataclass
class DomainStats:
    """Per-root-domain stats over the analysis window.

    `domain` is the registrable root (e.g. `emailreachos.com`).
    Subdomains roll up into this; `subdomain_count` and `mailbox_count`
    capture the underlying spread. All counters are over the window.
    """
    domain: str                    # root domain
    mailbox_count: int             # how many Bison sender mailboxes feed this root
    subdomain_count: int           # how many distinct subdomains under this root
    emails_sent: int               # in-window sends (Bison campaign-events stats)
    replies: int                   # in-window replies
    bounces: int                   # in-window Bison-reported bounces (SMTP-time)
    bounces_hard: int              # bounce-notification classifications, in-window
    bounces_reputation: int
    bounces_soft: int
    bounces_auth: int
    bounces_unknown: int
    bounces_warmup: int = 0  # excluded from verdict math; tracked for visibility
    domain_age_days: int | None = None   # from infra_domains.registrar_created_at
    history_days: int = 0
    reply_rate_prior_7d: float | None = None
    reply_rate_last_7d: float | None = None
    subdomains: list[str] = field(default_factory=list)  # for drilldown

    @property
    def bounces_classified(self) -> int:
        """Total bounce notifications we classified. Used as the denominator
        for rep/auth/soft percentage breakdowns. Often differs from `bounces`
        because multiple bounce notifications can arrive per bounced send."""
        return (
            self.bounces_hard
            + self.bounces_reputation
            + self.bounces_soft
            + self.bounces_auth
            + self.bounces_unknown
        )


@dataclass
class Verdict:
    domain: str
    label: str              # RETIRE | PAUSE | WARM | HEALTHY | INSUFFICIENT
    score: float
    bounce_rate: float
    rep_notif_rate: float
    reply_rate: float
    reputation_bounce_pct: float
    auth_bounce_pct: float
    reply_rate_decay: float | None
    dominant_signal: str
    components: dict
    insufficient_volume: bool = False


def _safe_div(num: float, denom: float) -> float:
    return (num / denom) if denom else 0.0


def score_domain(stats: DomainStats) -> Verdict:
    # Volume gate first. Below this floor, we can't separate signal from noise.
    if stats.emails_sent < MIN_VOLUME_FOR_VERDICT:
        return Verdict(
            domain=stats.domain,
            label="INSUFFICIENT",
            score=0.0,
            bounce_rate=_safe_div(stats.bounces, stats.emails_sent),
            rep_notif_rate=_safe_div(stats.bounces_reputation, stats.emails_sent),
            reply_rate=_safe_div(stats.replies, stats.emails_sent),
            reputation_bounce_pct=_safe_div(stats.bounces_reputation, stats.bounces_classified),
            auth_bounce_pct=_safe_div(stats.bounces_auth, stats.bounces_classified),
            reply_rate_decay=None,
            dominant_signal=(
                f"only {stats.emails_sent} sends in window "
                f"(<{MIN_VOLUME_FOR_VERDICT} threshold) - re-check after more volume"
            ),
            components={},
            insufficient_volume=True,
        )

    bounce_rate = _safe_div(stats.bounces, stats.emails_sent)
    reply_rate = _safe_div(stats.replies, stats.emails_sent)
    rep_notif_rate = _safe_div(stats.bounces_reputation, stats.emails_sent)
    auth_notif_rate = _safe_div(stats.bounces_auth, stats.emails_sent)
    # share-of-bounce-mix percentages, denom = classified notifications (not Bison's bounced_count)
    denom = stats.bounces_classified
    reputation_pct = _safe_div(stats.bounces_reputation, denom)
    auth_pct = _safe_div(stats.bounces_auth, denom)

    decay = None
    if (
        stats.reply_rate_prior_7d is not None
        and stats.reply_rate_last_7d is not None
        and stats.reply_rate_prior_7d > 0
    ):
        decay = (stats.reply_rate_last_7d - stats.reply_rate_prior_7d) / stats.reply_rate_prior_7d
    decay_component = max(0.0, -(decay or 0.0)) * 100 * W_REPLY_DECAY

    # All percentage inputs are fractions (0-1). Express as percentage (×100)
    # then multiply by weight, so e.g. 2% bounce × weight 15 = 30 points.
    components = {
        "bounce_rate": bounce_rate * 100 * W_BOUNCE_RATE,
        "rep_notif_rate": rep_notif_rate * 100 * W_REP_NOTIF_RATE,
        "reputation_pct": reputation_pct * 100 * W_REPUTATION_PCT,
        "reply_decay": decay_component,
        "auth_pct": auth_pct * 100 * W_AUTH_PCT,
    }
    score = sum(components.values())

    if rep_notif_rate >= REP_NOTIF_RATE_RETIRE:
        label = "RETIRE"
    elif bounce_rate > BOUNCE_RATE_RETIRE and reputation_pct > REPUTATION_PCT_RETIRE:
        label = "RETIRE"
    elif score >= RETIRE_THRESHOLD:
        label = "RETIRE"
    elif score >= PAUSE_THRESHOLD or BOUNCE_RATE_PAUSE <= bounce_rate <= BOUNCE_RATE_RETIRE:
        label = "PAUSE"
    elif score >= WARM_THRESHOLD:
        label = "WARM"
    elif 0 < stats.history_days < NEW_MAILBOX_DAYS and stats.bounces_reputation > 0:
        label = "WARM"
    else:
        label = "HEALTHY"

    if rep_notif_rate >= REP_NOTIF_RATE_RETIRE:
        signal = (
            f"{rep_notif_rate:.1%} of sends generate reputation-class DSNs "
            f"({stats.bounces_reputation} of {stats.emails_sent}) - domain being filtered"
        )
    elif stats.bounces_reputation > 0 and reputation_pct >= 0.50:
        signal = (
            f"{stats.bounces_reputation}/{denom} bounce notifications are reputation-class "
            f"({reputation_pct:.0%})"
        )
    elif bounce_rate >= BOUNCE_RATE_RETIRE:
        signal = f"bounce rate {bounce_rate:.1%} above 2% red line"
    elif bounce_rate >= BOUNCE_RATE_PAUSE:
        signal = f"bounce rate {bounce_rate:.1%} in pause-warning band (1.5-2%)"
    elif decay is not None and decay <= -0.30:
        signal = (
            f"reply rate down {abs(decay):.0%} week-over-week from {stats.reply_rate_prior_7d:.1%} baseline"
        )
    elif auth_pct >= 0.2:
        signal = f"{auth_pct:.0%} of bounces are SPF/DKIM/DMARC failures - check DNS"
    elif stats.bounces == 0 and stats.bounces_classified == 0 and stats.emails_sent > 100:
        signal = "clean - no bounces or DSNs"
    elif stats.bounces_reputation > 0:
        signal = (
            f"{stats.bounces_reputation} reputation-class DSN(s) over {stats.emails_sent} sends "
            f"({rep_notif_rate:.1%})"
        )
    else:
        signal = f"{bounce_rate:.1%} bounce rate, no dominant failure mode"

    return Verdict(
        domain=stats.domain,
        label=label,
        score=round(score, 1),
        bounce_rate=bounce_rate,
        rep_notif_rate=rep_notif_rate,
        reply_rate=reply_rate,
        reputation_bounce_pct=reputation_pct,
        auth_bounce_pct=auth_pct,
        reply_rate_decay=decay,
        dominant_signal=signal,
        components=components,
    )


def aggregate_to_root(rows: list[dict]) -> dict[str, DomainStats]:
    """Group per-mailbox rows into per-root-domain stats.

    Each input row corresponds to one Bison sender mailbox. Subdomains
    roll up to their registrable root because reputation is a root-level
    property (SPF, DMARC alignment, ISP IP-and-domain reputation).

    Rows expect keys: domain (the subdomain), emails_sent, replies, bounces,
    bounces_hard, bounces_reputation, bounces_soft, bounces_auth, bounces_unknown.
    """
    out: dict[str, DomainStats] = {}
    subs_seen: dict[str, set[str]] = {}
    for r in rows:
        sub = r["domain"]
        root = root_domain(sub)
        if root not in out:
            out[root] = DomainStats(
                domain=root,
                mailbox_count=0,
                subdomain_count=0,
                emails_sent=0,
                replies=0,
                bounces=0,
                bounces_hard=0,
                bounces_reputation=0,
                bounces_soft=0,
                bounces_auth=0,
                bounces_unknown=0,
                bounces_warmup=0,
            )
            subs_seen[root] = set()
        agg = out[root]
        agg.mailbox_count += 1
        agg.emails_sent += r.get("emails_sent") or 0
        agg.replies += r.get("replies") or 0
        agg.bounces += r.get("bounces") or 0
        agg.bounces_hard += r.get("bounces_hard") or 0
        agg.bounces_reputation += r.get("bounces_reputation") or 0
        agg.bounces_soft += r.get("bounces_soft") or 0
        agg.bounces_auth += r.get("bounces_auth") or 0
        agg.bounces_unknown += r.get("bounces_unknown") or 0
        agg.bounces_warmup += r.get("bounces_warmup") or 0
        if sub and sub != root:
            subs_seen[root].add(sub)
    for root, agg in out.items():
        agg.subdomain_count = len(subs_seen[root])
        agg.subdomains = sorted(subs_seen[root])
    return out


# Backwards-compatible alias so older importers still resolve.
aggregate_to_domain = aggregate_to_root


if __name__ == "__main__":
    cases = [
        DomainStats("healthy.com", 5, 5000, 150, 10, 8, 1, 1, 0, 0),
        DomainStats("warming.com", 3, 600, 12, 8, 4, 2, 1, 1, 0, history_days=7),
        DomainStats("pausing.com", 4, 2000, 30, 36, 20, 8, 5, 3, 0),
        DomainStats("retire-me.com", 6, 3000, 25, 90, 20, 50, 10, 8, 2),
    ]
    for c in cases:
        v = score_domain(c)
        print(f"{v.domain:20s} {v.label:8s} score={v.score:5.1f}  signal={v.dominant_signal}")
