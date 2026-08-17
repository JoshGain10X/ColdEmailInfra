#!/usr/bin/env python3
"""Fleet-wide retrofit: Dovecot connection limits + host inotify limits.

Two ceilings that every shard built before 2026-08-17 hit silently:

  1. `fs.inotify.max_user_instances` = 128 (kernel default). Dovecot takes one
     inotify instance per watched mailbox; a 100-mailbox shard needs ~225. Every
     unfixed shard measured EXACTLY 128/128 - fully exhausted - so Dovecot had
     logged "Inotify instance limit ... exceeded, disabling" and given up
     watching the overflow. Raised to 1024 (~4.5x measured demand).

  2. Dovecot `service imap-login` runs in high-security mode (service_count=1),
     forking one login process per connection and inheriting
     default_process_limit=500. A 100-mailbox shard holds ~400 concurrent
     sessions, so it sat at 80% of the ceiling and burst through it 25-40 times
     per shard per week, dropping client connections. High-performance mode
     collapses ~400 login processes into a handful, removing the ceiling.
     Note `service imap` was never the constraint - Dovecot's compiled default
     for it is already 1024.

Measured effect on the first shard done: imap-login 402 -> 15 processes, memory
1110MB -> 658MB used, swap 400MB -> 86MB, inotify warnings to zero.

Both fixes are applied through the same idempotent methods the deploy path uses,
so a retrofitted shard and a freshly built one end up identical:
  - MailserverClient.apply_memory_hardening()  -> inotify sysctl drop-in (+ swap,
    Caddy cap; both already present, re-applied as a no-op)
  - MailserverClient.apply_dovecot_limits()    -> dovecot.cf on the host AND
    copied into the live container, validated with `doveconf -n` before dovecot
    is restarted, previous config restored if it does not parse.

Why the container copy matters: DMS only copies dovecot.cf to
/etc/dovecot/local.conf inside `_setup_dovecot`, which runs at container START.
A plain `docker restart` does not re-run it, so writing the host file alone
changes nothing until the container is recreated.

"Live" means bison_loaded=true and status <> 'destroyed'. bison_loaded is the
authoritative is-it-sending signal; status can be stale. Passing --domain skips
the bison_loaded filter (explicit targeting), but never touches destroyed shards.

Run on the control VPS where the shard state files and SSH key live. The
Supabase env vars must be set (SUPABASE_URL, SUPABASE_SERVICE_KEY).

Usage:
    python scripts/retrofit_shard_limits.py --dry-run
    python scripts/retrofit_shard_limits.py --domain evolve10xleaders.com
    python scripts/retrofit_shard_limits.py --client 10x-managers
    python scripts/retrofit_shard_limits.py            # whole live fleet
    python scripts/retrofit_shard_limits.py --recheck  # re-verify, ignore step flag
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.client_context import _supabase  # type: ignore
from lib.mailserver import (  # type: ignore
    MailserverClient,
    INOTIFY_MAX_USER_INSTANCES,
    INOTIFY_MAX_USER_WATCHES,
)
from lib.state import ShardState


RETROFIT_STEP = "shard_limits_applied"
PAUSE_BETWEEN_DOMAINS_S = 3.0

# What a correctly retrofitted shard must report back.
EXPECTED_DOVECOT = {
    "imap_login_service_count": "0",      # high-performance mode
    "imap_login_process_limit": "64",
    "imap_login_client_limit": "1000",
}


def _ssh_user(state: ShardState) -> str:
    vps = state.get("vps") or {}
    return vps.get("ssh_user") or os.environ.get("SSH_USER", "admin")


def _retrofit_one(shard_row: dict, dry_run: bool, recheck: bool) -> dict:
    domain = shard_row["domain"]
    client_slug = (shard_row.get("clients") or {}).get("slug", "?")

    state = ShardState(domain)
    if not state.path.exists():
        return {"domain": domain, "skipped": "no_state_file"}
    if state.is_step_done(RETROFIT_STEP) and not recheck:
        return {"domain": domain, "skipped": "already_retrofitted"}

    vps_state = state.get("vps") or {}
    vps_ip = vps_state.get("ip") or shard_row.get("vps_ip")
    if not vps_ip:
        return {"domain": domain, "error": "no_vps_ip"}

    click.echo(f"\n=== {domain} ===")
    click.echo(f"  client : {client_slug}")
    click.echo(f"  vps_ip : {vps_ip}")

    if dry_run:
        click.echo("  DRY-RUN: would apply inotify sysctl drop-in + dovecot.cf "
                   "(host + live container) and restart dovecot")
        return {"domain": domain, "dry_run": True}

    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ms = MailserverClient(vps_ip, ssh_key, user=_ssh_user(state))
    ms.connect()
    try:
        before_i = ms.verify_inotify_limits()
        click.echo(f"  before: inotify {before_i.get('inuse')}/{before_i.get('inst')} in use")

        # (a) host kernel limits - idempotent, also re-asserts swap + Caddy cap
        ms.apply_memory_hardening()
        after_i = ms.verify_inotify_limits()

        # (b) dovecot limits - validated before restart, restored on parse failure
        ms.apply_dovecot_limits()
        dov = ms.verify_dovecot_limits()
    finally:
        ms.close()

    problems = []
    if after_i.get("inst") != str(INOTIFY_MAX_USER_INSTANCES):
        problems.append(f"inotify instances={after_i.get('inst')} expected={INOTIFY_MAX_USER_INSTANCES}")
    if after_i.get("watch") != str(INOTIFY_MAX_USER_WATCHES):
        problems.append(f"inotify watches={after_i.get('watch')} expected={INOTIFY_MAX_USER_WATCHES}")
    for key, expected in EXPECTED_DOVECOT.items():
        if dov.get(key) != expected:
            problems.append(f"{key}={dov.get(key)} expected={expected}")
    if problems:
        return {"domain": domain, "error": "verify_failed: " + "; ".join(problems)}

    click.echo(f"  ok  inotify {after_i.get('inuse')}/{after_i.get('inst')} in use "
               f"(was capped at {before_i.get('inst')})")
    click.echo(f"  ok  dovecot imap-login service_count={dov['imap_login_service_count']} "
               f"process_limit={dov['imap_login_process_limit']} "
               f"client_limit={dov['imap_login_client_limit']}")

    state.mark_step_done(RETROFIT_STEP)
    click.echo(f"  ok  flag set (state.{RETROFIT_STEP})")
    return {"domain": domain, "ok": True, "inotify_inuse": after_i.get("inuse")}


@click.command()
@click.option("--domain", default=None, help="Scope to one shard root (skips the bison_loaded filter).")
@click.option("--client", "client_slug", default=None, help="Scope to one client slug (e.g. 10x-managers).")
@click.option("--dry-run", is_flag=True, help="Print intent only; touch nothing.")
@click.option("--recheck", is_flag=True, help="Re-apply and re-verify even if the step flag is set.")
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

    click.echo(f"Retrofitting {len(shards)} shard(s) (dry_run={dry_run}, recheck={recheck})")
    click.echo("NOTE: each shard gets a dovecot restart (seconds). Postfix is untouched, "
               "so inbound mail queues rather than bounces.")
    results: list[dict] = []
    for i, row in enumerate(shards):
        try:
            results.append(_retrofit_one(row, dry_run=dry_run, recheck=recheck))
        except Exception as exc:
            click.echo(f"  x ERROR: {exc}")
            results.append({"domain": row["domain"], "error": str(exc)[:300]})
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
    for r in ok:
        click.echo(f"    OK   {r['domain']}  (inotify in use: {r.get('inotify_inuse')})")
    for r in skipped:
        click.echo(f"    SKIP {r['domain']}: {r['skipped']}")
    for r in errs:
        click.echo(f"    FAIL {r['domain']}: {r['error']}")


if __name__ == "__main__":
    main()
