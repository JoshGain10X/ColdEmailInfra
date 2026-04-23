#!/usr/bin/env python3
"""Provision one cold email shard end-to-end.

Idempotent: re-run after a failure and it resumes from the last completed step.
"""
from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path

import click
import dns.resolver
import dns.reversename
import requests
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib import blocklist
from lib.bison import export as bison_export
from lib.cloudflare import CloudflareClient
from lib.contabo import ContaboClient
from lib.generate import generate_mailboxes, pick_subdomains
from lib.mailserver import MailserverClient
from lib.state import ShardState, SHARDS_DIR
from lib.webdock import WebdockClient


PROVIDER_ENV: dict[str, tuple[str, ...]] = {
    "contabo": (
        "CONTABO_CLIENT_ID",
        "CONTABO_CLIENT_SECRET",
        "CONTABO_API_USER",
        "CONTABO_API_PASSWORD",
    ),
    "webdock": ("WEBDOCK_API_TOKEN",),
}


def _make_vps_client(provider: str):
    if provider == "contabo":
        return ContaboClient()
    if provider == "webdock":
        return WebdockClient()
    raise click.ClickException(f"Unknown provider: {provider!r}")


def _load_env(provider: str) -> None:
    load_dotenv(override=True)
    required: tuple[str, ...] = (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
    ) + PROVIDER_ENV.get(provider, ())
    for key in required:
        if not os.environ.get(key):
            raise click.ClickException(f"Missing env var: {key}")


def _mail_hostname(domain: str) -> str:
    return f"mail.{domain}"


def _step_ensure_domain(state: ShardState, domain: str, skip_purchase: bool, assume_yes: bool) -> str:
    """Ensure the domain is registered and owned on Cloudflare. Returns the zone ID."""
    cf = CloudflareClient()
    zone_id = cf.get_zone_id(domain)
    if zone_id:
        click.echo(f"[0/10] Domain {domain} already on Cloudflare (zone {zone_id})")
        state.set("cloudflare_zone_id", zone_id)
        state.mark_step_done("ensure_domain")
        return zone_id

    if skip_purchase:
        raise click.ClickException(
            f"Domain {domain} is not on Cloudflare and --skip-purchase was set. "
            "Add the domain to Cloudflare manually, then re-run."
        )

    click.echo(f"[0/10] Domain {domain} not yet on Cloudflare. Checking availability...")
    avail = cf.registrar_check_availability(domain)
    if not avail.get("available"):
        raise click.ClickException(
            f"Domain {domain} is not available for registration via Cloudflare Registrar. "
            "If you already own it elsewhere, transfer it to Cloudflare first."
        )

    price = avail.get("price") or avail.get("renewal_price") or avail.get("created_price") or "?"
    click.echo(f"       Available. Cost: {price} for 1 year (at-cost via Cloudflare Registrar).")
    if not assume_yes:
        click.confirm("       Register this domain now?", abort=True)

    click.echo("       Registering...")
    cf.registrar_register(domain, years=1, privacy=True)
    zone_id = cf.wait_for_zone(domain)
    click.echo(f"       Registered. Zone ID: {zone_id}")
    state.set("cloudflare_zone_id", zone_id)
    state.mark_step_done("ensure_domain")
    return zone_id


def _step_generate(state: ShardState, domain: str) -> None:
    if state.is_step_done("generate"):
        return
    click.echo("[1/10] Generating subdomains and mailboxes")
    subs = pick_subdomains(domain)
    seed = secrets.randbits(64)
    mailboxes = generate_mailboxes(domain, subs, seed=seed)
    state.set("subdomains", subs)
    state.set("mailbox_seed", seed)
    state.set("mailboxes", mailboxes)
    state.mark_step_done("generate")


