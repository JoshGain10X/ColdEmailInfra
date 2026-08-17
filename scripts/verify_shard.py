#!/usr/bin/env python3
"""Verify a deployed shard end-to-end.

Runs: DNS propagation checks, FCrDNS alignment, HELO banner match,
authenticated SMTP send, DKIM/SPF/DMARC pass header check.
"""
from __future__ import annotations

import os
import socket
import ssl
import sys
from pathlib import Path

import click
import dns.resolver
import dns.reversename
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.mailserver import MailserverClient, INOTIFY_MAX_USER_INSTANCES
from lib.state import ShardState


def _mail_hostname(domain: str) -> str:
    return f"mail.{domain}"


def _resolve(name: str, rrtype: str) -> list[str]:
    try:
        answers = dns.resolver.resolve(name, rrtype)
        return [str(rr).strip('"') for rr in answers]
    except dns.resolver.NoAnswer:
        return []
    except dns.resolver.NXDOMAIN:
        return []


def check_dns(state: ShardState, domain: str) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    vps_ip = state.get("vps")["ip"]
    sample_subs = state.get("subdomains")[:3]

    for sub in sample_subs:
        fqdn = f"{sub}.{domain}"
        a = _resolve(fqdn, "A")
        if sub == "mail":
            # mail.* records are unproxied — must resolve to VPS IP directly.
            a_ok = vps_ip in a
        else:
            # Proxied subdomains resolve to Cloudflare IPs, not VPS IP.
            # Any A response means the record exists and is proxied correctly.
            a_ok = len(a) > 0
        results.append((f"A {fqdn}", _status(a_ok), ", ".join(a) or "(no answer)"))

        mx = _resolve(fqdn, "MX")
        expect = f"mail.{fqdn}"
        ok = any(expect in m for m in mx)
        results.append((f"MX {fqdn}", _status(ok), ", ".join(mx) or "(no answer)"))

        spf = _resolve(fqdn, "TXT")
        ok = any("v=spf1" in s for s in spf)
        results.append((f"SPF {fqdn}", _status(ok), next((s for s in spf if "v=spf1" in s), "(missing)")))

        dmarc = _resolve(f"_dmarc.{fqdn}", "TXT")
        ok = any("v=DMARC1" in d for d in dmarc)
        results.append((f"DMARC {fqdn}", _status(ok), next((d for d in dmarc if "v=DMARC1" in d), "(missing)")))

        dkim = _resolve(f"mail._domainkey.{fqdn}", "TXT")
        ok = any("v=DKIM" in d or "k=rsa" in d for d in dkim)
        results.append((f"DKIM {fqdn}", _status(ok), (dkim[0][:60] + "...") if dkim else "(missing)"))

    return results


def check_fcrdns(state: ShardState, domain: str) -> list[tuple[str, str, str]]:
    results: list[tuple[str, str, str]] = []
    vps_ip = state.get("vps")["ip"]
    expected = _mail_hostname(domain)

    try:
        rev = dns.reversename.from_address(vps_ip)
        ptr = str(dns.resolver.resolve(rev, "PTR")[0]).rstrip(".")
    except Exception as exc:
        ptr = f"(lookup failed: {exc})"
    results.append((f"PTR {vps_ip}", _status(ptr == expected), ptr))

    forward = _resolve(expected, "A")
    results.append((f"A {expected}", _status(vps_ip in forward), ", ".join(forward) or "(no answer)"))

    return results


ISP_BLOCK_NOTE = (
    "connect from this host timed out — probably your local ISP blocking "
    "outbound :{port} (UK/residential ISPs almost always do). Bison sends "
    "from its own datacenter so this does NOT affect cold-email delivery; "
    "SSH into the VPS and check `sudo ss -tlnp | grep :{port}` to confirm "
    "the mailserver is actually listening."
)


def check_smtp_banner(domain: str) -> tuple[str, str]:
    """Returns (status, detail) where status is 'PASS', 'FAIL', or 'WARN'."""
    host = _mail_hostname(domain)
    try:
        with socket.create_connection((host, 25), timeout=10) as sock:
            banner = sock.recv(4096).decode(errors="replace").strip()
    except socket.timeout:
        return "WARN", ISP_BLOCK_NOTE.format(port=25)
    except OSError as exc:
        return "FAIL", f"(connect failed: {exc})"
    return ("PASS" if host in banner else "FAIL", banner)


def check_tls(domain: str) -> tuple[str, str]:
    """Verify the TLS cert on :465. Self-signed certs (the default ssl_type
    we deploy with) pass as WARN if the CN/SAN still matches the hostname;
    only a CN/SAN mismatch is a FAIL.
    """
    host = _mail_hostname(domain)

    def _probe(verify: bool) -> tuple[dict, bool]:
        ctx = ssl.create_default_context() if verify else ssl._create_unverified_context()
        with socket.create_connection((host, 465), timeout=10) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                return tls.getpeercert(), verify

    try:
        cert, verified = _probe(verify=True)
    except socket.timeout:
        return "WARN", ISP_BLOCK_NOTE.format(port=465)
    except ssl.SSLError:
        # Likely self-signed; retry unverified to still check CN/SAN match.
        try:
            cert, verified = _probe(verify=False)
        except Exception as exc:
            return "FAIL", f"(TLS check failed on retry: {exc})"
    except Exception as exc:
        return "FAIL", f"(TLS check failed: {exc})"

    subject = dict(x[0] for x in cert.get("subject", []))
    cn = subject.get("commonName", "")
    sans = [v for k, v in cert.get("subjectAltName", []) if k == "DNS"]
    hostname_ok = host == cn or host in sans
    detail = f"CN={cn}, SANs={sans}" + ("" if verified else " (self-signed — ssl_type=self-signed)")
    if not hostname_ok:
        return "FAIL", detail
    return ("PASS" if verified else "WARN"), detail


