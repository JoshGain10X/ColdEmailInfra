#!/usr/bin/env python3
"""Fleet-wide retrofit: put every sending subdomain's MX host on the TLS cert.

Each sending subdomain has its own MX target:

    team.evolve10xleaders.com   MX -> mail.team.evolve10xleaders.com
    journey.evolve10xleaders.com MX -> mail.journey.evolve10xleaders.com

but the Let's Encrypt cert carries a single SAN, `mail.<root>`. So the cert
never matches the hostname a sender actually connects to.

Today that is invisible: senders using opportunistic TLS do not verify the
name, and our MTA-STS policies are all `mode: testing`, which requires senders
to deliver even when the policy cannot be satisfied. It is a latent trap, not a
live fault - inbound mail is arriving normally.

What it blocks is `mode: enforce`. An MTA-STS-honouring sender (Google and
Microsoft both are) validates the presented cert against the MX hostname, gets a
mismatch, and REFUSES delivery - which would silently kill inbound replies for
every mailbox. See reference-mta-sts-enforce-blocked-by-cert.

This script closes that gap so the option exists. It does NOT change the
MTA-STS mode: the deliberate decision is to stay on `testing`, because MTA-STS
is a receiver-side policy and enforcing buys nothing on outbound placement.

Mechanics: reissues the cert via certbot DNS-01 with `--cert-name mail.<root>`
(pins the lineage so the path docker-mailserver reads does not change) plus
`--expand`, then restarts the mailserver so Dovecot/Postfix pick up the new
chain. The name list is derived from the shard's own subdomain list, which is
the same source the MTA-STS policy is built from, so cert and policy cannot
drift apart.

Certbot waits 600s for DNS propagation per run, so budget ~10 min per shard.
Run it in the background for the whole fleet.

Usage:
    python scripts/retrofit_cert_sans.py --dry-run
    python scripts/retrofit_cert_sans.py --domain evolve10xleaders.com
    python scripts/retrofit_cert_sans.py                # whole live fleet
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.client_context import load_client_context_by_id, _supabase  # type: ignore
from lib.mailserver import MailserverClient  # type: ignore
from lib.state import ShardState


RETROFIT_STEP = "cert_sans_expanded"
PAUSE_BETWEEN_DOMAINS_S = 5.0


def _ssh_user(state: ShardState) -> str:
    vps = state.get("vps") or {}
    return vps.get("ssh_user") or os.environ.get("SSH_USER", "admin")


def _retrofit_one(shard_row: dict, dry_run: bool, recheck: bool) -> dict:
    domain = shard_row["domain"]
    state = ShardState(domain)
    if not state.path.exists():
        return {"domain": domain, "skipped": "no_state_file"}
    if state.is_step_done(RETROFIT_STEP) and not recheck:
        return {"domain": domain, "skipped": "already_expanded"}

    subs = state.get("subdomains") or []
    if not subs:
        return {"domain": domain, "skipped": "no_subdomains_in_state"}

    vps_ip = (state.get("vps") or {}).get("ip") or shard_row.get("vps_ip")
    if not vps_ip:
        return {"domain": domain, "error": "no_vps_ip"}

    primary = f"mail.{domain}"
    extra = [f"mail.{s}.{domain}" for s in sorted(subs)]

    click.echo(f"\n=== {domain} ===")
    click.echo(f"  vps_ip : {vps_ip}")
    click.echo(f"  names  : {primary} + {len(extra)} subdomain MX hosts")
    for n in extra:
        click.echo(f"           {n}")

    if dry_run:
        click.echo("  DRY-RUN: would reissue cert (--cert-name pinned, --expand) + restart mailserver")
        return {"domain": domain, "dry_run": True, "names": 1 + len(extra)}

    ctx = load_client_context_by_id(shard_row["client_id"])
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ms = MailserverClient(vps_ip, ssh_key, user=_ssh_user(state))
    ms.connect()
    try:
        ms.acquire_letsencrypt_cert(
            primary, ctx.le_email, ctx.cloudflare.token, extra_names=extra
        )
        # Read back the SANs actually on the issued cert before restarting.
        rc, out, _ = ms.sudo(
            f"openssl x509 -in /etc/letsencrypt/live/{primary}/fullchain.pem "
            f"-noout -ext subjectAltName",
            check=False,
        )
        got = {t.strip().removeprefix("DNS:") for t in out.replace("\n", ",").split(",") if "DNS:" in t}
        missing = [n for n in [primary] + extra if n not in got]
        if missing:
            return {"domain": domain, "error": f"cert missing {len(missing)} name(s): {missing[:4]}"}
        ms.restart_mailserver()
    finally:
        ms.close()

    click.echo(f"  ok  cert now carries {len(got)} names; mailserver restarted")
    state.mark_step_done(RETROFIT_STEP)
    return {"domain": domain, "ok": True, "names": len(got)}


@click.command()
@click.option("--domain", default=None, help="Scope to one shard root (skips the bison_loaded filter).")
@click.option("--client", "client_slug", default=None, help="Scope to one client slug.")
@click.option("--dry-run", is_flag=True, help="Print intent only; touch nothing.")
@click.option("--recheck", is_flag=True, help="Re-issue even if the step flag is set.")
def main(domain: str | None, client_slug: str | None, dry_run: bool, recheck: bool) -> None:
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
        q = q.eq("bison_loaded", True)
    if client_slug:
        q = q.eq("clients.slug", client_slug)
    shards = q.execute().data or []
    if not shards:
        click.echo("No shards matched.")
        return

    click.echo(f"Expanding cert SANs on {len(shards)} shard(s) (dry_run={dry_run})")
    click.echo("Each shard: ~10 min of certbot DNS-01 propagation wait, then a mailserver restart.")
    results = []
    for i, row in enumerate(shards):
        try:
            results.append(_retrofit_one(row, dry_run=dry_run, recheck=recheck))
        except Exception as exc:
            click.echo(f"  x ERROR: {exc}")
            results.append({"domain": row["domain"], "error": str(exc)[:300]})
        if i < len(shards) - 1:
            time.sleep(PAUSE_BETWEEN_DOMAINS_S)

    click.echo("\n=== SUMMARY ===")
    for label, key in (("ok", "ok"), ("dry-run", "dry_run"), ("skipped", "skipped"), ("errors", "error")):
        rows = [r for r in results if r.get(key)]
        click.echo(f"  {label:8}: {len(rows)}")
        for r in rows:
            detail = r.get("error") or r.get("skipped") or f"{r.get('names')} names"
            click.echo(f"    {r['domain']}: {detail}")


if __name__ == "__main__":
    main()