def _step_provision_vps(state: ShardState, domain: str, provider: str, product_id: str, region: str, image_id: str) -> None:
    if state.is_step_done("provision_vps"):
        return
    click.echo(f"[2/10] Provisioning VPS via {provider}")
    vps_client = _make_vps_client(provider)
    display_name = _mail_hostname(domain)
    ssh_pub_path = Path(os.environ.get("SSH_PUBLIC_KEY_PATH", "~/.ssh/id_ed25519.pub")).expanduser()
    public_key = ssh_pub_path.read_text().strip()
    ssh_key_id = vps_client.find_or_create_ssh_key(f"coldemail-{domain}", public_key)

    # Seed instance_id from state or orphan lookup.
    # Guard against provider mismatch: if the stored VPS was created by a
    # different provider (e.g. legacy Contabo state file, no `provider` key
    # at all → treat as contabo), ignore the stale id and start fresh on
    # the current provider. The orphan VPS on the other provider is NOT
    # auto-destroyed — user destroys it manually via the other provider's
    # dashboard, or via `destroy_shard.py` before redeploying.
    vps_state = state.get("vps") or {}
    stored_provider = vps_state.get("provider", "contabo")
    if vps_state.get("id") and stored_provider != provider:
        click.echo(
            f"  state references a {stored_provider} VPS ({vps_state['id']}), "
            f"but this deploy is on {provider}. Ignoring stale entry and starting "
            f"fresh — destroy the {stored_provider} VPS manually if it's still live."
        )
        instance_id = None
    else:
        instance_id = vps_state.get("id")
    if not instance_id:
        existing = vps_client.find_instance_by_display_name(display_name)
        if existing:
            instance_id = existing["id"]
            click.echo(f"  Found existing {provider} instance {instance_id} (name '{display_name}'), reusing")

    blocked_ips_tried: list[dict] = state.get("blocked_ips_tried") or []
    max_attempts = 3
    ip: str | None = None

    for attempt in range(1, max_attempts + 1):
        # Create a fresh instance if we don't have one yet (first iteration,
        # or we destroyed the previous one because its IP was blocklisted)
        if not instance_id:
            try:
                inst = vps_client.create_instance(
                    display_name=display_name,
                    product_id=product_id,
                    region=region,
                    ssh_key_id=ssh_key_id,
                    image_id=image_id,
                )
            except requests.HTTPError as exc:
                body = exc.response.text if exc.response is not None else str(exc)
                if provider == "contabo" and ("not available" in body.lower() or "productid" in body.lower()):
                    raise click.ClickException(
                        f"Contabo rejected product '{product_id}' in region '{region}': {body}\n"
                        f"Contabo does not expose a list-products API — pick a current productId from\n"
                        f"  https://contabo.com/en/vps/  (current range is roughly V91 through V107)\n"
                        f"and set CONTABO_PRODUCT_ID=<id> in .env (or pass --product-id <id>), then re-run."
                    )
                raise
            instance_id = inst["id"]

        # Save state immediately so a crash mid-wait doesn't lose track of the instance
        state.set("vps", {
            "provider": provider,
            "id": instance_id,
            "ip": None,
            "product_id": product_id,
            "region": region,
        })

        inst = vps_client.wait_for_instance_ready(instance_id)
        ip = inst.get("ip")

        # Blocklist gate — Spamhaus Zen, Barracuda, SpamCop
        listed = blocklist.check_ip(ip)
        if not listed:
            break

        # Dirty IP: record, destroy, and loop to reprovision
        click.echo(f"  IP {ip} listed on {', '.join(listed)} — destroying and retrying ({attempt}/{max_attempts})")
        blocked_ips_tried.append({"ip": ip, "listed_on": listed})
        state.set("blocked_ips_tried", blocked_ips_tried)
        try:
            vps_client.destroy_instance(instance_id)
        except Exception as exc:
            click.echo(f"  warning: destroy failed for {instance_id}: {exc}")
        instance_id = None
        ip = None
    else:
        # Loop exited without break = all attempts blocklisted
        summary = "\n".join(
            f"  - {t['ip']} ({', '.join(t['listed_on'])})" for t in blocked_ips_tried
        )
        raise click.ClickException(
            f"All {max_attempts} provisioned {provider} IPs were on a DNSBL:\n{summary}\n"
            f"{provider}'s IP pool may be hot right now; try a different region or retry later."
        )

    # Create a sudoer shell user on the VM with our SSH key attached.
    # No-op on Contabo (admin preconfigured at provision); on Webdock the
    # cloud image ships without any shell user, so this is mandatory.
    ssh_user = vps_client.ensure_ssh_user(instance_id, ssh_key_id)

    # Clean IP obtained; persist final state
    state.set("vps", {
        "provider": provider,
        "id": instance_id,
        "ip": ip,
        "product_id": product_id,
        "region": region,
        "ssh_user": ssh_user,
    })
    if blocked_ips_tried:
        click.echo(f"  Clean IP {ip} obtained after {len(blocked_ips_tried)} blocklisted retries")
    vps_client.wait_for_ssh(ip)
    state.mark_step_done("provision_vps")


