#!/usr/bin/env python3
"""DMARC report poller — one tick of the */30 cron schedule.

Reads every active/verified shard from `infra_shards`, opens each shard's
DMARC IMAP mailbox at `dmarc@<sub>.<root>` (creds: shared mailbox password
from generate.py), uses parsedmarc to fetch + parse aggregate and forensic
reports, and inserts structured rows into Supabase. Successful messages are
moved to the IMAP `Archive` folder on the shard's mail VPS so they don't
re-parse next tick. A fail in one shard never blocks the rest of the run.

Run inside the coldemail-dmarc-ingest container; supercronic kicks this off
every 30 min. Logs go to stdout → docker json log driver.
"""
from __future__ import annotations

import hashlib
import os
import ssl
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, "/app/scripts")

from supabase import Client, create_client

from lib.generate import SHARED_MAILBOX_PASSWORD
from lib.state import ShardState


NO_VERIFY_CTX = ssl.create_default_context()
NO_VERIFY_CTX.check_hostname = False
NO_VERIFY_CTX.verify_mode = ssl.CERT_NONE


def _sb() -> Client:
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def _log(msg: str) -> None:
    print(f"[{datetime.now(timezone.utc).isoformat()}] {msg}", flush=True)


def _start_run(sb: Client) -> int:
    row = sb.table("dmarc_ingest_runs").insert({}).execute().data[0]
    return row["id"]


def _finish_run(sb: Client, run_id: int, *, polled: int, new: int, dup: int, errors: list[dict]) -> None:
    sb.table("dmarc_ingest_runs").update({
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "shards_polled": polled,
        "reports_new": new,
        "reports_dup": dup,
        "errors": errors,
        "ok": len(errors) == 0,
    }).eq("id", run_id).execute()


def _upsert_aggregate(sb: Client, report: dict, shard_domain: str, client_slug: str | None) -> bool:
    # parsedmarc returns dicts shaped per its docs. We map the fields we care
    # about — anything missing is recorded as NULL. Dedup key is the UNIQUE
    # (org_name, report_id, shard_domain) constraint plus a sha256 fallback.
    meta = report.get("report_metadata") or {}
    policy = report.get("policy_published") or {}
    raw = (report.get("xml") or "").encode("utf-8", "replace")
    sha = hashlib.sha256(raw).hexdigest() if raw else hashlib.sha256(
        (meta.get("report_id", "") + meta.get("org_name", "")).encode()
    ).hexdigest()

    res = sb.table("dmarc_aggregate_reports").upsert({
        "report_id": meta.get("report_id", ""),
        "org_name": meta.get("org_name", ""),
        "org_email": meta.get("org_email"),
        "date_begin": _iso(meta.get("begin_date") or meta.get("date_range_begin")),
        "date_end": _iso(meta.get("end_date") or meta.get("date_range_end")),
        "policy_domain": policy.get("domain", ""),
        "shard_domain": shard_domain,
        "client_slug": client_slug,
        "policy_p": policy.get("p"),
        "policy_sp": policy.get("sp"),
        "policy_pct": _int(policy.get("pct")),
        "raw_xml_sha256": sha,
    }, on_conflict="org_name,report_id,shard_domain", ignore_duplicates=False).execute()

    # `upsert` doesn't tell us insert-vs-update directly. Probe by checking
    # whether records already exist for this report.
    if not res.data:
        return False
    report_id = res.data[0]["id"]

    # Check whether records already inserted — if yes, this was a re-upsert
    existing = sb.table("dmarc_aggregate_records").select("id").eq("report_id", report_id).limit(1).execute()
    if existing.data:
        return False

    rows_to_insert = []
    for rec in report.get("records", []) or []:
        ident = rec.get("identifiers") or {}
        result = rec.get("auth_results") or {}
        rec_row = rec.get("row") or {}
        policy_eval = rec_row.get("policy_evaluated") or {}
        dkim_results = result.get("dkim") or []
        spf_results = result.get("spf") or []
        rows_to_insert.append({
            "report_id": report_id,
            "source_ip": rec_row.get("source_ip") or rec.get("source", {}).get("ip_address"),
            "source_ptr": rec.get("source", {}).get("reverse_dns"),
            "count": _int(rec_row.get("count")) or 0,
            "disposition": policy_eval.get("disposition"),
            "dkim_aligned": policy_eval.get("dkim") == "pass",
            "spf_aligned": policy_eval.get("spf") == "pass",
            "dkim_result": (dkim_results[0].get("result") if dkim_results else None),
            "spf_result": (spf_results[0].get("result") if spf_results else None),
            "header_from": ident.get("header_from"),
            "envelope_from": ident.get("envelope_from"),
            "envelope_to": ident.get("envelope_to"),
            "dkim_domains": [d.get("domain") for d in dkim_results if d.get("domain")],
            "spf_domain": (spf_results[0].get("domain") if spf_results else None),
        })
    if rows_to_insert:
        sb.table("dmarc_aggregate_records").insert(rows_to_insert).execute()
    return True


