"""Background job runner with Supabase progress tracking.

Each public function (run_deploy, run_destroy, run_verify, run_load_to_bison)
is designed to be called from a FastAPI BackgroundTask. It:
  1. Updates the infra_jobs row in Supabase as each step completes
  2. Calls into the existing scripts/lib/ modules directly
  3. Upserts the infra_shards row on completion
"""
from __future__ import annotations

import csv
import json
import os
import secrets
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from supabase import create_client, Client

# ---------------------------------------------------------------------------
# Ensure scripts/lib is importable
# ---------------------------------------------------------------------------
import sys

_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import blocklist
from lib.bison import export as bison_export
from lib.client_context import (
    ClientContext,
    load_client_context_by_id,
    load_client_context_for_shard,
)
from lib.cloudflare import CloudflareClient
from lib.contabo import ContaboClient
from lib.generate import generate_mailboxes, pick_subdomains
from lib.mailserver import MailserverClient
from lib.state import ShardState, SHARDS_DIR
from lib.webdock import WebdockClient

import dns.resolver
import dns.reversename


# ---------------------------------------------------------------------------
# Supabase helpers
# ---------------------------------------------------------------------------

def _supabase() -> Client:
    url = os.environ["SUPABASE_URL"]
    key = os.environ["SUPABASE_SERVICE_KEY"]
    return create_client(url, key)


def _log_entry(message: str) -> dict:
    return {"ts": datetime.now(timezone.utc).isoformat(), "msg": message}


def _update_job(sb: Client, job_id: str, **fields: Any) -> None:
    sb.table("infra_jobs").update(fields).eq("id", job_id).execute()


def _append_log(sb: Client, job_id: str, message: str, step: int | None = None) -> None:
    """Append a log line and optionally bump progress_step."""
    row = sb.table("infra_jobs").select("logs").eq("id", job_id).single().execute()
    logs = row.data.get("logs", []) if row.data else []
    logs.append(_log_entry(message))
    update: dict[str, Any] = {"logs": logs}
    if step is not None:
        update["progress_step"] = step
    _update_job(sb, job_id, **update)