def _ssh_user(state: ShardState) -> str:
    """Where to pull the SSH username from for MailserverClient calls.

    Priority: shard state (set during provision) → env override → 'admin'
    default (Contabo's default, also the convention we use on Webdock).
    """
    vps = state.get("vps") or {}
    return vps.get("ssh_user") or os.environ.get("SSH_USER", "admin")


def _step_set_ptr(state: ShardState, domain: str) -> None:
    if state.is_step_done("set_ptr"):
        return
    click.echo("[3/10] Setting PTR (reverse DNS)")
    vps = state.get("vps")
    provider = vps.get("provider", "contabo")
    vps_client = _make_vps_client(provider)
    hostname = _mail_hostname(domain)
    rev = dns.reversename.from_address(vps["ip"])

    # Fast-path 1: public DNS already shows the right PTR (previous run propagated).
    try:
        current = str(dns.resolver.resolve(rev, "PTR")[0]).rstrip(".")
        if current == hostname:
            click.echo(f"  Public PTR already {hostname}, skipping update")
            state.mark_step_done("set_ptr")
            return
    except Exception:
        current = None

    # Fast-path 2: provider already has it on file (previous run pushed but public DNS slow).
    provider_current = vps_client.get_ptr(vps["ip"])
    if provider_current == hostname:
        click.echo(f"  {provider} PTR already {hostname} (public DNS still propagating)")
    else:
        click.echo(f"  Setting PTR to {hostname} via {provider} API")
        vps_client.set_ptr(vps["id"], hostname)

    # Best-effort public DNS report — does not block deploy.
    try:
        public_now = str(dns.resolver.resolve(rev, "PTR")[0]).rstrip(".")
    except Exception:
        public_now = None
    if public_now == hostname:
        click.echo(f"  Public DNS matches: {hostname}")
    else:
        click.echo(
            f"  {provider} has PTR set. Public DNS still shows "
            f"{public_now or '(nothing)'} — will propagate in 30–60 min. "
            f"Does not block deploy."
        )
    state.mark_step_done("set_ptr")


def _step_configure_dns(state: ShardState, domain: str, zone_id: str) -> None:
    if state.is_step_done("configure_dns"):
        return
    click.echo("[4/10] Configuring Cloudflare DNS (pre-DKIM records)")
    cf = CloudflareClient()
    vps_ip = state.get("vps")["ip"]
    subs = state.get("subdomains")
    dmarc_rua = os.environ.get("DMARC_RUA", f"dmarc@{domain}")
    redirect_target = os.environ.get("REDIRECT_TARGET", "https://10xmanagers.com")
    mail_host = _mail_hostname(domain)

    cf.upsert_record(zone_id, "A", domain, vps_ip, proxied=False)
    cf.upsert_record(zone_id, "A", mail_host, vps_ip, proxied=False)
    cf.upsert_record(
        zone_id, "TXT", f"_dmarc.{domain}",
        f"v=DMARC1; p=quarantine; sp=quarantine; rua=mailto:{dmarc_rua}; adkim=r; aspf=r",
    )

    for sub in subs:
        fqdn = f"{sub}.{domain}"
        sub_mail = f"mail.{fqdn}"
        cf.upsert_record(zone_id, "A", fqdn, vps_ip, proxied=False)
        cf.upsert_record(zone_id, "A", sub_mail, vps_ip, proxied=False)
        cf.upsert_record(zone_id, "MX", fqdn, sub_mail, priority=10)
        cf.upsert_record(zone_id, "TXT", fqdn, "v=spf1 a mx -all")
        cf.upsert_record(
            zone_id, "TXT", f"_dmarc.{fqdn}",
            f"v=DMARC1; p=quarantine; rua=mailto:{dmarc_rua}; adkim=r; aspf=r",
        )

    try:
        cf.ensure_redirect_rule(zone_id, domain, redirect_target)
    except RuntimeError as exc:
        click.echo(f"  WARNING: could not auto-create redirect rule ({exc}).")
        click.echo(f"  Add it manually: Cloudflare dashboard -> {domain} -> Rules -> Redirect Rules -> Create Rule")
        click.echo(f"                   Match: Hostname equals '{domain}'  ->  Static redirect 301 -> {redirect_target}")
        click.echo(f"  Cold email does not depend on this; the shard can finish without it.")
    state.mark_step_done("configure_dns")


