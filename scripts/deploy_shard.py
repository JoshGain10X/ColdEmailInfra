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
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.bison import export as bison_export
from lib.cloudflare import CloudflareClient
from lib.generate import generate_mailboxes, pick_subdomains
from lib.mailcheap import MailcheapClient
from lib.mailserver import MailserverClient
from lib.state import ShardState, SHARDS_DIR


def _load_env() -> None:
    load_dotenv()
    for required in ("CLOUDFLARE_API_TOKEN", "MAILCHEAP_API_KEY"):
        if not os.environ.get(required):
            raise click.ClickException(f"Missing env var: {required}")


def _mail_hostname(domain: str) -> str:
    return f"mail.{domain}"


def _step_generate(state: ShardState, domain: str) -> None:
    if state.is_step_done("generate"):
        return
    click.echo("[1/9] Generating subdomains and mailboxes")
    subs = pick_subdomains(domain)
    seed = secrets.randbits(64)
    mailboxes = generate_mailboxes(domain, subs, seed=seed)
    state.set("subdomains", subs)
    state.set("mailbox_seed", seed)
    state.set("mailboxes", mailboxes)
    state.mark_step_done("generate")


def _step_provision_vps(state: ShardState, domain: str, plan: str, region: str) -> None:
    if state.is_step_done("provision_vps"):
        return
    click.echo("[2/9] Provisioning Mailcheap VPS")
    mc = MailcheapClient()
    ssh_pub_path = Path(os.environ.get("SSH_PUBLIC_KEY_PATH", "~/.ssh/id_ed25519.pub")).expanduser()
    public_key = ssh_pub_path.read_text().strip()
    ssh_key_id = mc.find_or_create_ssh_key(f"coldemail-{domain}", public_key)
    vps = mc.create_vps(
        hostname=_mail_hostname(domain),
        plan=plan,
        region=region,
        ssh_key_id=ssh_key_id,
    )
    vps = mc.wait_for_vps_ready(vps["id"])
    state.set("vps", {"id": vps["id"], "ip": vps["ipv4_address"], "plan": plan, "region": region})
    mc.wait_for_ssh(vps["ipv4_address"])
    state.mark_step_done("provision_vps")


def _step_set_ptr(state: ShardState, domain: str) -> None:
    if state.is_step_done("set_ptr"):
        return
    click.echo("[3/9] Setting PTR (reverse DNS)")
    mc = MailcheapClient()
    vps = state.get("vps")
    hostname = _mail_hostname(domain)
    mc.set_ptr(vps["id"], hostname)

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
    click.echo("[4/9] Configuring Cloudflare DNS (pre-DKIM records)")
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

    cf.ensure_redirect_rule(zone_id, domain, redirect_target)
    state.mark_step_done("configure_dns")


def _step_install_mailserver(state: ShardState, domain: str) -> None:
    if state.is_step_done("install_mailserver"):
        return
    click.echo("[5/9] Installing docker-mailserver on VPS")
    vps = state.get("vps")
    le_email = os.environ.get("LE_EMAIL", f"ops@{domain}")
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ms = MailserverClient(vps["ip"], ssh_key)
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
    click.echo("[6/9] Creating 100 mailboxes")
    vps = state.get("vps")
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ms = MailserverClient(vps["ip"], ssh_key)
    ms.connect()
    try:
        for mb in state.get("mailboxes"):
            ms.add_mailbox(mb["email"], mb["password"])
    finally:
        ms.close()
    state.mark_step_done("create_mailboxes")


def _step_setup_dkim(state: ShardState, domain: str, zone_id: str) -> None:
    if state.is_step_done("setup_dkim"):
        return
    click.echo("[7/9] Generating DKIM keys and publishing to Cloudflare")
    vps = state.get("vps")
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    cf = CloudflareClient()
    dkim: dict[str, str] = state.get("dkim") or {}

    ms = MailserverClient(vps["ip"], ssh_key)
    ms.connect()
    try:
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
        ms.run("cd /opt/mailserver && docker compose restart mailserver")
    finally:
        ms.close()
    state.mark_step_done("setup_dkim")


def _step_export_bison(state: ShardState, domain: str) -> None:
    if state.is_step_done("export_bison"):
        return
    click.echo("[8/9] Exporting Email Bison CSV")
    out_path = SHARDS_DIR / f"{domain}_bison.csv"
    bison_export(state.get("mailboxes"), _mail_hostname(domain), out_path)
    state.set("bison_csv", str(out_path))
    state.mark_step_done("export_bison")


def _step_final_summary(state: ShardState, domain: str) -> None:
    click.echo("[9/9] Deploy complete")
    click.echo(f"  VPS IP          : {state.get('vps')['ip']}")
    click.echo(f"  Mail hostname   : {_mail_hostname(domain)}")
    click.echo(f"  Subdomains      : {len(state.get('subdomains'))}")
    click.echo(f"  Mailboxes       : {len(state.get('mailboxes'))}")
    click.echo(f"  Bison CSV       : {state.get('bison_csv')}")
    click.echo(f"  State file      : {state.path}")
    click.echo("")
    click.echo("Next: ./scripts/verify_shard.py --domain {0}".format(domain))


@click.command()
@click.option("--domain", required=True, help="Root pre-warmed domain, e.g. example.co.uk")
@click.option("--cloudflare-zone-id", required=True, envvar="CLOUDFLARE_ZONE_ID")
@click.option("--mailcheap-plan", default="vps-starter")
@click.option("--region", default="uk-lon")
def main(domain: str, cloudflare_zone_id: str, mailcheap_plan: str, region: str) -> None:
    _load_env()
    state = ShardState(domain)
    _step_generate(state, domain)
    _step_provision_vps(state, domain, mailcheap_plan, region)
    _step_set_ptr(state, domain)
    _step_configure_dns(state, domain, cloudflare_zone_id)
    _step_install_mailserver(state, domain)
    _step_create_mailboxes(state)
    _step_setup_dkim(state, domain, cloudflare_zone_id)
    _step_export_bison(state, domain)
    _step_final_summary(state, domain)


if __name__ == "__main__":
    main()
