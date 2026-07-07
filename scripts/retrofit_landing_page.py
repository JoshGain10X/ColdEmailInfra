#!/usr/bin/env python3
"""Fleet-wide retrofit: outreach landing page on sending-domain roots.

For every live shard of a client with client_settings.landing_page_url set,
this script:
  1. Flips the apex A record to proxied=False (grey cloud) so Caddy on the
     shard VPS terminates TLS for https://<root> itself.
  2. Adds the landing-page vhost to the shard's Caddyfile via SSH and
     reloads Caddy. Idempotent - the managed block is replaced, never
     duplicated (see lib.mailserver.landing_vhost_block).
  3. Attempts to delete the legacy apex redirect rule. The current API
     token 403s on the Rulesets API, so failure is tolerated and the rule
     is simply left in place (it is unreachable once the apex is
     grey-cloud, since the request never touches Cloudflare's edge).
  4. Verifies https://<root>/ returns 200 with a known landing-page marker,
     retrying for up to ~90s while Let's Encrypt issuance completes.
  5. Marks state.steps["landing_page_configured"] and sets the
     "landing_page" key in infra_shards.step_flags so reruns are no-ops.

"Live" means bison_loaded=true and status <> 'destroyed'. bison_loaded is
the authoritative is-it-sending signal; status can be stale. Passing
--domain skips the bison_loaded filter (explicit targeting of a shard that
has not been loaded yet), but never touches destroyed shards.

Run on infraapi1 where the shard state files and SSH key live. The Supabase
env vars must be set (SUPABASE_URL, SUPABASE_SERVICE_KEY).

Usage:
    python scripts/retrofit_landing_page.py --dry-run
    python scripts/retrofit_landing_page.py --domain get10xleaders.com
    python scripts/retrofit_landing_page.py --client 10x-managers
    python scripts/retrofit_landing_page.py          # whole eligible fleet
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click
import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.client_context import ClientContext, load_client_context_by_slug, _supabase  # type: ignore
from lib.mailserver import MailserverClient
from lib.state import ShardState


RETROFIT_STEP = "landing_page_configured"

# Strings we expect somewhere in the landing page body. Either is enough.
VERIFY_MARKERS = ("Let's end the theatre", "10X Managers")
VERIFY_TIMEOUT_S = 90   # allow Let's Encrypt issuance on first hit
VERIFY_POLL_S = 5
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


def _verify_landing(domain: str) -> tuple[bool, str]:
    """GET https://<domain>/ until it serves the landing page (or timeout)."""
    deadline = time.time() + VERIFY_TIMEOUT_S
    last = "no attempt made"
    while time.time() < deadline:
        try:
            resp = requests.get(f"https://{domain}/", timeout=15)
            marker_hit = any(m in resp.text for m in VERIFY_MARKERS)
            if resp.status_code == 200 and marker_hit:
                return True, f"200 OK, marker present ({len(resp.text)} bytes)"
            last = f"HTTP {resp.status_code}, marker={'yes' if marker_hit else 'no'}"
        except requests.RequestException as exc:
            # Expected while Caddy is mid Let's Encrypt issuance for the apex
            last = f"{type(exc).__name__}: {str(exc)[:150]}"
        time.sleep(VERIFY_POLL_S)
    return False, last


def _set_shard_landing_flag(sb, domain: str, client_id: str) -> None:
    """Merge {"landing_page": true} into infra_shards.step_flags."""
    row = (
        sb.table("infra_shards")
        .select("step_flags")
        .eq("domain", domain)
        .eq("client_id", client_id)
        .limit(1)
        .execute()
        .data
    )
    flags = (row[0].get("step_flags") if row else None) or {}
    flags["landing_page"] = True
    (
        sb.table("infra_shards")
        .update({"step_flags": flags})
        .eq("domain", domain)
        .eq("client_id", client_id)
        .execute()
    )