def _step_install_mailserver(state: ShardState, domain: str, ssl_type: str) -> None:
    click.echo(f"[5/10] Installing docker-mailserver on VPS (ssl_type={ssl_type})")
    vps = state.get("vps")
    le_email = os.environ.get("LE_EMAIL", f"ops@{domain}")
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ssh_user = _ssh_user(state)
    ms = MailserverClient(vps["ip"], ssh_key, user=ssh_user)
    ms.connect()
    try:
        # Self-healing: if the step is already marked done and the container
        # is actually healthy, skip. Otherwise re-run install to fix whatever
        # broke (missing LE cert, bad config, crash loop).
        if state.is_step_done("install_mailserver"):
            _, out, _ = ms.sudo(
                "docker inspect -f '{{.State.Status}} {{.State.Health.Status}}' mailserver",
                check=False,
            )
            if out.strip().endswith(" healthy"):
                click.echo("  mailserver container already healthy, skipping reinstall")
                return
            click.echo("  mailserver container is not healthy — re-running install to fix")
        ms.install_docker()
        ms.install_dms(
            domain,
            le_email,
            os.environ["CLOUDFLARE_API_TOKEN"].strip(),
            ssl_type=ssl_type,
        )
    finally:
        ms.close()
    state.mark_step_done("install_mailserver")


def _step_create_mailboxes(state: ShardState) -> None:
    if state.is_step_done("create_mailboxes"):
        return
    click.echo("[6/10] Creating 100 mailboxes")
    vps = state.get("vps")
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ssh_user = _ssh_user(state)
    ms = MailserverClient(vps["ip"], ssh_key, user=ssh_user)
    ms.connect()
    try:
        # Safety net for resumed runs: step 5 may have marked done while
        # the container was still initialising. Wait for healthy before exec.
        ms.wait_for_mailserver_ready()
        for mb in state.get("mailboxes"):
            ms.add_mailbox(mb["email"], mb["password"])
    finally:
        ms.close()
    state.mark_step_done("create_mailboxes")


def _step_setup_dkim(state: ShardState, domain: str, zone_id: str) -> None:
    if state.is_step_done("setup_dkim"):
        return
    click.echo("[7/10] Generating DKIM keys and publishing to Cloudflare")
    vps = state.get("vps")
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ssh_user = _ssh_user(state)
    cf = CloudflareClient()
    dkim: dict[str, str] = state.get("dkim") or {}

    ms = MailserverClient(vps["ip"], ssh_key, user=ssh_user)
    ms.connect()
    try:
        ms.wait_for_mailserver_ready()
        for sub in state.get("subdomains"):
            fqdn = f"{sub}.{domain}"
            public_key = ms.setup_dkim(fqdn, keysize=2048)
            dkim[fqdn] = public_key
            cf.upsert_record(
                zone_id, "TXT", f"mail._domainkey.{fqdn}",
                public_key,
            )
            state.set("dkim", dkim)
        # OpenDKIM needs a restart to pick up newly-generated keys
        ms.restart_mailserver()
    finally:
        ms.close()
    state.mark_step_done("setup_dkim")


def _step_export_bison(state: ShardState, domain: str) -> None:
    if state.is_step_done("export_bison"):
        return
    click.echo("[8/10] Exporting Email Bison CSV")
    out_path = SHARDS_DIR / f"{domain}_bison.csv"
    bison_export(state.get("mailboxes"), _mail_hostname(domain), out_path)
    state.set("bison_csv", str(out_path))
    state.mark_step_done("export_bison")