def _complete_job(sb: Client, job_id: str, error: str | None = None) -> None:
    status = "failed" if error else "completed"
    update: dict[str, Any] = {
        "status": status,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    if error:
        update["error"] = error
    # Set progress to total on success
    if not error:
        row = sb.table("infra_jobs").select("progress_total").eq("id", job_id).single().execute()
        if row.data:
            update["progress_step"] = row.data["progress_total"]
    _update_job(sb, job_id, **update)


def _upsert_shard(sb: Client, domain: str, **fields: Any) -> None:
    """Insert or update the infra_shards row for (client_id, domain).

    Multi-tenant note: pass client_id as a keyword arg to scope the lookup.
    Without client_id this falls back to legacy single-tenant behaviour
    (matches any shard with this domain), which is safe only when running
    against a single-client database.
    """
    client_id = fields.get("client_id")
    query = sb.table("infra_shards").select("id").eq("domain", domain)
    if client_id:
        query = query.eq("client_id", client_id)
    existing = query.execute()

    if existing.data:
        # Don't try to overwrite client_id on update — it's the primary key
        update_fields = {k: v for k, v in fields.items() if k != "client_id"}
        update_query = sb.table("infra_shards").update(update_fields).eq("domain", domain)
        if client_id:
            update_query = update_query.eq("client_id", client_id)
        update_query.execute()
    else:
        sb.table("infra_shards").insert({"domain": domain, **fields}).execute()


# ---------------------------------------------------------------------------
# VPS client factory (mirrors deploy_shard.py)
# ---------------------------------------------------------------------------

def _make_vps_client(provider: str):
    if provider == "contabo":
        return ContaboClient()
    if provider == "webdock":
        return WebdockClient()
    raise ValueError(f"Unknown provider: {provider!r}")


def _mail_hostname(domain: str) -> str:
    return f"mail.{domain}"


def _ssh_user(state: ShardState) -> str:
    vps = state.get("vps") or {}
    return vps.get("ssh_user") or os.environ.get("SSH_USER", "admin")


# ---------------------------------------------------------------------------
# DEPLOY
# ---------------------------------------------------------------------------

def run_deploy(
    job_id: str,
    client_id: str,
    domain: str,
    provider: str = "webdock",
    product_id: str | None = None,
    region: str | None = None,
    image_id: str | None = None,
    ssl_type: str | None = None,
) -> None:
    """Full shard deployment — called as a background task.

    Multi-tenant: client_id is required. Webdock + Cloudflare credentials,
    redirect URL, DMARC RUA, LE email, mailbox count, subdomain count,
    VPS plan/region/image — all resolved from the client's ClientContext
    rather than process env vars. Per-call request overrides still win
    (passed-in product_id/region/image_id/ssl_type override client defaults).
    """
    load_dotenv()  # process env wins (set via docker --env-file)
    sb = _supabase()
    _update_job(sb, job_id, status="running")

    try:
        ctx = load_client_context_by_id(client_id)

        # Apply client-default fallbacks for any unspecified deploy params
        product_id = product_id or ctx.vps_plan
        region = region or ctx.vps_region
        image_id = image_id or ctx.vps_image_slug
        ssl_type = ssl_type or ctx.ssl_type

        # Friendly error if the client's Webdock isn't configured yet
        if provider == "webdock" and not ctx.webdock:
            raise RuntimeError(
                f"Client {ctx.slug!r} has no Webdock token configured "
                "(client_credentials.webdock_api_token_secret_id is NULL). "
                "Add it to Vault and link via client_credentials before deploying."
            )

        # Initial shard row — mailbox_count + bison_loaded get their real values
        # later in the deploy. Pass mailbox_count up front to satisfy NOT NULL
        # in case the column default isn't set on the target schema.
        _upsert_shard(sb, domain, client_id=client_id, status="deploying",
                      provider=provider, region=region,
                      mailbox_count=ctx.mailbox_count)

        state = ShardState(domain)

        # Step 0: Ensure domain
        _append_log(sb, job_id, "Ensuring domain on Cloudflare", step=0)
        cf = ctx.cloudflare
        zone_id = cf.get_zone_id(domain)
        if not zone_id:
            _append_log(sb, job_id, "Domain not on Cloudflare, registering...")
            avail = cf.registrar_check_availability(domain)
            if not avail.get("available"):
                raise RuntimeError(f"Domain {domain} not available for registration")
            cf.registrar_register(domain, years=1, privacy=True)
            zone_id = cf.wait_for_zone(domain)
        state.set("cloudflare_zone_id", zone_id)
        state.mark_step_done("ensure_domain")
        _append_log(sb, job_id, f"Domain ready, zone_id={zone_id}", step=1)

        # Step 1: Generate subdomains & mailboxes
        # Subdomain count is random 6-8 per shard, sampled from the client's
        # subdomain_pool (NULL → legacy 20-item generic list).
        # Per-subdomain mailbox count is random 5-15, summing to ctx.mailbox_count.
        # Two shards from the same client never have an identical layout.
        if not state.is_step_done("generate"):
            _append_log(sb, job_id, "Generating subdomains and mailboxes")
            seed = secrets.randbits(64)
            subs = pick_subdomains(domain, seed=seed, pool=ctx.subdomain_pool)
            mailboxes = generate_mailboxes(
                domain, subs, seed=seed,
                local_parts=ctx.mailbox_local_parts,
                display_first_name=ctx.mailbox_display_first_name,
                display_last_name=ctx.mailbox_display_last_name,
                mailbox_count_total=ctx.mailbox_count,
            )
            state.set("subdomains", subs)
            state.set("mailbox_seed", seed)
            state.set("mailboxes", mailboxes)
            state.mark_step_done("generate")
        mailbox_mode = "single-persona" if ctx.mailbox_local_parts else "multi-persona"
        _append_log(sb, job_id,
            f"Generated {len(state.get('subdomains'))} subdomains "
            f"({', '.join(state.get('subdomains'))}), "
            f"{len(state.get('mailboxes'))} mailboxes ({mailbox_mode})", step=2)

        # Step 2: Provision VPS
        if not state.is_step_done("provision_vps"):
            _append_log(sb, job_id, f"Provisioning VPS via {provider}")
            vps_client = ctx.webdock if provider == "webdock" else _make_vps_client(provider)
            display_name = _mail_hostname(domain)
            ssh_pub_path = Path(os.environ.get("SSH_PUBLIC_KEY_PATH", "~/.ssh/id_ed25519.pub")).expanduser()
            public_key = ssh_pub_path.read_text().strip()
            ssh_key_id = vps_client.find_or_create_ssh_key(f"coldemail-{domain}", public_key)

            vps_state = state.get("vps") or {}
            stored_provider = vps_state.get("provider", "contabo")
            if vps_state.get("id") and stored_provider != provider:
                instance_id = None
            else:
                instance_id = vps_state.get("id")
            if not instance_id:
                existing = vps_client.find_instance_by_display_name(display_name)
                if existing:
                    instance_id = existing["id"]

            if not instance_id:
                inst = vps_client.create_instance(
                    display_name=display_name,
                    product_id=product_id,
                    region=region,
                    ssh_key_id=ssh_key_id,
                    image_id=image_id,
                )
                instance_id = inst["id"]

            state.set("vps", {
                "provider": provider, "id": instance_id,
                "ip": None, "product_id": product_id, "region": region,
            })

            inst = vps_client.wait_for_instance_ready(instance_id)
            ip = inst.get("ip")
            # Per-client blocklist webhook — skips silently if the client
            # hasn't configured one (no leaks to other clients' monitoring).
            blocklist.notify_check_ip(ip, ctx.blocklist_webhook_url)
            ssh_credentials = vps_client.ensure_ssh_user(instance_id, ssh_key_id)

            state.set("vps", {
                "provider": provider, "id": instance_id, "ip": ip,
                "product_id": product_id, "region": region,
                "ssh_user": ssh_credentials["username"],
                "ssh_bootstrap_password": ssh_credentials.get("password"),
            })
            vps_client.wait_for_ssh(ip)
            state.mark_step_done("provision_vps")
        vps_ip = state.get("vps")["ip"]
        _upsert_shard(sb, domain, client_id=client_id, vps_ip=vps_ip)
        _append_log(sb, job_id, f"VPS ready at {vps_ip}", step=3)

        # Step 3: Set PTR
        if not state.is_step_done("set_ptr"):
            _append_log(sb, job_id, "Setting PTR (reverse DNS)")
            vps = state.get("vps")
            vps_provider = vps.get("provider", provider)
            vps_client = ctx.webdock if vps_provider == "webdock" else _make_vps_client(vps_provider)
            hostname = _mail_hostname(domain)
            try:
                vps_client.set_ptr(vps["id"], hostname)
            except Exception as exc:
                _append_log(sb, job_id, f"PTR warning: {exc} (non-blocking)")
            state.mark_step_done("set_ptr")
        _append_log(sb, job_id, "PTR configured", step=4)

        # Step 4: Configure DNS
        if not state.is_step_done("configure_dns"):
            _append_log(sb, job_id, "Configuring Cloudflare DNS")
            vps_ip = state.get("vps")["ip"]
            subs = state.get("subdomains")
            dmarc_rua = ctx.dmarc_rua or f"dmarc@{domain}"
            redirect_target = ctx.redirect_url or f"https://{domain}"
            mail_host = _mail_hostname(domain)

            cf.upsert_record(zone_id, "A", domain, vps_ip, proxied=True)
            cf.upsert_record(zone_id, "A", mail_host, vps_ip, proxied=False)
            cf.upsert_record(
                zone_id, "TXT", f"_dmarc.{domain}",
                f"v=DMARC1; p=quarantine; sp=quarantine; rua=mailto:{dmarc_rua}; adkim=r; aspf=r",
            )

            # MTA-STS + TLS-RPT records.
            # mta-sts.<domain> A → VPS IP (unproxied so Let's Encrypt ACME
            # challenge can reach Caddy on port 80/443 during cert issuance).
            # _mta-sts.<domain> TXT carries the policy version/id.
            # _smtp._tls.<domain> TXT enables TLS reporting (RFC 8460).
            cf.upsert_record(zone_id, "A", f"mta-sts.{domain}", vps_ip, proxied=False)
            mta_sts_id = int(datetime.now(timezone.utc).timestamp())
            cf.upsert_record(
                zone_id, "TXT", f"_mta-sts.{domain}",
                f"v=STSv1; id={mta_sts_id};",
            )
            cf.upsert_record(
                zone_id, "TXT", f"_smtp._tls.{domain}",
                f"v=TLSRPTv1; rua=mailto:tls-rpt@{domain}",
            )
            for sub in subs:
                fqdn = f"{sub}.{domain}"
                sub_mail = f"mail.{fqdn}"
                cf.upsert_record(zone_id, "A", fqdn, vps_ip, proxied=(sub != "mail"))
                cf.upsert_record(zone_id, "A", sub_mail, vps_ip, proxied=False)
                cf.upsert_record(zone_id, "MX", fqdn, sub_mail, priority=10)
                cf.upsert_record(zone_id, "TXT", fqdn, f"v=spf1 ip4:{vps_ip} mx -all")
                cf.upsert_record(
                    zone_id, "TXT", f"_dmarc.{fqdn}",
                    f"v=DMARC1; p=quarantine; rua=mailto:{dmarc_rua}; adkim=r; aspf=r",
                )
            try:
                cf.ensure_redirect_rule(zone_id, domain, redirect_target)
            except RuntimeError:
                _append_log(sb, job_id, "Redirect rule warning: add manually in Cloudflare")
            state.mark_step_done("configure_dns")
        _append_log(sb, job_id, "DNS configured", step=5)

        # Step 5: Install mailserver
        _append_log(sb, job_id, f"Installing docker-mailserver (ssl={ssl_type})")
        vps = state.get("vps")
        le_email = ctx.le_email or f"ops@{domain}"
        ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
        ssh_user = _ssh_user(state)
        ms = MailserverClient(vps["ip"], ssh_key, user=ssh_user)
        ms.connect()
        try:
            bootstrap_pw = vps.get("ssh_bootstrap_password")
            if bootstrap_pw:
                ms.bootstrap_passwordless_sudo(bootstrap_pw)
                vps_cleaned = {k: v for k, v in vps.items() if k != "ssh_bootstrap_password"}
                state.set("vps", vps_cleaned)
            if state.is_step_done("install_mailserver"):
                _, out, _ = ms.sudo(
                    "docker inspect -f '{{.State.Status}} {{.State.Health.Status}}' mailserver",
                    check=False,
                )
                if not out.strip().endswith(" healthy"):
                    ms.install_docker()
                    ms.install_dms(domain, le_email, ctx.cloudflare.token, ssl_type=ssl_type)
            else:
                ms.install_docker()
                ms.install_dms(domain, le_email, ctx.cloudflare.token, ssl_type=ssl_type)
        finally:
            ms.close()
        state.mark_step_done("install_mailserver")
        _append_log(sb, job_id, "Mailserver installed", step=6)

        # Step 6: Create mailboxes
        if not state.is_step_done("create_mailboxes"):
            _append_log(sb, job_id, "Creating 100 mailboxes")
            ms = MailserverClient(vps["ip"], ssh_key, user=ssh_user)
            ms.connect()
            try:
                ms.wait_for_mailserver_ready()
                for mb in state.get("mailboxes"):
                    ms.add_mailbox(mb["email"], mb["password"])
            finally:
                ms.close()
            state.mark_step_done("create_mailboxes")
        _append_log(sb, job_id, "Mailboxes created", step=7)

        # Step 6.5: Install MTA-STS policy server (Caddy on the mail VPS)
        # Runs after mailserver install + before DKIM so the policy URL is
        # live before any outbound mail starts. Caddy auto-acquires the LE
        # cert for mta-sts.<domain> — the DNS A record was set in step 5.
        if not state.is_step_done("install_mta_sts"):
            _append_log(sb, job_id, "Installing MTA-STS policy server (Caddy)")
            ms = MailserverClient(vps["ip"], ssh_key, user=ssh_user)
            ms.connect()
            try:
                ms.install_mta_sts(domain, mode="testing")
            finally:
                ms.close()
            state.mark_step_done("install_mta_sts")
        _append_log(sb, job_id, "MTA-STS + TLS-RPT live")

        # Step 7: Setup DKIM
        if not state.is_step_done("setup_dkim"):
            _append_log(sb, job_id, "Generating DKIM keys")
            dkim: dict[str, str] = state.get("dkim") or {}
            ms = MailserverClient(vps["ip"], ssh_key, user=ssh_user)
            ms.connect()
            try:
                ms.wait_for_mailserver_ready()
                for sub in state.get("subdomains"):
                    fqdn = f"{sub}.{domain}"
                    public_key = ms.setup_dkim(fqdn, keysize=2048)
                    dkim[fqdn] = public_key
                    cf.upsert_record(zone_id, "TXT", f"mail._domainkey.{fqdn}", public_key)
                    state.set("dkim", dkim)
                ms.restart_mailserver()
            finally:
                ms.close()
            state.mark_step_done("setup_dkim")
        _append_log(sb, job_id, "DKIM configured", step=8)

        # Step 8: Export Bison CSV
        if not state.is_step_done("export_bison"):
            _append_log(sb, job_id, "Exporting Bison CSV")
            out_path = SHARDS_DIR / f"{domain}_bison.csv"
            bison_export(state.get("mailboxes"), _mail_hostname(domain), out_path)
            state.set("bison_csv", str(out_path))
            state.mark_step_done("export_bison")
        _append_log(sb, job_id, "Bison CSV exported", step=9)

        # Upload CSV to Supabase Storage, namespaced by client slug so two
        # clients can hold the same domain without colliding in the bucket.
        # Matches the path written by run_load_to_bison so both ends agree.
        # Non-blocking — failures here just leave csv_storage_path NULL.
        csv_storage_path = None
        try:
            csv_file = SHARDS_DIR / f"{domain}_bison.csv"
            storage_key = f"{ctx.slug}/{domain}.csv"
            with open(csv_file, "rb") as fh:
                sb.storage.from_("shard-csvs").upload(
                    storage_key, fh.read(),
                    file_options={"content-type": "text/csv", "upsert": "true"},
                )
            csv_storage_path = storage_key
            _append_log(sb, job_id, f"CSV uploaded to storage: {storage_key}")
        except Exception as upload_exc:
            _append_log(sb, job_id, f"CSV storage upload warning: {upload_exc}")

        # Update shard status
        step_flags = state.data.get("steps", {})
        _upsert_shard(sb, domain,
            client_id=client_id,
            status="active",
            mailbox_count=len(state.get("mailboxes", [])),
            step_flags=step_flags,
            csv_storage_path=csv_storage_path,
        )

        _append_log(sb, job_id, "Deploy complete", step=10)
        _complete_job(sb, job_id)

    except Exception as exc:
        _append_log(sb, job_id, f"FAILED: {exc}")
        _complete_job(sb, job_id, error=f"{type(exc).__name__}: {exc}")
        _upsert_shard(sb, domain, client_id=client_id, status="failed")


# ---------------------------------------------------------------------------
# DESTROY
# ---------------------------------------------------------------------------

def run_destroy(job_id: str, client_id: str, domain: str) -> None:
    """Destroy a shard — called as a background task.

    client_id is now required: it determines which Webdock account holds the
    VPS and which Cloudflare account holds the zone. The two are looked up
    via ClientContext at the top so the per-step code stays unchanged in
    shape.
    """
    import shutil
    import time

    load_dotenv()  # process env wins (set via docker --env-file)
    sb = _supabase()
    _update_job(sb, job_id, status="running")

    try:
        ctx = load_client_context_by_id(client_id)
        state = ShardState(domain)
        if not state.path.exists():
            raise RuntimeError(f"No state file for {domain}")

        _upsert_shard(sb, domain, client_id=client_id, status="destroying")

        # Step 1: Destroy VPS
        # Distinguish "already gone" (404 / not found) from "really failed".
        # The old behaviour swallowed both as a warning and let the job continue,
        # which left orphan VPSes billing forever (~£3.70/mo each on Webdock).
        _append_log(sb, job_id, "Destroying VPS", step=1)
        vps = state.get("vps")
        if vps and vps.get("id"):
            provider = vps.get("provider", "webdock")
            try:
                if provider == "webdock":
                    if not ctx.webdock:
                        raise RuntimeError(
                            f"Client {ctx.slug!r} has no Webdock credentials configured — "
                            "cannot destroy a Webdock VPS. Add to client_credentials."
                        )
                    ctx.webdock.destroy_instance(vps["id"])
                else:
                    _make_vps_client(provider).destroy_instance(vps["id"])
                _append_log(sb, job_id, f"VPS {vps['id']} deletion requested")
            except Exception as exc:
                msg = str(exc).lower()
                already_gone = "404" in msg or "not found" in msg or "does not exist" in msg
                if already_gone:
                    _append_log(sb, job_id, f"VPS {vps['id']} already gone — treating as success")
                else:
                    # Hard fail: leave the shard in 'destroying' so the operator
                    # knows there's an orphan VPS to chase. Re-running destroy
                    # is safe (idempotent on DNS + state archival).
                    _append_log(sb, job_id, f"VPS DESTROY FAILED: {exc}")
                    _append_log(sb, job_id,
                        "Shard will NOT be marked destroyed. The Webdock VPS is "
                        "likely still running and billing. Investigate in the Webdock "
                        "dashboard or re-run destroy after fixing the underlying issue.")
                    raise

        # Step 2: Delete DNS records (use the client's Cloudflare account)
        _append_log(sb, job_id, "Deleting DNS records", step=2)
        cf = ctx.cloudflare
        zone_id = state.get("cloudflare_zone_id") or cf.get_zone_id(domain)
        if zone_id:
            removed = cf.delete_all_records(zone_id)
            _append_log(sb, job_id, f"Removed {removed} DNS records")
            removed_rules = cf.delete_redirect_rules(zone_id, domain)
            _append_log(sb, job_id, f"Removed {removed_rules} redirect rules")

        # Step 3: Archive state
        _append_log(sb, job_id, "Archiving state files", step=3)
        archive_dir = SHARDS_DIR / "archived"
        archive_dir.mkdir(exist_ok=True)
        timestamp = time.strftime("%Y%m%dT%H%M%S")
        target = archive_dir / f"{domain}-{timestamp}.json"
        shutil.move(str(state.path), target)
        bison_csv = SHARDS_DIR / f"{domain}_bison.csv"
        if bison_csv.exists():
            shutil.move(str(bison_csv), archive_dir / f"{domain}-{timestamp}_bison.csv")

        _upsert_shard(sb, domain,
            client_id=client_id,
            status="destroyed",
            destroyed_at=datetime.now(timezone.utc).isoformat(),
        )
        _append_log(sb, job_id, "Shard destroyed and archived", step=4)
        _complete_job(sb, job_id)

    except Exception as exc:
        _append_log(sb, job_id, f"FAILED: {exc}")
        _complete_job(sb, job_id, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# VERIFY
# ---------------------------------------------------------------------------

def run_verify(job_id: str, client_id: str, domain: str) -> None:
    """Run verification checks — called as a background task.

    Verification itself uses no per-client credentials (DNS/SMTP/TLS probes
    are public), but client_id is loaded so the shard row stays scoped to
    the right tenant on update.
    """
    load_dotenv()  # process env wins (set via docker --env-file)
    sb = _supabase()
    _update_job(sb, job_id, status="running")

    try:
        ctx = load_client_context_by_id(client_id)
        state = ShardState(domain)
        if not state.is_step_done("setup_dkim"):
            raise RuntimeError(f"Shard {domain} has not completed DKIM step")

        vps_ip = state.get("vps")["ip"]
        sample_subs = state.get("subdomains")[:3]
        all_passed = True

        # DNS checks
        _append_log(sb, job_id, "Checking DNS records (3 sample subdomains)", step=1)
        for sub in sample_subs:
            fqdn = f"{sub}.{domain}"
            checks = []
            try:
                a = [str(rr) for rr in dns.resolver.resolve(fqdn, "A")]
            except Exception:
                a = []
            a_ok = (vps_ip in a) if sub == "mail" else len(a) > 0
            if not a_ok:
                all_passed = False
            checks.append(f"A={'PASS' if a_ok else 'FAIL'}")

            try:
                mx = [str(rr) for rr in dns.resolver.resolve(fqdn, "MX")]
            except Exception:
                mx = []
            mx_ok = any(f"mail.{fqdn}" in m for m in mx)
            if not mx_ok:
                all_passed = False
            checks.append(f"MX={'PASS' if mx_ok else 'FAIL'}")

            try:
                txt = [str(rr).strip('"') for rr in dns.resolver.resolve(fqdn, "TXT")]
            except Exception:
                txt = []
            spf_ok = any("v=spf1" in t for t in txt)
            if not spf_ok:
                all_passed = False
            checks.append(f"SPF={'PASS' if spf_ok else 'FAIL'}")

            try:
                dmarc = [str(rr).strip('"') for rr in dns.resolver.resolve(f"_dmarc.{fqdn}", "TXT")]
            except Exception:
                dmarc = []
            dmarc_ok = any("v=DMARC1" in d for d in dmarc)
            if not dmarc_ok:
                all_passed = False
            checks.append(f"DMARC={'PASS' if dmarc_ok else 'FAIL'}")

            try:
                dkim = [str(rr).strip('"') for rr in dns.resolver.resolve(f"mail._domainkey.{fqdn}", "TXT")]
            except Exception:
                dkim = []
            dkim_ok = any("v=DKIM" in d or "k=rsa" in d for d in dkim)
            if not dkim_ok:
                all_passed = False
            checks.append(f"DKIM={'PASS' if dkim_ok else 'FAIL'}")

            _append_log(sb, job_id, f"  {fqdn}: {', '.join(checks)}")

        # FCrDNS
        _append_log(sb, job_id, "Checking FCrDNS", step=2)
        expected = _mail_hostname(domain)
        try:
            rev = dns.reversename.from_address(vps_ip)
            ptr = str(dns.resolver.resolve(rev, "PTR")[0]).rstrip(".")
        except Exception:
            ptr = "(failed)"
        ptr_ok = ptr == expected
        if not ptr_ok:
            all_passed = False
        _append_log(sb, job_id, f"  PTR {vps_ip}: {'PASS' if ptr_ok else 'FAIL'} ({ptr})")

        # SMTP banner
        import socket
        _append_log(sb, job_id, "Checking SMTP banner", step=3)
        host = _mail_hostname(domain)
        try:
            with socket.create_connection((host, 25), timeout=10) as sock:
                banner = sock.recv(4096).decode(errors="replace").strip()
            smtp_ok = host in banner
        except socket.timeout:
            banner = "timeout (ISP may block port 25)"
            smtp_ok = True  # WARN, not FAIL
        except OSError as exc:
            banner = str(exc)
            smtp_ok = False
        if not smtp_ok:
            all_passed = False
        _append_log(sb, job_id, f"  SMTP: {'PASS' if smtp_ok else 'FAIL'} ({banner[:200]})")

        # TLS
        import ssl as ssl_mod
        _append_log(sb, job_id, "Checking TLS certificate", step=4)
        try:
            ctx = ssl_mod.create_default_context()
            with socket.create_connection((host, 465), timeout=10) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as tls:
                    cert = tls.getpeercert()
            tls_status = "PASS"
        except ssl_mod.SSLError:
            try:
                ctx = ssl_mod._create_unverified_context()
                with socket.create_connection((host, 465), timeout=10) as sock:
                    with ctx.wrap_socket(sock, server_hostname=host) as tls:
                        cert = tls.getpeercert()
                tls_status = "WARN (self-signed)"
            except Exception as exc:
                tls_status = f"FAIL ({exc})"
                all_passed = False
        except socket.timeout:
            tls_status = "WARN (timeout, ISP may block port 465)"
        except Exception as exc:
            tls_status = f"FAIL ({exc})"
            all_passed = False
        _append_log(sb, job_id, f"  TLS: {tls_status}")

        if all_passed:
            state.mark_step_done("verify")
            _upsert_shard(sb, domain, client_id=client_id, status="verified")
            _append_log(sb, job_id, "All checks PASSED", step=5)
        else:
            _append_log(sb, job_id, "Some checks FAILED", step=5)

        _complete_job(sb, job_id, error=None if all_passed else "Verification failures detected")

    except Exception as exc:
        _append_log(sb, job_id, f"FAILED: {exc}")
        _complete_job(sb, job_id, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# LOAD TO BISON
# ---------------------------------------------------------------------------

def _generate_signature(first: str, last: str, email: str, company: str) -> str:
    """Legacy single-tenant signature generator (random.choice templates).

    Kept for the v1 single-tenant code path. The v2 multi-tenant path uses
    _generate_signature_from_formula below, which consumes a SignatureFormula
    from the client's signature_formulas row.
    """
    import random

    templates = [
        f"<p>{first} {last}</p>",
        f"<p><strong>{first} {last}</strong> | {company}</p>",
        f"<p>{first} {last}<br>{company}</p>",
        f"<p>{first} {last}<br>{email}</p>",
        f"<p><strong>{first} {last}</strong> | {company}<br>{email}</p>",
        f"<p>{first} {last}<br>{company}<br>{email}</p>",
        f"<p>{first} {last} - {company}</p>",
        f"<p>{email}</p>",
        f"<p>{first} {last}, {company}</p>",
        f"<p>{first}<br>{company}</p>",
        f"<p>{first} {last} | {email}</p>",
        f"<p>{first} from {company}</p>",
        f"<p>Best,<br>{first} {last}</p>",
        f"<p>Thanks,<br>{first}</p>",
        f"<p>Cheers,<br>{first} {last}<br>{company}</p>",
        f"<p>{first} {last}<br>{company} Team</p>",
    ]
    sig = random.choice(templates)
    if random.random() < 0.10:
        mobile_tags = ["Sent from my iPhone", "Sent from my mobile", "Sent from mobile"]
        sig += f"<p style=\"font-size:12px;color:#888;\">{random.choice(mobile_tags)}</p>"
    return sig


def _email_seed(email: str) -> int:
    """Deterministic seed derived from email address.

    Matches the seed function used in the old supabase/functions/load-to-bison
    edge function, so the v2 API produces identical signatures to what the
    edge function was producing for existing senders.
    """
    h = 0
    for c in email:
        h = (h * 31 + ord(c)) & 0xFFFFFFFF
    return h


def _generate_signature_from_formula(first: str, last: str, email: str, formula) -> str:
    """Multi-tenant signature generator driven by a SignatureFormula.

    Deterministic by email address so re-runs produce the same signature
    for the same sender. Honours the client's style (html / plaintext),
    company name pool, title pool, quote pool, opt-out pool, and per-pool
    inclusion rates.
    """
    full_name = f"{first} {last}".strip() if last else first

    # Plaintext: short name + company (e.g. ReachOS workspace style)
    if formula.style == "plaintext":
        company = formula.company_names[0] if formula.company_names else ""
        if company:
            return f"{full_name}\n{company}".strip()
        return full_name

    # Degenerate HTML case — no formula pools configured
    if not formula.titles or not formula.company_names:
        return f"<p>{full_name}</p>"

    seed = _email_seed(email)
    title = formula.titles[seed % len(formula.titles)]
    company = formula.company_names[seed % len(formula.company_names)]
    fmt = seed % max(1, formula.format_variants)
    include_pronouns = (seed % 10) < int(formula.include_pronouns_rate * 10)
    include_quote = (seed % 7) < int(formula.include_quote_rate * 7)
    include_email = (seed % 5) < int(formula.include_email_rate * 5)

    quote = formula.quotes[(seed // 4) % len(formula.quotes)] if formula.quotes else None
    optout = formula.optouts[seed % len(formula.optouts)] if formula.optouts else None

    pronouns = " (she/her)" if include_pronouns else ""

    # 6 format variants. fmt 0–3 show the title on the name line; 4–5 keep it
    # on the company line. Matches the rotation the edge function used.
    if fmt <= 3:
        name_line = f"<p><strong>{full_name}</strong>{pronouns} | {title}</p>"
    else:
        name_line = f"<p><strong>{full_name}</strong>{pronouns}</p>"

    if fmt == 0:
        company_line = f"<p>{company}</p>"
    elif fmt == 1:
        company_line = f"<p>{title} · {company}</p>"
    elif fmt == 2:
        company_line = f"<p>{title}, {company}</p>"
    elif fmt == 3:
        company_line = f"<p>{company} | {title}</p>"
    elif fmt == 4:
        company_line = f"<p>{title} · {company}</p>"
    else:
        company_line = f"<p>{title}, {company}</p>"

    middle = []
    if include_email and include_quote and quote:
        if seed % 3 == 0:
            middle.append(f"<p>{email}</p>")
            middle.append(f"<p><em>\"{quote}\"</em></p>")
        else:
            middle.append(f"<p><em>\"{quote}\"</em></p>")
            middle.append(f"<p>{email}</p>")
    elif include_email:
        middle.append(f"<p>{email}</p>")
    elif include_quote and quote:
        middle.append(f"<p><em>\"{quote}\"</em></p>")

    parts = [name_line, company_line] + middle
    if optout:
        parts.append(f"<p>{optout}</p>")
    return "\n".join(parts)


def run_load_to_bison(
    job_id: str,
    client_id: str,
    domain: str,
    workspace: str | None = None,
    tag: str = "Custom SMTP",
) -> None:
    """Create shard mailboxes individually in Bison with formula-driven signatures.

    Multi-tenant: resolves the target Bison workspace and signature formula
    from the client's ClientContext rather than from a global bison_workspaces
    table or hardcoded company names.
    """
    import time as _time

    load_dotenv()  # process env wins (set via docker --env-file)
    sb = _supabase()
    _update_job(sb, job_id, status="running")

    try:
        csv_file = SHARDS_DIR / f"{domain}_bison.csv"
        if not csv_file.exists():
            raise RuntimeError(f"CSV not found at {csv_file}. Run deploy first.")

        # Resolve workspace + signature formula via ClientContext
        _append_log(sb, job_id, "Resolving workspace token", step=1)
        ctx = load_client_context_by_id(client_id)
        try:
            target_ws = ctx.workspace(workspace)  # named, or default if None
        except ValueError as exc:
            raise RuntimeError(str(exc))

        ws_name = target_ws.name
        bison = target_ws.client()
        base_url = target_ws.base_url.rstrip("/")
        chosen_token = target_ws._api_key  # used directly only for PATCH below
        formula = ctx.signature_for_workspace(target_ws.id)

        _append_log(sb, job_id, f"Target workspace: {ws_name} (client: {ctx.slug})", step=2)

        # Read CSV rows
        rows = []
        with csv_file.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                rows.append(row)
        if not rows:
            raise RuntimeError(f"CSV is empty: {csv_file}")

        _append_log(sb, job_id, f"Creating {len(rows)} senders individually with signatures", step=3)

        # Create each sender individually with formula-driven signature
        created_ids = []
        failed = 0

        for i, row in enumerate(rows, 1):
            name = row.get("Name", "")
            email_addr = row.get("Email", "")
            password = row.get("Password", "")
            imap_server = row.get("IMAP Server", "")
            imap_port = int(row.get("IMAP Port", "993"))
            smtp_server = row.get("SMTP Server", "")
            smtp_port = int(row.get("SMTP Port", "465"))
            daily_limit = int(row.get("Daily Limit", "10"))

            parts = name.strip().split(" ", 1)
            first = parts[0] if parts else "Team"
            last = parts[1] if len(parts) > 1 else ""

            signature = _generate_signature_from_formula(first, last, email_addr, formula)

            payload = {
                "name": name,
                "email": email_addr,
                "password": password,
                "imap_server": imap_server,
                "imap_port": imap_port,
                "smtp_server": smtp_server,
                "smtp_port": smtp_port,
                "imap_secure": True,
                "smtp_secure": True,
                "daily_limit": daily_limit,
                "email_signature": signature,
            }

            try:
                result = bison.create_sender_imap_smtp(payload)
                sender_id = result.get("id")
                if sender_id:
                    created_ids.append(sender_id)
                    # Bison ignores email_signature on create — PATCH it after
                    try:
                        import requests as _requests
                        _requests.patch(
                            f"{base_url}/api/sender-emails/{sender_id}",
                            headers={"Authorization": f"Bearer {chosen_token}",
                                     "Accept": "application/json",
                                     "Content-Type": "application/json"},
                            json={"email_signature": signature, "daily_limit": daily_limit},
                            timeout=15,
                        )
                    except Exception:
                        pass  # Non-blocking — signature is nice-to-have
            except Exception as exc:
                failed += 1
                if i <= 3 or failed <= 3:
                    _append_log(sb, job_id, f"Failed sender {email_addr}: {exc}")

            # Log progress every 5 senders
            if i % 5 == 0 or i == len(rows):
                _append_log(sb, job_id, f"Created {i}/{len(rows)} senders ({failed} failed)")

            # Small delay between calls to avoid rate limiting
            if i < len(rows):
                _time.sleep(0.5)

        _append_log(sb, job_id, f"Created {len(created_ids)}/{len(rows)} senders", step=4)

        # Tag all created senders
        if created_ids:
            _append_log(sb, job_id, f"Attaching tag '{tag}'", step=5)
            try:
                tag_obj = bison.find_or_create_tag(tag)
                tag_id = tag_obj.get("id")
                if tag_id:
                    bison.attach_tag_to_senders(tag_id, created_ids)
                    _append_log(sb, job_id, f"Tagged {len(created_ids)} senders")
            except Exception as tag_exc:
                _append_log(sb, job_id, f"Tag attach warning: {tag_exc}")

        # Upload CSV to Supabase Storage, namespaced by client
        storage_path = f"{ctx.slug}/{domain}.csv"
        try:
            with open(csv_file, "rb") as fh:
                sb.storage.from_("shard-csvs").upload(
                    storage_path, fh.read(),
                    file_options={"content-type": "text/csv", "upsert": "true"},
                )
        except Exception as upload_exc:
            _append_log(sb, job_id, f"CSV upload warning: {upload_exc}")
            storage_path = None

        _upsert_shard(
            sb, domain,
            client_id=client_id,
            client_bison_workspace_id=target_ws.id,
            bison_loaded=True,
            bison_workspace=ws_name,
            bison_loaded_at=datetime.now(timezone.utc).isoformat(),
            csv_storage_path=storage_path,
        )
        _append_log(sb, job_id, f"Done: {len(created_ids)} senders loaded to {ws_name}", step=6)
        _complete_job(sb, job_id)

    except Exception as exc:
        _append_log(sb, job_id, f"FAILED: {exc}")
        _complete_job(sb, job_id, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# DOMAIN SYNC
# ---------------------------------------------------------------------------

def run_domain_sync(job_id: str, client_id: str, default_client_id_for_new: str | None = None) -> None:
    """Sync the client's Cloudflare account zones into infra_domains.

    Multi-tenant note: a Cloudflare account can host domains for multiple
    clients (the 10x-managers CF account hosts both 10x-managers and
    reachos brand domains). This sync updates rows that already have a
    client_id mapping. For zones not yet in infra_domains, the row is
    inserted with client_id = default_client_id_for_new if provided
    (defaults to the calling client). Reassign via UPDATE infra_domains
    if a new zone actually belongs to a different client.
    """
    import time as _time

    load_dotenv()  # process env wins (set via docker --env-file)
    sb = _supabase()
    _update_job(sb, job_id, status="running")

    try:
        ctx = load_client_context_by_id(client_id)
        cf = ctx.cloudflare
        new_client_id = default_client_id_for_new or client_id

        # Step 1: List all zones in this CF account
        _append_log(sb, job_id, "Fetching all zones from Cloudflare", step=1)
        zones = cf.list_zones()
        _append_log(sb, job_id, f"Found {len(zones)} zones")

        # Load existing rows to preserve client_id and skip registrar refetch
        existing = (
            sb.table("infra_domains")
            .select("domain, client_id, registrar_created_at")
            .execute()
        )
        existing_client_id: dict[str, str] = {}
        has_registrar: set[str] = set()
        for r in existing.data or []:
            existing_client_id[r["domain"]] = r.get("client_id")
            if r.get("registrar_created_at"):
                has_registrar.add(r["domain"])

        # Step 2: Upsert each zone
        _append_log(sb, job_id, "Syncing zone and registrar data", step=2)
        synced = 0
        for zone in zones:
            domain = zone.get("name", "")
            if not domain:
                continue

            row = {
                "domain": domain,
                "zone_id": zone.get("id"),
                "zone_status": zone.get("status"),
                "name_servers": zone.get("name_servers", []),
                "zone_created_on": zone.get("created_on"),
                "last_synced_at": datetime.now(timezone.utc).isoformat(),
            }

            if domain not in has_registrar:
                try:
                    reg_info = cf.registrar_domain_info(domain)
                    if reg_info:
                        row["registrar_created_at"] = reg_info.get("created_at") or reg_info.get("registered_at")
                        row["registrar_status"] = reg_info.get("status")
                    _time.sleep(0.3)
                except Exception as exc:
                    _append_log(sb, job_id, f"  Registrar lookup failed for {domain}: {exc}")

            if domain in existing_client_id:
                # Existing row — preserve its client_id, just update zone data
                sb.table("infra_domains").update(row).eq("domain", domain).execute()
            else:
                # New zone — attribute to the configured default client
                row["client_id"] = new_client_id
                sb.table("infra_domains").insert(row).execute()

            synced += 1

        _append_log(sb, job_id, f"Synced {synced} domains", step=3)
        _complete_job(sb, job_id)

    except Exception as exc:
        _append_log(sb, job_id, f"FAILED: {exc}")
        _complete_job(sb, job_id, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# DOMAIN REGISTER
# ---------------------------------------------------------------------------

def run_domain_register(job_id: str, client_id: str, domain: str) -> None:
    """Register a domain via Cloudflare Registrar and add to infra_domains.

    Uses the client's Cloudflare account (via ClientContext) so the new
    zone lands on the right account. The domain is attributed to the
    given client in infra_domains.
    """
    load_dotenv()  # process env wins (set via docker --env-file)
    sb = _supabase()
    _update_job(sb, job_id, status="running")

    try:
        ctx = load_client_context_by_id(client_id)
        cf = ctx.cloudflare

        # Step 1: Check availability
        _append_log(sb, job_id, f"Checking availability of {domain}", step=1)
        avail = cf.registrar_check_availability(domain)
        if not avail.get("available"):
            raise RuntimeError(
                f"Domain {domain} is not available for registration. "
                f"Status: {avail.get('status', 'unknown')}"
            )
        price = avail.get("price") or avail.get("renewal_price") or "unknown"
        _append_log(sb, job_id, f"Available. Price: {price}/year")

        # Step 2: Register
        _append_log(sb, job_id, f"Registering {domain} via Cloudflare Registrar", step=2)
        reg_info = cf.registrar_register(domain, years=1, privacy=True)
        _append_log(sb, job_id, "Registration submitted, waiting for activation")

        # Step 3: Wait for zone + insert into infra_domains
        _append_log(sb, job_id, "Waiting for zone to appear", step=3)
        zone_id = cf.wait_for_zone(domain, timeout=1200)

        # Get zone details
        zone_info = cf._request("GET", f"/zones/{zone_id}")
        zone = zone_info.get("result", {})

        row = {
            "client_id": client_id,
            "domain": domain,
            "zone_id": zone_id,
            "zone_status": zone.get("status", "pending"),
            "name_servers": zone.get("name_servers", []),
            "zone_created_on": zone.get("created_on"),
            "registrar_created_at": (
                reg_info.get("created_at")
                or reg_info.get("registered_at")
                or datetime.now(timezone.utc).isoformat()
            ),
            "registrar_status": reg_info.get("status", "active"),
            "last_synced_at": datetime.now(timezone.utc).isoformat(),
        }

        # Scope by (client_id, domain) — multi-tenant unique key
        existing_row = (
            sb.table("infra_domains")
            .select("id")
            .eq("client_id", client_id)
            .eq("domain", domain)
            .execute()
        )
        if existing_row.data:
            (
                sb.table("infra_domains")
                .update(row)
                .eq("client_id", client_id)
                .eq("domain", domain)
                .execute()
            )
        else:
            sb.table("infra_domains").insert(row).execute()

        _append_log(sb, job_id, f"Domain {domain} registered and synced (zone={zone_id})", step=4)
        _complete_job(sb, job_id)

    except Exception as exc:
        _append_log(sb, job_id, f"FAILED: {exc}")
        _complete_job(sb, job_id, error=f"{type(exc).__name__}: {exc}")