def _retrofit_one(
    sb,
    shard_row: dict,
    landing_url: str,
    ctx_cache: dict[str, ClientContext],
    dry_run: bool,
    bootstrap_caddy: bool = False,
) -> dict:
    domain = shard_row["domain"]
    client_slug = shard_row["clients"]["slug"]

    state = ShardState(domain)
    if not state.path.exists():
        return {"domain": domain, "skipped": "no_state_file"}
    if state.is_step_done(RETROFIT_STEP):
        return {"domain": domain, "skipped": "already_retrofitted"}

    vps_state = state.get("vps") or {}
    vps_ip = vps_state.get("ip") or shard_row.get("vps_ip")
    if not vps_ip:
        return {"domain": domain, "error": "no_vps_ip"}

    click.echo(f"\n=== {domain} ===")
    click.echo(f"  client      : {client_slug}")
    click.echo(f"  vps_ip      : {vps_ip}")
    click.echo(f"  landing_url : {landing_url}")

    if dry_run:
        click.echo("  DRY-RUN: would flip apex to grey-cloud, add Caddy vhost, verify HTTPS")
        return {"domain": domain, "dry_run": True}

    if client_slug not in ctx_cache:
        ctx_cache[client_slug] = load_client_context_by_slug(client_slug)
    ctx = ctx_cache[client_slug]
    cf = ctx.cloudflare
    zone_id = state.get("cloudflare_zone_id") or _zone_id(sb, cf, domain)

    # (a) Landing vhost via SSH + Caddy reload. Deliberately BEFORE the DNS
    # flip: if this fails (e.g. pre-Caddy shard without --bootstrap-caddy),
    # the apex keeps its current behaviour instead of pointing at a VPS
    # with no web server. LE issuance simply retries once DNS lands.
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ms = MailserverClient(vps_ip, ssh_key, user=_ssh_user(state))
    ms.connect()
    try:
        ms.install_landing_page(domain, landing_url, bootstrap=bootstrap_caddy)
    finally:
        ms.close()
    click.echo("  ✓ Caddy vhost installed + reloaded")

    # (b) Apex A record -> grey cloud so Caddy terminates TLS itself
    cf.upsert_record(zone_id, "A", domain, vps_ip, proxied=False)
    click.echo("  ✓ apex A record set to proxied=False")

    # (c) Best-effort removal of the legacy apex redirect rule. The rule is
    # unreachable anyway once the apex is grey-cloud, so a 403 here is fine.
    try:
        removed = cf.delete_redirect_rules(zone_id, domain)
        if removed:
            click.echo(f"  ✓ removed {removed} redirect rule(s)")
        else:
            click.echo("  - redirect rule left in place (none removable; "
                       "token likely lacks ruleset permission)")
    except Exception as exc:
        click.echo(f"  - redirect rule left in place (token lacks ruleset "
                   f"permission): {str(exc)[:150]}")

    # (d) Verify the landing page actually serves over HTTPS
    ok, detail = _verify_landing(domain)
    if not ok:
        return {"domain": domain, "error": f"verify_failed: {detail}"}
    click.echo(f"  ✓ verified https://{domain}/ ({detail})")

    # (e) Idempotency flags: shard state file + infra_shards.step_flags
    state.mark_step_done(RETROFIT_STEP)
    _set_shard_landing_flag(sb, domain, shard_row["client_id"])
    click.echo("  ✓ flags set (state + infra_shards.step_flags.landing_page)")

    return {"domain": domain, "ok": True}


@click.command()
@click.option("--domain", default=None, help="Scope to one shard root (skips the bison_loaded filter).")
@click.option("--client", "client_slug", default=None, help="Scope to one client slug (e.g. 10x-managers).")
@click.option("--dry-run", is_flag=True, help="Print intent only; touch nothing.")
@click.option("--bootstrap-caddy", is_flag=True,
              help="Install Caddy on shards that predate MTA-STS (pre 2026-05-21) "
                   "and start from a landing-only Caddyfile.")
def main(domain: str | None, client_slug: str | None, dry_run: bool, bootstrap_caddy: bool) -> None:
    load_dotenv()
    sb = _supabase()

    # Clients that have opted in via client_settings.landing_page_url
    settings_rows = (
        sb.table("client_settings")
        .select("client_id, landing_page_url")
        .not_.is_("landing_page_url", "null")
        .execute()
        .data
    ) or []
    landing_by_client = {
        r["client_id"]: r["landing_page_url"]
        for r in settings_rows
        if r.get("landing_page_url")
    }
    if not landing_by_client:
        click.echo("No clients have landing_page_url set. Nothing to do.")
        return

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
    eligible = [s for s in shards if s["client_id"] in landing_by_client]
    ineligible = len(shards) - len(eligible)
    if ineligible:
        click.echo(f"Skipping {ineligible} shard(s) whose client has no landing_page_url.")
    if not eligible:
        click.echo("No shards matched.")
        return

    click.echo(f"Retrofitting {len(eligible)} shard(s) (dry_run={dry_run})")
    ctx_cache: dict[str, ClientContext] = {}
    results: list[dict] = []
    for i, row in enumerate(eligible):
        try:
            results.append(
                _retrofit_one(sb, row, landing_by_client[row["client_id"]], ctx_cache, dry_run,
                              bootstrap_caddy=bootstrap_caddy)
            )
        except Exception as exc:
            click.echo(f"  ✗ ERROR: {exc}")
            results.append({"domain": row["domain"], "error": str(exc)[:200]})
        if i < len(eligible) - 1:
            time.sleep(PAUSE_BETWEEN_DOMAINS_S)  # gentle throttle on CF + SSH + LE

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
        click.echo(f"    OK   {r['domain']}")
    for r in errs:
        click.echo(f"    FAIL {r['domain']}: {r['error']}")


if __name__ == "__main__":
    main()