def _step_final_summary(state: ShardState, domain: str) -> None:
    click.echo("[10/10] Deploy complete")
    click.echo(f"  VPS IP          : {state.get('vps')['ip']}")
    click.echo(f"  Mail hostname   : {_mail_hostname(domain)}")
    click.echo(f"  Subdomains      : {len(state.get('subdomains'))}")
    click.echo(f"  Mailboxes       : {len(state.get('mailboxes'))}")
    click.echo(f"  Bison CSV       : {state.get('bison_csv')}")
    click.echo(f"  State file      : {state.path}")
    click.echo("")
    click.echo("Next: ./scripts/verify_shard.py --domain {0}".format(domain))


@click.command()
@click.option("--domain", required=True, help="Root domain, e.g. example.co.uk (purchased via CF Registrar if not already yours)")
@click.option("--provider", type=click.Choice(["contabo", "webdock"]),
              default=lambda: os.environ.get("DEFAULT_PROVIDER", "webdock"),
              help="VPS provider. Webdock is pay-per-hour with credit refunded on destroy; "
                   "Contabo is month-prepaid with no refund. Defaults to DEFAULT_PROVIDER env var or 'webdock'.")
@click.option("--product-id", default=None,
              help="Provider product slug. Contabo: productId like 'V91'. "
                   "Webdock: profileSlug like 'webdocknano'. "
                   "Defaults to CONTABO_PRODUCT_ID / WEBDOCK_PROFILE_SLUG env var.")
@click.option("--region", default=None,
              help="Provider region/location. Contabo: 'EU', 'US-central', 'UK', etc. "
                   "Webdock: 'fi', 'nl', 'uk', 'us' (locationId). "
                   "Defaults to CONTABO_REGION / WEBDOCK_LOCATION_ID env var.")
@click.option("--image-id", default=None,
              help="Provider image. Contabo: UUID. Webdock: slug like 'ubuntu-jammy-cloud'. "
                   "Defaults to CONTABO_IMAGE_ID / WEBDOCK_IMAGE_SLUG env var.")
@click.option("--skip-purchase", is_flag=True, help="Error out if domain isn't already on Cloudflare (don't buy via Registrar)")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip interactive confirmations (e.g. domain purchase)")
@click.option("--ssl-type", type=click.Choice(["self-signed", "letsencrypt"]),
              default=lambda: os.environ.get("SSL_TYPE", "self-signed"),
              help="TLS cert strategy. 'self-signed' = docker-mailserver uses a cert we generate (default, Bison skips verification). 'letsencrypt' = real cert via Cloudflare DNS-01 challenge.")
def main(
    domain: str,
    provider: str,
    product_id: str | None,
    region: str | None,
    image_id: str | None,
    skip_purchase: bool,
    assume_yes: bool,
    ssl_type: str,
) -> None:
    _load_env(provider)
    if provider == "contabo":
        product_id = product_id or os.environ.get("CONTABO_PRODUCT_ID", "V91")
        region = region or os.environ.get("CONTABO_REGION", "EU")
        image_id = image_id or os.environ.get("CONTABO_IMAGE_ID", "d64d5c6c-9dda-4e38-8174-0ee282474d8a")
    else:  # webdock
        product_id = product_id or os.environ.get("WEBDOCK_PROFILE_SLUG")
        region = region or os.environ.get("WEBDOCK_LOCATION_ID")
        image_id = image_id or os.environ.get("WEBDOCK_IMAGE_SLUG")
        missing = [n for n, v in (("profileSlug", product_id), ("locationId", region), ("imageSlug", image_id)) if not v]
        if missing:
            raise click.ClickException(
                f"Webdock needs {', '.join(missing)} — set WEBDOCK_PROFILE_SLUG / "
                f"WEBDOCK_LOCATION_ID / WEBDOCK_IMAGE_SLUG in .env, or pass "
                f"--product-id / --region / --image-id. Run `python3 scripts/webdock_discover.py` "
                f"to list available values."
            )

    state = ShardState(domain)
    zone_id = _step_ensure_domain(state, domain, skip_purchase, assume_yes)
    _step_generate(state, domain)
    _step_provision_vps(state, domain, provider, product_id, region, image_id)
    _step_set_ptr(state, domain)
    _step_configure_dns(state, domain, zone_id)
    _step_install_mailserver(state, domain, ssl_type)
    _step_create_mailboxes(state)
    _step_setup_dkim(state, domain, zone_id)
    _step_export_bison(state, domain)
    _step_final_summary(state, domain)


if __name__ == "__main__":
    main()
