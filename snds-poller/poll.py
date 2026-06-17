#!/usr/bin/env python3
"""Microsoft SNDS (Smart Network Data Services) daily poller.

Microsoft publishes a CSV per account at a URL keyed by a per-workspace secret
('key' query string). The CSV is one row per IP per day, refreshed on a rolling
basis. We pull the URL daily and upsert any new rows.

CSV columns (Microsoft's documented format, no header row):
  ip, dataStart, dataEnd, rcptCommandsSeen, rcptCommandsAccepted,
  dataCommandsAccepted, messageRecipientsAcceptingRange (activityBand),
  complaintRate, trapMessagePeriod, trapHitCount, blockStatus,
  regularBoltStatus, sampleHelo, sampleFrom

We only persist the IP rows that intersect our active sending IPs (everything
else is noise from Microsoft's broader allocation view).

Env:
  SNDS_DATA_URL  (full https://.../snds/data.aspx?key=... URL Microsoft issues per workspace)
  SUPABASE_URL / SUPABASE_SERVICE_KEY
"""
from __future__ import annotations

import os
import sys
import csv
import io
import traceback
from datetime import datetime, timezone

import requests


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def _env(key: str, *alts: str) -> str | None:
    for k in (key, *alts):
        v = os.environ.get(k)
        if v:
            return v.strip()
    return None


def _our_ips() -> set[str]:
    """Pull the set of IPs we actually send from. Anything outside this is
    noise from Microsoft's broader allocation view (they sometimes return
    rows for IPs adjacent to ours in the same /24)."""
    url = _env("SUPABASE_URL", "SUPABASE_COLD_EMAIL_URL")
    key = _env("SUPABASE_SERVICE_KEY", "SUPABASE_COLD_EMAIL_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("Supabase env not set")
    r = requests.get(
        f"{url.rstrip('/')}/rest/v1/infra_shards?select=vps_ip&status=in.(active,verified)&bison_loaded=eq.true&vps_ip=not.is.null",
        headers={"apikey": key, "Authorization": f"Bearer {key}"},
        timeout=30,
    )
    r.raise_for_status()
    return {row["vps_ip"] for row in (r.json() or []) if row.get("vps_ip")}


def _fetch_csv() -> str:
    url = _env("SNDS_DATA_URL")
    if not url:
        raise RuntimeError("SNDS_DATA_URL not set - waiting on Microsoft approval")
    r = requests.get(url, timeout=60)
    r.raise_for_status()
    return r.text


def _parse_pct(s: str) -> float | None:
    """SNDS percentage strings come in formats: '< 0.1%', '5.2%', '-' for null."""
    s = (s or "").strip()
    if not s or s == "-":
        return None
    s = s.replace("<", "").replace(">", "").replace("%", "").strip()
    try:
        return float(s) / 100.0
    except ValueError:
        return None


def _parse_int(s: str) -> int | None:
    s = (s or "").strip()
    if not s or s == "-":
        return None
    try:
        return int(s.replace(",", ""))
    except ValueError:
        return None


def _parse_ts(s: str) -> str | None:
    """SNDS timestamps look like '6/16/2026 8:00:00 AM' (US format)."""
    s = (s or "").strip()
    if not s:
        return None
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            continue
    return None


def _parse_row(parts: list[str]) -> dict | None:
    # SNDS rows have at minimum 13 columns; tolerate missing tail columns
    if len(parts) < 4:
        return None
    ip = parts[0].strip()
    data_start = _parse_ts(parts[1]) if len(parts) > 1 else None
    data_end   = _parse_ts(parts[2]) if len(parts) > 2 else None
    rcpt_seen  = _parse_int(parts[3]) if len(parts) > 3 else None
    rcpt_acc   = _parse_int(parts[4]) if len(parts) > 4 else None
    data_acc   = _parse_int(parts[5]) if len(parts) > 5 else None
    activity   = parts[6].strip() if len(parts) > 6 else None
    complaint  = _parse_pct(parts[7]) if len(parts) > 7 else None
    trap_period = parts[8].strip() if len(parts) > 8 else None
    trap_hits  = _parse_int(parts[9]) if len(parts) > 9 else None
    sample_helo = parts[12].strip() if len(parts) > 12 else None
    sample_from = parts[13].strip() if len(parts) > 13 else None
    # Use dataEnd's date as the row's reporting date
    rep_date = None
    if data_end:
        rep_date = data_end[:10]  # YYYY-MM-DD
    elif data_start:
        rep_date = data_start[:10]
    if not rep_date:
        return None
    return {
        "ip": ip,
        "date": rep_date,
        "data_start": data_start,
        "data_end": data_end,
        "rcpt_commands_seen": rcpt_seen,
        "rcpt_commands_accepted": rcpt_acc,
        "data_commands_accepted": data_acc,
        "complaint_rate": complaint,
        "trap_message_period": trap_period,
        "trap_hit_count": trap_hits,
        "sample_helo": sample_helo,
        "sample_from": sample_from,
        "activity_band": activity,
        "raw_csv_row": ",".join(parts),
    }


def _sb_upsert(rows: list[dict]) -> None:
    if not rows:
        return
    url = _env("SUPABASE_URL", "SUPABASE_COLD_EMAIL_URL")
    key = _env("SUPABASE_SERVICE_KEY", "SUPABASE_COLD_EMAIL_SERVICE_KEY")
    r = requests.post(
        f"{url.rstrip('/')}/rest/v1/snds_daily_ip_stats?on_conflict=ip,date",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=merge-duplicates,return=minimal",
        },
        json=rows,
        timeout=60,
    )
    if not r.ok:
        raise RuntimeError(f"supabase upsert {r.status_code}: {r.text[:300]}")


def main() -> int:
    _log("=== snds poll start ===")
    our_ips = _our_ips()
    _log(f"  active fleet has {len(our_ips)} sending IPs")
    csv_text = _fetch_csv()
    _log(f"  fetched {len(csv_text)} bytes of CSV from Microsoft")

    rows_out: list[dict] = []
    skipped_not_ours = 0
    parsed = 0
    reader = csv.reader(io.StringIO(csv_text))
    for parts in reader:
        if not parts or parts[0].lower().startswith("ip"):
            continue  # skip blank rows or header if present
        row = _parse_row(parts)
        if not row:
            continue
        parsed += 1
        if row["ip"] not in our_ips:
            skipped_not_ours += 1
            continue
        rows_out.append(row)

    _log(f"  parsed {parsed} rows, kept {len(rows_out)} matching our fleet ({skipped_not_ours} adjacent-IP rows skipped)")
    if rows_out:
        _sb_upsert(rows_out)
        _log(f"  upserted {len(rows_out)} rows into snds_daily_ip_stats")
    _log("=== snds poll done ===")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