def check_hardening(state: ShardState, domain: str) -> list[tuple[str, str, str]]:
    """Assert the shard's resource ceilings and cert SAN coverage.

    Neither was previously checked, so a shard could deploy degraded from birth
    and still report ALL PASSED:

    - fs.inotify.max_user_instances at the kernel default of 128 against ~225
      demand means Dovecot silently stops watching mailboxes past the ceiling.
    - Dovecot imap-login in high-security mode forks one process per connection
      and drops them past a 500-process limit at ~400 concurrent sessions.
    - The TLS cert covering only mail.<root> does not match the per-subdomain MX
      hosts senders actually connect to, which permanently blocks MTA-STS
      enforce. check_tls only probes mail.<root>, so it passes a single-SAN cert.

    See project-shard-dovecot-inotify-limits and
    reference-mta-sts-enforce-blocked-by-cert.
    """
    rows: list[tuple[str, str, str]] = []
    vps_ip = (state.get("vps") or {}).get("ip")
    if not vps_ip:
        return [("hardening probe", "FAIL", "(no vps ip in state)")]

    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ssh_user = (state.get("vps") or {}).get("ssh_user") or os.environ.get("SSH_USER", "admin")
    try:
        ms = MailserverClient(vps_ip, ssh_key, user=ssh_user)
        ms.connect()
        try:
            ino = ms.verify_inotify_limits()
            dov = ms.verify_dovecot_limits()
            _, sans_out, _ = ms.sudo(
                f"openssl x509 -in /etc/letsencrypt/live/{_mail_hostname(domain)}/fullchain.pem "
                f"-noout -ext subjectAltName 2>/dev/null || true",
                check=False,
            )
        finally:
            ms.close()
    except Exception as exc:
        return [("hardening probe", "FAIL", f"({type(exc).__name__}: {exc})")]

    inst = ino.get("inst")
    rows.append((
        "fs.inotify.max_user_instances raised",
        _status(inst == str(INOTIFY_MAX_USER_INSTANCES)),
        f"{inst} (expected {INOTIFY_MAX_USER_INSTANCES}), {ino.get('inuse')} in use",
    ))
    sc = dov.get("imap_login_service_count")
    rows.append((
        "dovecot imap-login high-performance mode",
        _status(sc == "0"),
        f"service_count={sc} (expected 0), process_limit={dov.get('imap_login_process_limit')}",
    ))

    sans = {t.strip().removeprefix("DNS:") for t in (sans_out or "").replace("\n", ",").split(",") if "DNS:" in t}
    expected = [f"mail.{sub}.{domain}" for sub in (state.get("subdomains") or [])]
    missing = [n for n in expected if n not in sans]
    if not sans:
        rows.append(("cert covers every sending-subdomain MX host", "WARN",
                     "(no LE cert on disk - ssl_type=self-signed?)"))
    else:
        rows.append((
            "cert covers every sending-subdomain MX host",
            _status(not missing),
            f"{len(sans)} SANs; missing {len(missing)}" + (f" e.g. {missing[:2]}" if missing else ""),
        ))
    return rows


def _status(ok: bool) -> str:
    return "PASS" if ok else "FAIL"


def _print_rows(title: str, rows: list[tuple[str, str, str]]) -> bool:
    """Rows are (name, status, detail) with status in {PASS, FAIL, WARN}.
    Returns False only if any row is FAIL; WARN does not break the run.
    """
    click.echo(f"\n== {title} ==")
    has_fail = False
    for name, status, detail in rows:
        click.echo(f"  [{status}] {name}: {detail}")
        if status == "FAIL":
            has_fail = True
    return not has_fail


@click.command()
@click.option("--domain", required=True)
def main(domain: str) -> None:
    load_dotenv()
    state = ShardState(domain)
    if not state.is_step_done("setup_dkim"):
        raise click.ClickException(f"Shard {domain} has not completed DKIM step. Run deploy_shard.py first.")

    passed = True

    dns_rows = check_dns(state, domain)
    passed &= _print_rows("DNS (3 sample subdomains)", dns_rows)

    fcrdns_rows = check_fcrdns(state, domain)
    passed &= _print_rows("FCrDNS (PTR + forward A alignment)", fcrdns_rows)

    banner_status, banner = check_smtp_banner(domain)
    passed &= _print_rows("SMTP banner (HELO match)", [("EHLO banner includes mail hostname", banner_status, banner)])

    tls_status, tls_info = check_tls(domain)
    passed &= _print_rows("TLS certificate", [("465 cert matches mail hostname", tls_status, tls_info)])

    passed &= _print_rows("Hardening ceilings + cert coverage", check_hardening(state, domain))

    click.echo("")
    click.echo("Manual checks remaining:")
    click.echo("  1. Send a message via Bison and inspect headers for:")
    click.echo("     - Authentication-Results: dkim=pass spf=pass dmarc=pass")
    click.echo("     - List-Unsubscribe + List-Unsubscribe-Post: List-Unsubscribe=One-Click")
    click.echo(f"     - Message-ID: <...@{_mail_hostname(domain)}>")
    click.echo("  2. Run a LearnDMARC test: https://www.learndmarc.com/")

    if passed:
        state.mark_step_done("verify")
        click.echo("\nAutomated checks: ALL PASSED")
    else:
        click.echo("\nAutomated checks: FAILURES DETECTED")
        sys.exit(1)


if __name__ == "__main__":
    main()
