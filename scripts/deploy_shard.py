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

from lib.bison import export as bison_export
from lib.cloudflare import CloudflareClient
from lib.contabo import ContaboClient
from lib.generate import generate_mailboxes, pick_subdomains
from lib.mailserver import MailserverClient
from lib.state import ShardState, SHARDS_DIR


def _load_env() -> None:
    load_dotenv(override=True)
    required = (
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "CONTABO_CLIENT_ID",
        "CONTABO_CLIENT_SECRET",
        "CONTABO_API_USER",
        "CONTABO_API_PASSWORD",
    )
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


def _step_provision_vps(state: ShardState, domain: str, product_id: str, region: str, image_id: str) -> None:
    if state.is_step_done("provision_vps"):
        return
    click.echo("[2/10] Provisioning Contabo VPS")
    cb = ContaboClient()
    display_name = _mail_hostname(domain)
    ssh_pub_path = Path(os.environ.get("SSH_PUBLIC_KEY_PATH", "~/.ssh/id_ed25519.pub")).expanduser()
    public_key = ssh_pub_path.read_text().strip()
    ssh_key_id = cb.find_or_create_ssh_key(f"coldemail-{domain}", public_key)

    # 1) Check state for an instance ID we already acquired on a prior run
    vps_state = state.get("vps") or {}
    instance_id = vps_state.get("id")

    # 2) If not, look for an orphan instance with the same display name
    if not instance_id:
        existing = cb.find_instance_by_display_name(display_name)
        if existing:
            instance_id = existing.get("instanceId") or existing.get("id")
            click.echo(f"  Found existing Contabo instance {instance_id} (displayName '{display_name}'), reusing")

    # 3) Otherwise create a fresh one
    if not instance_id:
        try:
            inst = cb.create_instance(
                display_name=display_name,
                product_id=product_id,
                region=region,
                ssh_key_id=ssh_key_id,
                image_id=image_id,
            )
        except requests.HTTPError as exc:
            body = exc.response.text if exc.response is not None else str(exc)
            if "not available" in body.lower() or "productid" in body.lower():
                raise click.ClickException(
                    f"Contabo rejected product '{product_id}' in region '{region}': {body}\n"
                    f"Contabo does not expose a list-products API — pick a current productId from\n"
                    f"  https://contabo.com/en/vps/  (current range is roughly V91 through V107)\n"
                    f"and set CONTABO_PRODUCT_ID=<id> in .env (or pass --contabo-product-id <id>), then re-run."
                )
            raise
        instance_id = inst.get("instanceId") or inst.get("id")

    # Save the instance ID immediately so future re-runs can recover even if the next step fails
    state.set("vps", {
        "id": instance_id,
        "ip": vps_state.get("ip"),
        "product_id": product_id,
        "region": region,
    })

    inst = cb.wait_for_instance_ready(instance_id)
    ip = ((inst.get("ipConfig") or {}).get("v4") or {}).get("ip")
    state.set("vps", {
        "id": instance_id,
        "ip": ip,
        "product_id": product_id,
        "region": region,
    })
    cb.wait_for_ssh(ip)
    state.mark_step_done("provision_vps")


def _step_set_ptr(state: ShardState, domain: str) -> None:
    if state.is_step_done("set_ptr"):
        return
    click.echo("[3/10] Setting PTR (reverse DNS)")
    cb = ContaboClient()
    vps = state.get("vps")
    hostname = _mail_hostname(domain)
    cb.set_ptr(vps["id"], hostname)

    deadline = time.time() + 600
    while time.time() < deadline:
        try:
            rev = dns.reversename.from_address(vps["ip"])
            answers = dns.resolver.resolve(rev, "PTR")
            resolved = str(answers[0]).rstrip(".")
            if resolved == hostname:
                state.mark_step_done("set_ptr")
                return
        except Exception:
            pass
        time.sleep(20)
    raise click.ClickException("PTR did not propagate within 10 minutes")


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


def _step_install_mailserver(state: ShardState, domain: str) -> None:
    if state.is_step_done("install_mailserver"):
        return
    click.echo("[5/10] Installing docker-mailserver on VPS")
    vps = state.get("vps")
    le_email = os.environ.get("LE_EMAIL", f"ops@{domain}")
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ssh_user = os.environ.get("SSH_USER", "admin")
    ms = MailserverClient(vps["ip"], ssh_key, user=ssh_user)
    ms.connect()
    try:
        ms.install_docker()
        ms.install_dms(domain, le_email)
    finally:
        ms.close()
    state.mark_step_done("install_mailserver")


def _step_create_mailboxes(state: ShardState) -> None:
    if state.is_step_done("create_mailboxes"):
        return
    click.echo("[6/10] Creating 100 mailboxes")
    vps = state.get("vps")
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ssh_user = os.environ.get("SSH_USER", "admin")
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
    ssh_user = os.environ.get("SSH_USER", "admin")
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
@click.option("--contabo-product-id", default=lambda: os.environ.get("CONTABO_PRODUCT_ID", "V91"),
              help="Contabo product ID. V91 = current cheapest Cloud VPS. Check https://contabo.com/en/vps/ if this errors.")
@click.option("--region", default=lambda: os.environ.get("CONTABO_REGION", "EU"),
              help="Contabo region. EU (Germany), US-central, US-east, US-west, SIN, UK.")
@click.option("--image-id", default=lambda: os.environ.get("CONTABO_IMAGE_ID", "d64d5c6c-9dda-4e38-8174-0ee282474d8a"),
              help="Contabo image ID. Default is Ubuntu 22.04 LTS.")
@click.option("--skip-purchase", is_flag=True, help="Error out if domain isn't already on Cloudflare (don't buy via Registrar)")
@click.option("--yes", "assume_yes", is_flag=True, help="Skip interactive confirmations (e.g. domain purchase)")
def main(domain: str, contabo_product_id: str, region: str, image_id: str, skip_purchase: bool, assume_yes: bool) -> None:
    _load_env()
    state = ShardState(domain)
    zone_id = _step_ensure_domain(state, domain, skip_purchase, assume_yes)
    _step_generate(state, domain)
    _step_provision_vps(state, domain, contabo_product_id, region, image_id)
    _step_set_ptr(state, domain)
    _step_configure_dns(state, domain, zone_id)
    _step_install_mailserver(state, domain)
    _step_create_mailboxes(state)
    _step_setup_dkim(state, domain, zone_id)
    _step_export_bison(state, domain)
    _step_final_summary(state, domain)


if __name__ == "__main__":
    main()