def _upsert_forensic(sb: Client, report: dict, shard_domain: str, client_slug: str | None) -> bool:
    raw_headers = report.get("parsed_sample", {}).get("headers", "")
    sha = hashlib.sha256(str(raw_headers).encode()).hexdigest()
    res = sb.table("dmarc_forensic_reports").upsert({
        "shard_domain": shard_domain,
        "client_slug": client_slug,
        "arrival_date": _iso(report.get("arrival_date")),
        "source_ip": report.get("source", {}).get("ip_address"),
        "header_from": report.get("parsed_sample", {}).get("from", {}).get("address"),
        "subject": report.get("parsed_sample", {}).get("subject"),
        "spf_result": report.get("delivery_result"),
        "dkim_result": report.get("authentication_results"),
        "raw_sha256": sha,
        "raw_headers": str(raw_headers)[:8000] if raw_headers else None,
    }, on_conflict="raw_sha256", ignore_duplicates=True).execute()
    return bool(res.data)


def _iso(v) -> str | None:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.isoformat()
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(int(v), tz=timezone.utc).isoformat()
    return str(v)


def _int(v):
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _poll_shard(sb: Client, shard: dict) -> tuple[int, int, list[dict]]:
    domain = shard["domain"]
    client_slug = (shard.get("clients") or {}).get("slug")

    state = ShardState(domain)
    inbox = state.get("dmarc_inbox")
    if not inbox:
        return 0, 0, [{"shard": domain, "kind": "no_inbox", "msg": "state.dmarc_inbox empty"}]

    # mail host = subdomain prefixed with mail.
    fqdn_part = inbox.split("@", 1)[1]
    mail_host = f"mail.{fqdn_part}"

    try:
        from parsedmarc import get_dmarc_reports_from_mailbox
        from parsedmarc.mail import IMAPConnection
    except ImportError as exc:
        return 0, 0, [{"shard": domain, "kind": "import", "msg": str(exc)}]

    try:
        # `verify=False` because our mail VPSes serve a cert for mail.<root>,
        # not for mail.<sub>.<root> — Let's Encrypt only issued for the apex
        # mail host. The TLS itself is real (LE cert), just hostname-mismatched
        # for the wildcard subdomain pattern we use.
        connection = IMAPConnection(
            host=mail_host, user=inbox, password=SHARED_MAILBOX_PASSWORD,
            port=993, ssl=True, verify=False, timeout=30,
        )
        result = get_dmarc_reports_from_mailbox(
            connection=connection,
            reports_folder="INBOX",
            archive_folder="Archive",
            delete=False,
            test=False,
            create_folders=True,
        )
    except Exception as exc:
        return 0, 0, [{"shard": domain, "kind": "imap_or_parse", "msg": f"{type(exc).__name__}: {str(exc)[:300]}"}]

    new = 0
    dup = 0
    errs: list[dict] = []
    for ag in result.get("aggregate_reports", []) or []:
        try:
            if _upsert_aggregate(sb, ag, domain, client_slug):
                new += 1
            else:
                dup += 1
        except Exception as exc:
            errs.append({"shard": domain, "kind": "agg_upsert", "msg": f"{type(exc).__name__}: {str(exc)[:300]}"})
    for fo in result.get("forensic_reports", []) or []:
        try:
            if _upsert_forensic(sb, fo, domain, client_slug):
                new += 1
            else:
                dup += 1
        except Exception as exc:
            errs.append({"shard": domain, "kind": "forensic_upsert", "msg": f"{type(exc).__name__}: {str(exc)[:300]}"})

    return new, dup, errs


def main() -> int:
    sb = _sb()
    run_id = _start_run(sb)
    _log(f"run_id={run_id} starting")

    shards = (
        sb.table("infra_shards")
        .select("domain,vps_ip,status,client_id,clients!inner(slug)")
        .in_("status", ["active", "verified"])
        .execute()
        .data
        or []
    )
    _log(f"shards_to_poll={len(shards)}")

    total_new, total_dup = 0, 0
    all_errs: list[dict] = []
    for shard in shards:
        try:
            new, dup, errs = _poll_shard(sb, shard)
            total_new += new
            total_dup += dup
            all_errs.extend(errs)
            if new or dup or errs:
                _log(f"  {shard['domain']:<35} new={new} dup={dup} errors={len(errs)}")
        except Exception:
            tb = traceback.format_exc()
            _log(f"  {shard['domain']} UNEXPECTED:\n{tb}")
            all_errs.append({"shard": shard["domain"], "kind": "unexpected", "msg": tb[:500]})

    _finish_run(sb, run_id, polled=len(shards), new=total_new, dup=total_dup, errors=all_errs)
    _log(f"run_id={run_id} done new={total_new} dup={total_dup} errors={len(all_errs)}")
    return 0 if not all_errs else 1


if __name__ == "__main__":
    sys.exit(main())
