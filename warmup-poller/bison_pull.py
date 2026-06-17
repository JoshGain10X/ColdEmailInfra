"""Pull per-mailbox deliverability data from Email Bison.

Switches workspace using the SuperAdmin key, walks /api/sender-emails for
per-mailbox counters, then walks /api/replies?folder=bounced for each mailbox
that has any bounces to classify them.

Env vars (sourced from ~/.claude/skills/blitz-leads/.env):
  EB_INSTANCE_URL       e.g. https://send.spamproofed.com
  EB_SUPERADMIN_KEY     bearer token

Bison API quirks accounted for:
  - /api/sender-emails and /api/replies ignore per_page; always 15/page.
  - Workspace switch via POST /api/workspaces/switch-workspace {"team_id": N}.
"""

from __future__ import annotations

import os
import re
import sys
import json
import time
import datetime as dt
from pathlib import Path
from typing import Iterator

import requests

# Local import for root_domain. Path-safe whether run as a script or imported.
sys.path.insert(0, str(Path(__file__).parent))
from score import root_domain  # noqa: E402

ENV_PATH = Path.home() / ".claude/skills/blitz-leads/.env"

# Only mailboxes carrying this Bison tag are tracked. Other mailboxes (Google
# defaults, warmup-only pools, third-party providers, etc.) are out of scope.
# Tag IDs differ per workspace, so we filter by exact tag NAME client-side
# after fetching /api/sender-emails.
REQUIRED_TAG = "Custom SMTP"


def _load_env() -> dict:
    # When deployed inside a Docker container (e.g. coldemail-warmup-poller on
    # infraapi1), ENV_PATH won't exist - we get config via docker --env-file
    # instead. Seed `out` from os.environ first so the same code works in both
    # dev (file-backed) and container (env-backed) modes. File entries override
    # because that's the dev workflow: edit the file, re-run, expect the new value.
    out: dict[str, str] = dict(os.environ)
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


ENV = _load_env()
BASE_URL = ENV.get("EB_INSTANCE_URL", "").rstrip("/")
SUPERADMIN_KEY = ENV.get("EB_SUPERADMIN_KEY", "")


