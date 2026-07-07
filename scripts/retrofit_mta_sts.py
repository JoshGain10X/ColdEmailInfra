#!/usr/bin/env python3
"""Fleet-wide retrofit: MTA-STS + TLS-RPT, with an enforce-readiness audit.

Some shards (the four oldest roots) predate MTA-STS entirely and have no
records or vhost at all; others have the records but with the buggy policy that
listed only `mail.<root>` instead of the real per-subdomain MX hosts. This
script brings every live shard to the corrected policy (mode=testing) and
audits whether promoting to enforce would be safe.

For every live shard:

  1. Ensure the MTA-STS + TLS-RPT DNS records exist via Cloudflare (idempotent
     upserts):
       mta-sts.<root>    A   -> vps_ip (unproxied)
       _mta-sts.<root>   TXT = v=STSv1; id=<epoch>;
       _smtp._tls.<root> TXT = v=TLSRPTv1; rua=mailto:tls-rpt@<root>
     The policy id is pinned in shard state (mta_sts_id) so reruns do not churn
     it; it is only regenerated when the policy body changes.
  2. Ensure Caddy + the MTA-STS vhost are installed via install_mta_sts(), which
     now enumerates one `mx:` line per sending subdomain's mail host plus the
     apex, and installs Caddy if missing (the four oldest roots predate Caddy,
     so this bootstraps it the same way as landing --bootstrap-caddy).
  3. Cert-coverage AUDIT for enforce-readiness (report only, changes nothing):
     for each sending subdomain's MX host mail.<sub>.<root>, check the TLS cert
     the mailserver presents on port 25 and decide ENFORCE-SAFE (cert covers
     every MX host) or ENFORCE-UNSAFE (a name mismatch -> promoting to enforce
     would break inbound replies).
  4. Mark state.steps["mta_sts_retrofit_v2"] so reruns are no-ops.

Enforce is intentionally NEVER flipped here. mode stays testing regardless of
the audit result; the audit is the deliverable that decides whether enforce is
viable at all.

"Live" means bison_loaded=true and status <> 'destroyed'. Passing --domain
skips the bison_loaded filter (explicit targeting), but never touches destroyed
shards.

Run on the control VPS where the shard state files and SSH key live. The
Supabase env vars must be set (SUPABASE_URL, SUPABASE_SERVICE_KEY).

Usage:
    python scripts/retrofit_mta_sts.py --dry-run
    python scripts/retrofit_mta_sts.py --domain become-a-10xmanager.com
    python scripts/retrofit_mta_sts.py --client 10x-managers
    python scripts/retrofit_mta_sts.py          # whole live fleet
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import click
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.client_context import load_client_context_by_slug, _supabase  # type: ignore
from lib.mailserver import MailserverClient
from lib.state import ShardState


RETROFIT_STEP = "mta_sts_retrofit_v2"
PAUSE_BETWEEN_DOMAINS_S = 2.5


def _ssh_user(state: ShardState) -> str:
    vps = state.get("vps") or {}
    return vps.get("ssh_user") or os.environ.get("SSH_USER", "admin")


def _zone_id(sb, cf, domain: str) -> str:
    """infra_domains.zone_id first, live Cloudflare lookup as fallback."""
    row = (
        sb.table("infra_domains")
        .select("zone_id")
        .eq("domain", domain)
        .limit(1)
        .execute()
        .data
    )
    if row and row[0].get("zone_id"):
        return row[0]["zone_id"]
    zone_id = cf.get_zone_id(domain)
    if not zone_id:
        raise RuntimeError(f"no Cloudflare zone found for {domain}")
    return zone_id


def _mta_sts_id(state: ShardState) -> str:
    """Return a stable MTA-STS policy id, pinning it in state on first use.

    The id only needs to change when the policy body changes. We do not have a
    reliable prior body to diff against on the very old roots, so we pin an id
    once and reuse it on subsequent runs - avoiding the idempotency break of
    minting a fresh epoch every run.
    """
    existing = state.get("mta_sts_id")
    if existing:
        return str(existing)
    new_id = str(int(datetime.now(timezone.utc).timestamp()))
    state.set("mta_sts_id", new_id)
    return new_id


def _audit_enforce_readiness(
    ms: MailserverClient, domain: str, subs: list[str]
) -> dict:
    """Compare the presented port-25 cert against every MX host.

    Returns {"safe": bool, "cert_names": [...], "uncovered": [...]}.
    """
    cert_names = ms.smtp_cert_names()
    mx_hosts = [f"mail.{sub}.{domain}" for sub in sorted(subs)]
    mx_hosts.append(f"mail.{domain}")
    uncovered = [h for h in mx_hosts if not MailserverClient.cert_covers(h, cert_names)]
    return {
        "safe": not uncovered,
        "cert_names": cert_names,
        "uncovered": uncovered,
        "mx_hosts": mx_hosts,
    }


def _retrofit_one(sb, shard_row: dict, dry_run: bool) -> dict:
    domain = shard_row["domain"]
    client_slug = shard_row["clients"]["slug"]

    state = ShardState(domain)
    if not state.path.exists():
        return {"domain": domain, "skipped": "no_state_file"}
    if state.is_step_done(RETROFIT_STEP):
        return {"domain": domain, "skipped": "already_retrofitted"}

    subs = state.get("subdomains") or []
    if not subs:
        return {"domain": domain, "error": "no_subdomains"}

    vps_state = state.get("vps") or {}
    vps_ip = vps_state.get("ip") or shard_row.get("vps_ip")
    if not vps_ip:
        return {"domain": domain, "error": "no_vps_ip"}

    click.echo(f"\n=== {domain} ===")
    click.echo(f"  client     : {client_slug}")
    click.echo(f"  vps_ip     : {vps_ip}")
    click.echo(f"  subdomains : {subs}")

    if dry_run:
        preview = MailserverClient.mta_sts_policy(domain, subdomains=subs, mode="testing")
        click.echo("  DRY-RUN: would ensure MTA-STS/TLS-RPT DNS, install vhost, audit cert.")
        click.echo("  DRY-RUN policy that would be served:")
        for line in preview.rstrip("\n").splitlines():
            click.echo(f"      {line}")
        return {"domain": domain, "dry_run": True}

    ctx = load_client_context_by_slug(client_slug)
    cf = ctx.cloudflare
    zone_id = state.get("cloudflare_zone_id") or _zone_id(sb, cf, domain)

    # (1) Ensure DNS records (idempotent upserts).
    mta_sts_id = _mta_sts_id(state)
    cf.upsert_record(zone_id, "A", f"mta-sts.{domain}", vps_ip, proxied=False)
    cf.upsert_record(zone_id, "TXT", f"_mta-sts.{domain}", f"v=STSv1; id={mta_sts_id};")
    cf.upsert_record(
        zone_id, "TXT", f"_smtp._tls.{domain}", f"v=TLSRPTv1; rua=mailto:tls-rpt@{domain}"
    )
    click.echo("  ok  MTA-STS + TLS-RPT DNS records ensured")

    # (2) Ensure Caddy + MTA-STS vhost, with the corrected multi-MX policy.
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ms = MailserverClient(vps_ip, ssh_key, user=_ssh_user(state))
    ms.connect()
    try:
        ms.install_mta_sts(domain, mode="testing", subdomains=subs)
        click.echo("  ok  Caddy + MTA-STS vhost installed (mode=testing)")

        # (3) Enforce-readiness cert audit (report only).
        audit = _audit_enforce_readiness(ms, domain, subs)
    finally:
        ms.close()

    verdict = "ENFORCE-SAFE" if audit["safe"] else "ENFORCE-UNSAFE"
    click.echo(f"  audit: {verdict}")
    click.echo(f"    cert names : {audit['cert_names']}")
    if audit["uncovered"]:
        click.echo(f"    UNCOVERED  : {audit['uncovered']}")

    # (4) Idempotency flag.
    state.mark_step_done(RETROFIT_STEP)
    click.echo("  ok  flag set (state.mta_sts_retrofit_v2)")

    return {
        "domain": domain,
        "ok": True,
        "enforce_safe": audit["safe"],
        "uncovered": audit["uncovered"],
        "cert_names": audit["cert_names"],
    }


@click.command()
@click.option("--domain", default=None, help="Scope to one shard root (skips the bison_loaded filter).")
@click.option("--client", "client_slug", default=None, help="Scope to one client slug (e.g. 10x-managers).")
@click.option("--dry-run", is_flag=True, help="Print intent only; touch nothing.")
def main(domain: str | None, client_slug: str | None, dry_run: bool) -> None:
    load_dotenv()
    sb = _supabase()

    q = (
        sb.table("infra_shards")
        .select("domain,vps_ip,status,client_id,clients!inner(slug)")
        .neq("status", "destroyed")
    )
    if domain:
        q = q.eq("domain", domain)
    else:
        # bison_loaded is the authoritative live signal (status can be stale)
        q = q.eq("bison_loaded", True)
    if client_slug:
        q = q.eq("clients.slug", client_slug)

    shards = q.execute().data or []
    if not shards:
        click.echo("No shards matched.")
        return

    click.echo(f"Retrofitting {len(shards)} shard(s) (dry_run={dry_run})")
    results: list[dict] = []
    for i, row in enumerate(shards):
        try:
            results.append(_retrofit_one(sb, row, dry_run=dry_run))
        except Exception as exc:
            click.echo(f"  x ERROR: {exc}")
            results.append({"domain": row["domain"], "error": str(exc)[:200]})
        if i < len(shards) - 1:
            time.sleep(PAUSE_BETWEEN_DOMAINS_S)

    click.echo("\n=== SUMMARY ===")
    ok = [r for r in results if r.get("ok")]
    skipped = [r for r in results if r.get("skipped")]
    dry = [r for r in results if r.get("dry_run")]
    errs = [r for r in results if r.get("error")]
    click.echo(f"  ok       : {len(ok)}")
    click.echo(f"  dry-run  : {len(dry)}")
    click.echo(f"  skipped  : {len(skipped)}  (already retrofitted or no state)")
    click.echo(f"  errors   : {len(errs)}")
    for r in errs:
        click.echo(f"    FAIL {r['domain']}: {r['error']}")

    # Enforce-readiness table - the deliverable that decides enforce viability.
    if ok:
        click.echo("\n=== ENFORCE-READINESS ===")
        click.echo(f"  {'DOMAIN':<34} VERDICT         UNCOVERED MX HOSTS")
        for r in ok:
            verdict = "ENFORCE-SAFE" if r.get("enforce_safe") else "ENFORCE-UNSAFE"
            uncovered = ", ".join(r.get("uncovered") or []) or "-"
            click.echo(f"  {r['domain']:<34} {verdict:<15} {uncovered}")
        safe_n = sum(1 for r in ok if r.get("enforce_safe"))
        click.echo(f"\n  {safe_n}/{len(ok)} shard(s) ENFORCE-SAFE. "
                   "enforce is NOT auto-promoted - review before flipping.")


if __name__ == "__main__":
    main()
