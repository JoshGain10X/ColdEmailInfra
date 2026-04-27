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
    """Insert or update the infra_shards row for a domain."""
    existing = sb.table("infra_shards").select("id").eq("domain", domain).execute()
    if existing.data:
        sb.table("infra_shards").update(fields).eq("domain", domain).execute()
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
    domain: str,
    provider: str,
    product_id: str,
    region: str,
    image_id: str,
    ssl_type: str = "self-signed",
) -> None:
    """Full shard deployment — called as a background task."""
    load_dotenv(override=True)
    sb = _supabase()
    _update_job(sb, job_id, status="running")
    _upsert_shard(sb, domain, status="deploying", provider=provider, region=region)

    try:
        state = ShardState(domain)

        # Step 0: Ensure domain
        _append_log(sb, job_id, "Ensuring domain on Cloudflare", step=0)
        cf = CloudflareClient()
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
        if not state.is_step_done("generate"):
            _append_log(sb, job_id, "Generating subdomains and mailboxes")
            subs = pick_subdomains(domain)
            seed = secrets.randbits(64)
            mailboxes = generate_mailboxes(domain, subs, seed=seed)
            state.set("subdomains", subs)
            state.set("mailbox_seed", seed)
            state.set("mailboxes", mailboxes)
            state.mark_step_done("generate")
        _append_log(sb, job_id, f"Generated {len(state.get('subdomains'))} subdomains, {len(state.get('mailboxes'))} mailboxes", step=2)

        # Step 2: Provision VPS
        if not state.is_step_done("provision_vps"):
            _append_log(sb, job_id, f"Provisioning VPS via {provider}")
            vps_client = _make_vps_client(provider)
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
            blocklist.notify_check_ip(ip)
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
        _upsert_shard(sb, domain, vps_ip=vps_ip)
        _append_log(sb, job_id, f"VPS ready at {vps_ip}", step=3)

        # Step 3: Set PTR
        if not state.is_step_done("set_ptr"):
            _append_log(sb, job_id, "Setting PTR (reverse DNS)")
            vps = state.get("vps")
            vps_client = _make_vps_client(vps.get("provider", provider))
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
            cf = CloudflareClient()
            vps_ip = state.get("vps")["ip"]
            subs = state.get("subdomains")
            dmarc_rua = os.environ.get("DMARC_RUA", f"dmarc@{domain}")
            redirect_target = os.environ.get("REDIRECT_TARGET", "https://10xmanagers.com")
            mail_host = _mail_hostname(domain)

            cf.upsert_record(zone_id, "A", domain, vps_ip, proxied=True)
            cf.upsert_record(zone_id, "A", mail_host, vps_ip, proxied=False)
            cf.upsert_record(
                zone_id, "TXT", f"_dmarc.{domain}",
                f"v=DMARC1; p=quarantine; sp=quarantine; rua=mailto:{dmarc_rua}; adkim=r; aspf=r",
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
        le_email = os.environ.get("LE_EMAIL", f"ops@{domain}")
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
                    ms.install_dms(domain, le_email, os.environ["CLOUDFLARE_API_TOKEN"].strip(), ssl_type=ssl_type)
            else:
                ms.install_docker()
                ms.install_dms(domain, le_email, os.environ["CLOUDFLARE_API_TOKEN"].strip(), ssl_type=ssl_type)
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

        # Step 7: Setup DKIM
        if not state.is_step_done("setup_dkim"):
            _append_log(sb, job_id, "Generating DKIM keys")
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

        # Update shard status
        step_flags = state.data.get("steps", {})
        _upsert_shard(sb, domain,
            status="active",
            mailbox_count=len(state.get("mailboxes", [])),
            step_flags=step_flags,
        )

        _append_log(sb, job_id, "Deploy complete", step=10)
        _complete_job(sb, job_id)

    except Exception as exc:
        _append_log(sb, job_id, f"FAILED: {exc}")
        _complete_job(sb, job_id, error=f"{type(exc).__name__}: {exc}")
        _upsert_shard(sb, domain, status="failed")


# ---------------------------------------------------------------------------
# DESTROY
# ---------------------------------------------------------------------------

def run_destroy(job_id: str, domain: str) -> None:
    """Destroy a shard — called as a background task."""
    import shutil
    import time

    load_dotenv(override=True)
    sb = _supabase()
    _update_job(sb, job_id, status="running")

    try:
        state = ShardState(domain)
        if not state.path.exists():
            raise RuntimeError(f"No state file for {domain}")

        _upsert_shard(sb, domain, status="destroying")

        # Step 1: Destroy VPS
        _append_log(sb, job_id, "Destroying VPS", step=1)
        vps = state.get("vps")
        if vps and vps.get("id"):
            provider = vps.get("provider", "contabo")
            try:
                _make_vps_client(provider).destroy_instance(vps["id"])
            except Exception as exc:
                _append_log(sb, job_id, f"VPS destroy warning: {exc}")

        # Step 2: Delete DNS records
        _append_log(sb, job_id, "Deleting DNS records", step=2)
        cf = CloudflareClient()
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

def run_verify(job_id: str, domain: str) -> None:
    """Run verification checks — called as a background task."""
    load_dotenv(override=True)
    sb = _supabase()
    _update_job(sb, job_id, status="running")

    try:
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
            _upsert_shard(sb, domain, status="verified")
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

def run_load_to_bison(job_id: str, domain: str, workspace: str | None = None, tag: str = "Custom SMTP") -> None:
    """Bulk-upload shard mailboxes to Bison — called as a background task."""
    load_dotenv(override=True)
    sb = _supabase()
    _update_job(sb, job_id, status="running")

    try:
        csv_file = SHARDS_DIR / f"{domain}_bison.csv"
        if not csv_file.exists():
            raise RuntimeError(f"CSV not found at {csv_file}. Run deploy first.")

        tokens_raw = os.environ.get("BISON_API_TOKENS", "").strip()
        if not tokens_raw:
            raise RuntimeError("BISON_API_TOKENS not set")
        tokens = [t.strip() for t in tokens_raw.split(",") if t.strip()]
        base_url = os.environ.get("BISON_API_BASE", "https://send.spamproofed.com").rstrip("/")

        # Import the curl helper from load_to_bison
        # We re-implement minimally here to avoid Click dependency
        import subprocess

        _CURL_SENTINEL = "<<<BISON_HTTP_CODE>>>"

        def _curl(method, path, token, body=None, form=None):
            cmd = [
                "curl", "-s", "-X", method, f"{base_url}{path}",
                "-H", f"Authorization: Bearer {token}",
                "-H", "Accept: application/json",
                "-w", f"\n{_CURL_SENTINEL}%{{http_code}}",
            ]
            if body is not None:
                cmd += ["-H", "Content-Type: application/json",
                        "-d", json.dumps(body, separators=(",", ":"))]
            if form is not None:
                for f in form:
                    cmd += ["-F", f]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            output = result.stdout or ""
            if _CURL_SENTINEL not in output:
                return 0, {"error": f"curl no sentinel; raw={output[:400]}"}
            body_str, code_str = output.rsplit(_CURL_SENTINEL, 1)
            try:
                status = int(code_str.strip())
            except ValueError:
                status = 0
            body_str = body_str.strip()
            try:
                parsed = json.loads(body_str) if body_str else {}
            except json.JSONDecodeError:
                parsed = {"raw": body_str[:500]}
            return status, parsed

        # Discover workspaces
        _append_log(sb, job_id, "Discovering Bison workspaces", step=1)
        usable = []
        for tok in tokens:
            status_code, body = _curl("GET", "/api/workspaces/v1.1", tok)
            if status_code != 200:
                continue
            workspaces = body.get("data") or []
            if len(workspaces) == 1:
                usable.append((tok, workspaces[0]))

        if not usable:
            raise RuntimeError("No usable per-workspace tokens found")

        if workspace:
            matches = [(tok, ws) for tok, ws in usable if ws.get("name") == workspace]
            if not matches:
                available = [ws.get("name") for _, ws in usable]
                raise RuntimeError(f"Workspace {workspace!r} not found. Available: {available}")
            chosen_token, chosen_ws = matches[0]
        else:
            chosen_token, chosen_ws = usable[0]

        ws_name = chosen_ws.get("name", "?")
        _append_log(sb, job_id, f"Target workspace: {ws_name}", step=2)

        # Upload CSV
        _append_log(sb, job_id, "Uploading CSV to Bison", step=3)
        status_code, body = _curl(
            "POST", "/api/sender-emails/bulk", chosen_token,
            form=[f"csv=@{csv_file}"],
        )
        if status_code not in (200, 201):
            raise RuntimeError(f"Bulk upload failed: HTTP {status_code}: {json.dumps(body)[:500]}")

        created = body.get("data") or []
        created_ids = [s.get("id") for s in created if s.get("id")]
        _append_log(sb, job_id, f"Uploaded {len(created_ids)} senders", step=4)

        if created_ids:
            # Resolve/create tag
            _append_log(sb, job_id, f"Attaching tag '{tag}'", step=5)
            tag_status, tag_body = _curl("GET", "/api/tags", chosen_token)
            tag_id = None
            if tag_status == 200:
                for t in tag_body.get("data") or []:
                    if t.get("name") == tag:
                        tag_id = t.get("id")
                        break
            if tag_id is None:
                tag_status, tag_body = _curl("POST", "/api/tags", chosen_token, body={"name": tag})
                if tag_status in (200, 201):
                    tag_id = (tag_body.get("data") or {}).get("id")

            if tag_id:
                _curl("POST", "/api/tags/attach-to-sender-emails", chosen_token,
                       body={"tag_ids": [tag_id], "sender_email_ids": created_ids, "skip_webhooks": True})
                _append_log(sb, job_id, f"Tagged {len(created_ids)} senders")

        # Upload CSV to Supabase Storage for CRM download
        storage_path = f"{domain}.csv"
        try:
            with open(csv_file, "rb") as fh:
                sb.storage.from_("shard-csvs").upload(
                    storage_path, fh.read(),
                    file_options={"content-type": "text/csv", "upsert": "true"},
                )
        except Exception as upload_exc:
            _append_log(sb, job_id, f"CSV upload warning: {upload_exc}")
            storage_path = None

        _upsert_shard(sb, domain, bison_loaded=True, bison_workspace=ws_name,
                      bison_loaded_at=datetime.now(timezone.utc).isoformat(),
                      csv_storage_path=storage_path)
        _append_log(sb, job_id, f"Done: {len(created_ids)} senders loaded to {ws_name}", step=6)
        _complete_job(sb, job_id)

    except Exception as exc:
        _append_log(sb, job_id, f"FAILED: {exc}")
        _complete_job(sb, job_id, error=f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# DOMAIN SYNC
# ---------------------------------------------------------------------------

def run_domain_sync(job_id: str) -> None:
    """Sync all Cloudflare zones + registrar data into infra_domains table."""
    import time as _time

    load_dotenv(override=True)
    sb = _supabase()
    _update_job(sb, job_id, status="running")

    try:
        cf = CloudflareClient()

        # Step 1: List all zones
        _append_log(sb, job_id, "Fetching all zones from Cloudflare", step=1)
        zones = cf.list_zones()
        _append_log(sb, job_id, f"Found {len(zones)} zones")

        # Load existing domains to check which already have registrar data
        existing = sb.table("infra_domains").select("domain, registrar_created_at").execute()
        has_registrar = {
            r["domain"] for r in (existing.data or [])
            if r.get("registrar_created_at")
        }

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

            # Only fetch registrar info if we don't already have the reg date
            if domain not in has_registrar:
                try:
                    reg_info = cf.registrar_domain_info(domain)
                    if reg_info:
                        row["registrar_created_at"] = reg_info.get("created_at") or reg_info.get("registered_at")
                        row["registrar_status"] = reg_info.get("status")
                    _time.sleep(0.3)  # Gentle rate limiting
                except Exception as exc:
                    _append_log(sb, job_id, f"  Registrar lookup failed for {domain}: {exc}")

            # Upsert by domain
            existing_row = sb.table("infra_domains").select("id").eq("domain", domain).execute()
            if existing_row.data:
                sb.table("infra_domains").update(row).eq("domain", domain).execute()
            else:
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

def run_domain_register(job_id: str, domain: str) -> None:
    """Register a domain via Cloudflare Registrar and add to infra_domains."""
    load_dotenv(override=True)
    sb = _supabase()
    _update_job(sb, job_id, status="running")

    try:
        cf = CloudflareClient()

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
        zone_id = cf.wait_for_zone(domain, timeout=600)

        # Get zone details
        zone_info = cf._request("GET", f"/zones/{zone_id}")
        zone = zone_info.get("result", {})

        row = {
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

        existing_row = sb.table("infra_domains").select("id").eq("domain", domain).execute()
        if existing_row.data:
            sb.table("infra_domains").update(row).eq("domain", domain).execute()
        else:
            sb.table("infra_domains").insert(row).execute()

        _append_log(sb, job_id, f"Domain {domain} registered and synced (zone={zone_id})", step=4)
        _complete_job(sb, job_id)

    except Exception as exc:
        _append_log(sb, job_id, f"FAILED: {exc}")
        _complete_job(sb, job_id, error=f"{type(exc).__name__}: {exc}")