def _headers() -> dict:
    if not SUPERADMIN_KEY:
        raise RuntimeError("EB_SUPERADMIN_KEY not set in ~/.claude/skills/blitz-leads/.env")
    return {
        "Authorization": f"Bearer {SUPERADMIN_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _request_with_retry(method: str, url: str, *, max_attempts: int = 5, **kw) -> requests.Response:
    """HTTP call that retries on 5xx + transient network errors with exponential backoff.

    Bison's `/api/replies?folder=bounced` endpoint intermittently returns 502
    during long paginations. Without retry, the whole snapshot run aborts on
    the first hiccup. Backoff: 2s, 4s, 8s, 16s.
    """
    last_exc: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            r = requests.request(method, url, **kw)
            if r.status_code < 500:
                # 4xx still raises immediately. Only retry 5xx.
                r.raise_for_status()
                return r
            print(
                f"  HTTP {r.status_code} on {url.rsplit('?', 1)[0]} (attempt {attempt}/{max_attempts}); retrying",
                file=sys.stderr,
            )
            last_exc = requests.HTTPError(f"{r.status_code} for {url}", response=r)
        except (requests.ConnectionError, requests.Timeout) as e:
            print(
                f"  network error on {url.rsplit('?', 1)[0]} (attempt {attempt}/{max_attempts}): {e}",
                file=sys.stderr,
            )
            last_exc = e
        if attempt < max_attempts:
            time.sleep(2 ** attempt)
    assert last_exc is not None
    raise last_exc


def switch_workspace(team_id: int) -> dict:
    """Switch the SuperAdmin session to a specific workspace."""
    r = _request_with_retry(
        "POST",
        f"{BASE_URL}/api/workspaces/switch-workspace",
        headers=_headers(),
        json={"team_id": team_id},
        timeout=30,
    )
    return r.json()


def list_workspaces() -> list[dict]:
    """List all workspaces visible to the SuperAdmin key."""
    r = _request_with_retry("GET", f"{BASE_URL}/api/workspaces", headers=_headers(), timeout=30)
    body = r.json()
    return body.get("data", body if isinstance(body, list) else [])


def _paginate(path: str, params: dict | None = None) -> Iterator[dict]:
    page = 1
    while True:
        q = dict(params or {})
        q["page"] = page
        r = _request_with_retry("GET", f"{BASE_URL}{path}", headers=_headers(), params=q, timeout=60)
        body = r.json()
        rows = body.get("data", [])
        if not rows:
            return
        for row in rows:
            yield row
        meta = body.get("meta") or {}
        last_page = meta.get("last_page")
        if page % 10 == 0:
            print(f"  {path}: page {page}/{last_page or '?'}", file=sys.stderr)
        if last_page is None:
            # No meta — stop if we got fewer than what looks like a full page.
            if len(rows) < 15:
                return
        elif page >= last_page:
            return
        page += 1
        time.sleep(0.05)


def list_sender_emails() -> list[dict]:
    """Pull all mailboxes for the active workspace with cumulative counters."""
    return list(_paginate("/api/sender-emails"))


def _has_required_tag(sender: dict, tag_name: str = REQUIRED_TAG) -> bool:
    """True iff this sender has the required tag attached.

    Bison returns tags as `[{id, name, default}, ...]`. Tag IDs differ per
    workspace, so we match by name. Comparison is case-insensitive and
    whitespace-insensitive to be tolerant of typos like 'CustomSMTP' vs
    'Custom SMTP'.
    """
    target = tag_name.replace(" ", "").lower()
    for tag in sender.get("tags") or []:
        if not isinstance(tag, dict):
            continue
        name = (tag.get("name") or "").replace(" ", "").lower()
        if name == target:
            return True
    return False


def list_all_bounced_replies(max_pages: int = 3000) -> list[dict]:
    """Walk every bounced reply in the active workspace in one paginated sweep.
    Faster than N×per-sender walks. Each row carries sender_email_id."""
    out = []
    page = 1
    while page <= max_pages:
        r = _request_with_retry(
            "GET",
            f"{BASE_URL}/api/replies",
            headers=_headers(),
            params={"folder": "bounced", "page": page},
            timeout=60,
        )
        body = r.json()
        rows = body.get("data", [])
        if not rows:
            break
        out.extend(rows)
        meta = body.get("meta") or {}
        last_page = meta.get("last_page")
        if page % 10 == 0:
            print(f"  bounced replies: page {page}/{last_page or '?'} ({len(out)} so far)", file=sys.stderr)
        if last_page is not None and page >= last_page:
            break
        if last_page is None and len(rows) < 15:
            break
        page += 1
        time.sleep(0.05)
    return out


# Bounce classifier — order matters. First match wins, except 'auth' is
# evaluated first so DKIM/SPF copy doesn't get bucketed as reputation.
_PATTERNS = [
    (
        "auth",
        re.compile(
            r"(5\.7\.26|\bdkim\b|\bspf\b|\bdmarc\b|authentication\s+fail|signature\s+invalid)",
            re.IGNORECASE,
        ),
    ),
    (
        "reputation",
        re.compile(
            r"(5\.7\.1\b|550[\s\S]{0,80}(spam|blacklist|blocklist|policy|reputation|rejected|refused|denied|abuse)"
            r"|\b552\b|\b553\b|\b554\b|spamhaus|spamcop|barracuda|sorbs|surbl|"
            r"listed[\s\S]{0,30}block|message\s+(rejected|refused)|content\s+rejected|"
            r"recipient\s+policy|sending\s+ip\s+is\s+listed)",
            re.IGNORECASE,
        ),
    ),
    (
        "hard",
        re.compile(
            r"(5\.1\.[12]\b|user\s+unknown|mailbox[\s\S]{0,20}(disabled|not\s+exist|unavailable|no\s+such)|"
            r"no\s+such\s+user|address\s+(rejected|unknown)|recipient\s+address\s+rejected|"
            r"does\s+not\s+exist|address\s+not\s+found)",
            re.IGNORECASE,
        ),
    ),
    (
        "soft",
        re.compile(
            r"(\b4\d\d\b|\b4\.\d\.\d\b|try\s+again|temporary(\s+failure)?|throttl|"
            r"mailbox\s+full|over\s+quota|rate[\s-]?limit|deferred|greylist)",
            re.IGNORECASE,
        ),
    ),
]


# Anchors marking the start of quoted-original / attached-message sections
# that we want to strip before running the classifier. Anything after one
# of these markers is upstream content, not the bounce diagnostic.
_QUOTED_TAILS = re.compile(
    r"(-{3,}\s*Original message"
    r"|-{3,}\s*Forwarded message"
    r"|Begin forwarded message"
    r"|Content-Type:\s+message/rfc822"
    r"|^>\s)",
    re.IGNORECASE | re.MULTILINE,
)

# Postfix-style: `<recipient@domain>: host X.Y.Z[ip] said: NNN ...`
_POSTFIX_SAID = re.compile(
    r"said:\s*(?P<body>.+?)(?:\n\s*\n|\Z)",
    re.IGNORECASE | re.DOTALL,
)

# RFC 3464 DSN: `Diagnostic-Code: smtp; NNN ...` possibly with continuation lines.
_DIAGNOSTIC_CODE = re.compile(
    r"Diagnostic-Code:\s*smtp;\s*(?P<body>.+?)(?:\n[A-Z][a-zA-Z-]+:|\Z)",
    re.IGNORECASE | re.DOTALL,
)


def extract_diagnostic_section(text: str) -> str:
    """Strip quoted-original sections and prefer the SMTP diagnostic block.

    The full reply body includes Postfix preamble, sometimes a Diagnostic-Code
    block, then the attached original message - which often contains the
    outbound campaign copy. Classifying the whole body causes false positives
    when copy mentions words like "spam" or "blocked".

    Returns the smallest slice we trust: Diagnostic-Code body if present,
    else the `said: ...` body, else the pre-quote portion of the message.
    """
    if not text:
        return text

    # 1. Truncate at quoted-original markers
    m = _QUOTED_TAILS.search(text)
    head = text[: m.start()] if m else text

    # 2. Prefer Diagnostic-Code block
    diag = _DIAGNOSTIC_CODE.search(head)
    if diag:
        return diag.group("body")

    # 3. Else Postfix `said:` block
    said = _POSTFIX_SAID.search(head)
    if said:
        return said.group("body")

    # 4. Fall back to the pre-quote portion of the body
    return head


# Warmup-tag fingerprint. Mail (including bounce NDRs derived from warmup
# sends) carrying this string in subject or body is peer-warmup chatter and
# must not count toward deliverability stats. Set in instantly_admin.py as
# WARMUP_FILTER_TAG; duplicated here as a constant to keep bison_pull.py
# importable without pulling in the Instantly module.
WARMUP_TAG_FINGERPRINT = "sointerested"


def is_warmup_mail(text: str) -> bool:
    """True if this reply/NDR carries the Instantly warmup tag."""
    if not text:
        return False
    return WARMUP_TAG_FINGERPRINT in text.lower()


def classify_bounce(text: str) -> str:
    """Return one of: auth, reputation, hard, soft, warmup, unknown.

    'warmup' bucket added: any body/subject containing the warmup fingerprint
    is filed there instead of being scored against the rep/auth/etc rubric.
    Caller is expected to exclude warmup-bucket counts from verdict math.

    Slices to the diagnostic section first to avoid regex-matching on
    quoted outbound copy.
    """
    if not text:
        return "unknown"
    # Warmup gate runs BEFORE diagnostic slicing - the tag may appear anywhere
    # in the body or subject and we want it caught regardless of position.
    if is_warmup_mail(text):
        return "warmup"
    sliced = extract_diagnostic_section(text)
    if not sliced.strip():
        return "unknown"
    for label, pat in _PATTERNS:
        if pat.search(sliced):
            return label
    return "unknown"


def classify_replies(replies: list[dict]) -> dict:
    """Bucket counts: returns {hard, reputation, soft, auth, unknown, warmup}.

    `warmup` is the count of bounce notifications whose body/subject contain
    the Instantly warmup tag. These come from peer-warmup chatter or from
    NDRs of warmup sends - either way, they're NOT real deliverability
    signal about cold sending. Excluded from verdict math.
    """
    counts = {"hard": 0, "reputation": 0, "soft": 0, "auth": 0, "unknown": 0, "warmup": 0}
    for r in replies:
        text = (r.get("text_body") or "") + "\n" + (r.get("subject") or "")
        if not text.strip():
            text = r.get("html_body") or ""
        counts[classify_bounce(text)] += 1
    return counts


def pull_workspace(team_id: int, workspace_name: str = "") -> dict:
    """Returns a payload ready for snapshotting/scoring:
    {
        "workspace_id": int, "workspace_name": str,
        "mailboxes": [
            {sender_email_id, email, domain, sender_status, warmup_enabled,
             emails_sent, opens, replies, unique_replies, interested,
             unsubscribed, bounces, bounces_hard, bounces_reputation,
             bounces_soft, bounces_auth, bounces_unknown}, ...
        ]
    }
    """
    print(f"[1/3] switching to workspace {team_id}", file=sys.stderr)
    switch_workspace(team_id)
    print(f"[2/3] pulling sender-emails (mailboxes)", file=sys.stderr)
    all_senders = list_sender_emails()
    senders = [s for s in all_senders if _has_required_tag(s)]
    print(
        f"      {len(senders)}/{len(all_senders)} mailboxes tagged '{REQUIRED_TAG}' "
        f"(filtered {len(all_senders) - len(senders)} untagged)",
        file=sys.stderr,
    )
    any_bounces = any((s.get("bounced_count") or 0) > 0 for s in senders)
    bounced_replies: list[dict] = []
    if any_bounces:
        print(f"[3/3] walking bounced replies (workspace-wide)", file=sys.stderr)
        bounced_replies = list_all_bounced_replies()
        print(f"      {len(bounced_replies)} bounced replies retrieved", file=sys.stderr)
    else:
        print(f"[3/3] no bounces in workspace, skipping reply walk", file=sys.stderr)

    by_sender: dict[int, list[dict]] = {}
    for r in bounced_replies:
        sid = r.get("sender_email_id")
        if sid is None:
            continue
        by_sender.setdefault(sid, []).append(r)

    mailboxes = []
    for s in senders:
        replies = by_sender.get(s["id"], [])
        buckets = classify_replies(replies)
        email = s.get("email", "")
        domain = email.split("@", 1)[1].lower() if "@" in email else ""
        mailboxes.append({
            "sender_email_id": s["id"],
            "email": email,
            "domain": domain,
            "sender_status": s.get("status"),
            "warmup_enabled": s.get("warmup_enabled"),
            "emails_sent": s.get("emails_sent_count") or 0,
            "opens": s.get("total_opened_count") or 0,
            "replies": s.get("total_replied_count") or 0,
            "unique_replies": s.get("unique_replied_count") or 0,
            "interested": s.get("interested_leads_count") or 0,
            "unsubscribed": s.get("unsubscribed_count") or 0,
            "bounces": s.get("bounced_count") or 0,
            "bounces_hard": buckets["hard"],
            "bounces_reputation": buckets["reputation"],
            "bounces_soft": buckets["soft"],
            "bounces_auth": buckets["auth"],
            "bounces_unknown": buckets["unknown"],
            "bounces_warmup": buckets.get("warmup", 0),
            "bounced_replies_scanned": len(replies),
        })
    return {
        "workspace_id": team_id,
        "workspace_name": workspace_name,
        "mailboxes": mailboxes,
    }


def get_window_stats(sender_email_ids: list[int], start_date: str, end_date: str) -> dict:
    """In-window send/reply/bounce totals for a set of senders.

    Uses /api/campaign-events/stats which returns chart-style data: a list of
    {label, dates: [[YYYY-MM-DD, count]]} series. We sum each series within the
    window. The response is aggregated across the supplied sender_email_ids,
    so this gives one root-level total per call.
    """
    params: dict = {"start_date": start_date, "end_date": end_date}
    if sender_email_ids:
        # Laravel-style array param. requests serialises list values as repeated keys.
        params["sender_email_ids[]"] = [str(x) for x in sender_email_ids]
    r = _request_with_retry(
        "GET",
        f"{BASE_URL}/api/campaign-events/stats",
        headers=_headers(),
        params=params,
        timeout=60,
    )
    body = r.json()
    totals = {"sent": 0, "replied": 0, "bounced": 0, "opens": 0, "interested": 0}
    label_map = {
        "sent": "sent",
        "replied": "replied",
        "bounced": "bounced",
        "interested": "interested",
        "total opens": "opens",
        "unique opens": None,        # ignore to avoid double counting
        "unsubscribed": None,
    }
    for series in body.get("data", []):
        label = (series.get("label") or "").strip().lower()
        key = label_map.get(label)
        if not key:
            continue
        totals[key] = sum((row[1] or 0) for row in series.get("dates", []))
    return totals


def pull_root_window(team_id: int, days: int = 7, workspace_name: str = "") -> dict:
    """Pull root-domain-keyed deliverability stats for the last `days` days.

    Steps:
      1. Switch workspace.
      2. List sender mailboxes (also gives subdomain identity per sender).
      3. Group senders by root domain.
      4. For each root: GET /api/campaign-events/stats over the window.
      5. Walk bounced replies workspace-wide once. Filter to window by
         date_received. Group by root via sender_email_id -> sender -> root.
      6. Classify each in-window bounce. Aggregate buckets per root.

    Returns:
        {
            "workspace_id": int,
            "workspace_name": str,
            "window_start": str (YYYY-MM-DD),
            "window_end":   str (YYYY-MM-DD),
            "roots": [  # one entry per root domain
                {
                    "domain":          str,    # root, e.g. "emailreachos.com"
                    "subdomains":      [str],  # subdomains in this root
                    "subdomain_count": int,
                    "mailbox_count":   int,
                    "sender_email_ids": [int],
                    "emails_sent":     int,    # in-window
                    "replies":         int,
                    "bounces":         int,
                    "bounces_hard":    int,
                    "bounces_reputation": int,
                    "bounces_soft":    int,
                    "bounces_auth":    int,
                    "bounces_unknown": int,
                },
                ...
            ],
        }
    """
    end_date = dt.date.today()
    start_date = end_date - dt.timedelta(days=days)
    window_start_iso = start_date.isoformat()
    window_end_iso = end_date.isoformat()

    print(f"[1/4] switching to workspace {team_id}", file=sys.stderr)
    switch_workspace(team_id)

    print(f"[2/4] listing sender mailboxes", file=sys.stderr)
    all_senders = list_sender_emails()
    senders = [s for s in all_senders if _has_required_tag(s)]
    print(
        f"      {len(senders)}/{len(all_senders)} mailboxes tagged '{REQUIRED_TAG}' "
        f"(filtered {len(all_senders) - len(senders)} untagged)",
        file=sys.stderr,
    )

    # Map sender_id -> (subdomain, root)
    sender_meta: dict[int, dict] = {}
    by_root: dict[str, dict] = {}
    for s in senders:
        sid = s["id"]
        email = s.get("email") or ""
        sub = email.split("@", 1)[1].lower() if "@" in email else ""
        root = root_domain(sub) if sub else ""
        sender_meta[sid] = {"sub": sub, "root": root, "status": s.get("status")}
        if not root:
            continue
        bucket = by_root.setdefault(root, {
            "domain": root,
            "subdomains": set(),
            "sender_email_ids": [],
            "mailbox_count": 0,
        })
        if sub:
            bucket["subdomains"].add(sub)
        bucket["sender_email_ids"].append(sid)
        bucket["mailbox_count"] += 1

    print(f"[3/4] pulling in-window stats per root ({len(by_root)} roots, {days}-day window)", file=sys.stderr)
    for i, (root, bucket) in enumerate(by_root.items(), 1):
        stats = get_window_stats(bucket["sender_email_ids"], window_start_iso, window_end_iso)
        bucket["emails_sent"] = stats["sent"]
        bucket["replies"] = stats["replied"]
        bucket["bounces"] = stats["bounced"]
        bucket["opens"] = stats["opens"]
        bucket["interested"] = stats["interested"]
        if i % 10 == 0:
            print(f"      progress: {i}/{len(by_root)} roots", file=sys.stderr)
        time.sleep(0.05)

    print(f"[4/4] walking bounced replies workspace-wide", file=sys.stderr)
    bounced = list_all_bounced_replies()
    print(f"      {len(bounced)} total bounce notifications retrieved", file=sys.stderr)

    # Filter to window + classify + group by root
    window_start_dt = dt.datetime.combine(start_date, dt.time.min, tzinfo=dt.timezone.utc)
    in_window_count = 0
    for r in bounced:
        # date_received format: "2024-09-21T02:10:42.000000Z"
        dr_raw = r.get("date_received") or ""
        try:
            dr = dt.datetime.fromisoformat(dr_raw.replace("Z", "+00:00"))
        except ValueError:
            continue
        if dr < window_start_dt:
            continue
        sid = r.get("sender_email_id")
        if sid is None:
            continue
        meta = sender_meta.get(sid)
        if not meta or not meta["root"]:
            continue
        root = meta["root"]
        bucket = by_root.get(root)
        if not bucket:
            continue
        text = (r.get("text_body") or "") + "\n" + (r.get("subject") or "")
        if not text.strip():
            text = r.get("html_body") or ""
        bucket_key = f"bounces_{classify_bounce(text)}"
        bucket[bucket_key] = bucket.get(bucket_key, 0) + 1
        in_window_count += 1
    print(f"      {in_window_count} bounces in window, classified into roots", file=sys.stderr)

    # Materialise roots into a list with stable shape
    roots_out = []
    for root, bucket in by_root.items():
        roots_out.append({
            "domain": root,
            "subdomains": sorted(bucket["subdomains"]),
            "subdomain_count": len(bucket["subdomains"]),
            "mailbox_count": bucket["mailbox_count"],
            "sender_email_ids": bucket["sender_email_ids"],
            "emails_sent": bucket.get("emails_sent", 0),
            "replies": bucket.get("replies", 0),
            "bounces": bucket.get("bounces", 0),
            "bounces_hard": bucket.get("bounces_hard", 0),
            "bounces_reputation": bucket.get("bounces_reputation", 0),
            "bounces_soft": bucket.get("bounces_soft", 0),
            "bounces_auth": bucket.get("bounces_auth", 0),
            "bounces_unknown": bucket.get("bounces_unknown", 0),
            "bounces_warmup": bucket.get("bounces_warmup", 0),
        })

    return {
        "workspace_id": team_id,
        "workspace_name": workspace_name,
        "window_start": window_start_iso,
        "window_end": window_end_iso,
        "days": days,
        "roots": roots_out,
    }


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: bison_pull.py <workspace_id> [workspace_name] [days]", file=sys.stderr)
        sys.exit(2)
    wid = int(sys.argv[1])
    wname = sys.argv[2] if len(sys.argv) > 2 else ""
    days = int(sys.argv[3]) if len(sys.argv) > 3 else 7
    result = pull_root_window(wid, days, wname)
    json.dump(result, sys.stdout, indent=2, default=str)
