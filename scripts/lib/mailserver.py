from __future__ import annotations

import io
import re
import secrets
import time
from pathlib import Path

import paramiko


REPO_ROOT = Path(__file__).resolve().parents[2]
DMS_TEMPLATE_DIR = REPO_ROOT / "docker-mailserver"

# Where the mailserver compose project lives on the VPS. Must be writable
# by the SSH user without sudo (so we avoid /opt).
REMOTE_WORKDIR = "~/mailserver"


class MailserverClient:
    def __init__(self, ip: str, ssh_key_path: str, user: str = "admin"):
        self.ip = ip
        self.user = user
        self.ssh_key_path = Path(ssh_key_path).expanduser()
        self.ssh: paramiko.SSHClient | None = None
        self._home: str | None = None

    def connect(self) -> None:
        if self.ssh is not None:
            return
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        for attempt in range(10):
            try:
                client.connect(
                    self.ip,
                    username=self.user,
                    key_filename=str(self.ssh_key_path),
                    timeout=15,
                )
                # Keepalive every 60s so idle SSH channels (e.g. during
                # certbot's 600s DNS propagation wait) don't get dropped by
                # NAT / CGNAT middleboxes. Without this, recv_exit_status()
                # hangs forever when the server-side command finishes but the
                # TCP half is already dead.
                client.get_transport().set_keepalive(60)
                self.ssh = client
                return
            except (paramiko.SSHException, OSError) as exc:
                if attempt == 9:
                    raise
                time.sleep(5 * (attempt + 1))
                _ = exc

    def _home_dir(self) -> str:
        if self._home is None:
            _, out, _ = self.run("echo $HOME")
            self._home = out.strip() or f"/home/{self.user}"
        return self._home

    def _workdir(self) -> str:
        return f"{self._home_dir()}/mailserver"

    @property
    def workdir(self) -> str:
        return self._workdir()

    def restart_mailserver(self) -> None:
        """Restart the docker-mailserver container (used after DKIM keygen)."""
        self.sudo(f"sh -c 'cd {self._workdir()} && docker compose restart mailserver'")

    def run(self, cmd: str, check: bool = True) -> tuple[int, str, str]:
        assert self.ssh is not None
        stdin, stdout, stderr = self.ssh.exec_command(cmd)
        stdin.close()
        out = stdout.read().decode()
        err = stderr.read().decode()
        rc = stdout.channel.recv_exit_status()
        if check and rc != 0:
            raise RuntimeError(f"Command failed ({rc}): {cmd}\nstdout: {out}\nstderr: {err}")
        return rc, out, err

    def sudo(self, cmd: str, check: bool = True) -> tuple[int, str, str]:
        """Run a command as root via passwordless sudo."""
        return self.run(f"sudo -n {cmd}", check=check)

    def bootstrap_passwordless_sudo(self, password: str) -> None:
        """Grant the current SSH user passwordless sudo by writing
        /etc/sudoers.d/99-<user> via a one-time password-based `sudo -S`.

        Needed on Webdock cloud images where the shell user is created with
        a password but `passwordlessSudo` is a dashboard-only toggle (not
        exposed on the shellUsers API). Called once at the start of the
        install step; subsequent `sudo -n` calls in this client work
        unprompted.

        Idempotent: if the sudoers.d file already exists (prior resume),
        returns without re-writing.
        """
        _, out, _ = self.run("sudo -n true 2>/dev/null; echo $?", check=False)
        if out.strip().endswith("0"):
            return  # already passwordless
        # Password came from our own secrets.choice(string.ascii_letters+digits),
        # so no shell-meta characters to escape.
        sudoers_path = f"/etc/sudoers.d/99-{self.user}"
        sudoers_line = f"{self.user} ALL=(ALL) NOPASSWD: ALL"
        cmd = (
            f"echo '{password}' | sudo -S sh -c "
            f"\"echo '{sudoers_line}' > {sudoers_path} && chmod 440 {sudoers_path}\""
        )
        rc, _, err = self.run(cmd, check=False)
        if rc != 0:
            raise RuntimeError(
                f"Failed to bootstrap passwordless sudo for {self.user}: {err.strip()}"
            )
        # Verify it actually took.
        _, out, _ = self.run("sudo -n true 2>/dev/null; echo $?", check=False)
        if not out.strip().endswith("0"):
            raise RuntimeError(
                f"Wrote {sudoers_path} but `sudo -n` still prompts for password"
            )

    def upload_text(self, content: str, remote_path: str) -> None:
        assert self.ssh is not None
        sftp = self.ssh.open_sftp()
        try:
            with sftp.file(remote_path, "w") as f:
                f.write(content)
        finally:
            sftp.close()

    def upload_file(self, local_path: Path, remote_path: str) -> None:
        self.upload_text(local_path.read_text(), remote_path)

    def wait_for_cloud_init(self, timeout: int = 600) -> None:
        """Block until cloud-init has finished on a fresh VPS.

        Contabo's Ubuntu image runs cloud-init on first boot, which holds
        /var/lib/dpkg/lock-frontend for 1–5 min. contabo.wait_for_ssh() only
        waits for port 22, so without this wait install_docker's `curl | sh`
        races cloud-init on apt and fails with exit 100 "Could not get lock".

        Uses `cloud-init status --wait`, which is purpose-built for this race.
        No-ops if cloud-init isn't installed (non-Ubuntu images, etc).

        Note: cloud-init uses exit code 2 for "done with recoverable errors"
        (common on stock cloud images — e.g. a service reload that no-ops).
        The dpkg lock is released regardless, so we key off the 'status: done'
        line in stdout rather than the exit code.
        """
        rc, out, err = self.sudo(
            f"sh -c 'command -v cloud-init >/dev/null || {{ echo status: done; exit 0; }}; "
            f"timeout {timeout} cloud-init status --wait; true'",
            check=False,
        )
        if "status: done" not in out:
            raise RuntimeError(
                f"cloud-init did not reach 'done' within {timeout}s "
                f"(rc={rc})\nstdout: {out}\nstderr: {err}"
            )

    def install_docker(self) -> None:
        self.wait_for_cloud_init()
        # Install Docker if not present. get.docker.com detects sudo automatically.
        self.run("command -v docker >/dev/null || (curl -fsSL https://get.docker.com | sudo -n sh)")
        self.sudo("systemctl enable --now docker")
        # Also install certbot + Cloudflare DNS plugin for Let's Encrypt via DNS-01.
        self.sudo("apt-get update -qq", check=False)
        self.sudo("apt-get install -y certbot python3-certbot-dns-cloudflare")
        # Add the login user to the docker group so subsequent docker commands
        # don't need sudo. Takes effect on new sessions, so we still use sudo
        # for docker commands in this session.
        self.sudo(f"usermod -aG docker {self.user}", check=False)

    def acquire_letsencrypt_cert(self, hostname: str, email: str, cf_api_token: str) -> None:
        """Acquire a Let's Encrypt cert for `hostname` via Cloudflare DNS-01.

        Avoids the port-80 HTTP-01 challenge entirely — useful when the
        hostname's A record doesn't resolve to this VPS (e.g. zone still
        has stale records from a previous host), when port 80 is proxied
        through Cloudflare, or when any edge rule returns non-200 on
        /.well-known/acme-challenge/.

        Requires a Cloudflare API token with Zone:DNS:Edit + Zone:Zone:Read
        (our deploy token already has both).

        Idempotent — `--keep-until-expiring` makes certbot a no-op if the
        cert is present and >30 days from expiry.
        """
        import base64
        creds_path = "/root/.cf-certbot.ini"
        creds = f"dns_cloudflare_api_token = {cf_api_token}\n"
        # Base64-encode to avoid any shell quoting / $ expansion issues.
        b64 = base64.b64encode(creds.encode()).decode()
        self.sudo(f"sh -c 'umask 077 && echo {b64} | base64 -d > {creds_path}'")
        self.sudo(f"chmod 600 {creds_path}")

        # propagation-seconds=600 so the new TXT record stays live past LE
        # resolvers' negative-cache window (~5 min per CF's SOA minimum).
        # Shorter values race the cached NXDOMAIN from an earlier attempt.
        try:
            self.sudo(
                f"certbot certonly --dns-cloudflare "
                f"--dns-cloudflare-credentials {creds_path} "
                f"--dns-cloudflare-propagation-seconds 600 "
                f"--non-interactive --agree-tos --email {email} "
                f"-d {hostname} --keep-until-expiring"
            )
        except RuntimeError as exc:
            _, log, _ = self.sudo("tail -80 /var/log/letsencrypt/letsencrypt.log", check=False)
            raise RuntimeError(
                f"certbot DNS-01 failed for {hostname}: {exc}\n"
                f"--- Last 80 lines of /var/log/letsencrypt/letsencrypt.log ---\n{log}"
            )

    def acquire_self_signed_cert(self, hostname: str) -> None:
        """Generate a self-signed cert for `hostname` into the DMS ssl dir.

        docker-mailserver's SSL_TYPE=self-signed is Bring-Your-Own: it expects
        these files at /tmp/docker-mailserver/ssl/ (mounted from the host):
          - <hostname>-key.pem       (private key)
          - <hostname>-cert.pem      (cert)
          - demoCA/cacert.pem        (CA cert; same as cert for self-signed)
        Generated on the host so the container can mount them read-only.
        Idempotent — skips regeneration if both key and cert already exist.
        """
        ssl_dir = f"{self._workdir()}/docker-data/dms/config/ssl"
        key = f"{ssl_dir}/{hostname}-key.pem"
        crt = f"{ssl_dir}/{hostname}-cert.pem"
        self.run(f"mkdir -p {ssl_dir}/demoCA")
        self.run(
            f"( test -f {key} && test -f {crt} ) || "
            f"openssl req -x509 -newkey rsa:4096 -nodes -days 3650 "
            f"-keyout {key} -out {crt} "
            f'-subj "/CN={hostname}" '
            f'-addext "subjectAltName=DNS:{hostname}"'
        )
        self.run(f"cp {crt} {ssl_dir}/demoCA/cacert.pem")

    def seed_postmaster_account(self, root_domain: str) -> str:
        """Write a postmaster account to postfix-accounts.cf so Dovecot starts.

        docker-mailserver v15 refuses to boot Dovecot without at least one
        account and gives a 120s grace window before shutting down. Seeding
        one account on the host before `docker compose up` bypasses this
        chicken-and-egg so the container hits healthy and step 6 can then
        docker exec to add the 100 real mailboxes.

        Returns the generated password, or empty string if a file already
        exists with content (idempotent).
        """
        accounts_file = f"{self._workdir()}/docker-data/dms/config/postfix-accounts.cf"
        self.run(f"mkdir -p $(dirname {accounts_file})")
        rc, _, _ = self.run(f"test -s {accounts_file}", check=False)
        if rc == 0:
            return ""
        email = f"postmaster@mail.{root_domain}"
        password = secrets.token_urlsafe(24)
        _, hash_out, _ = self.run(f"echo '{password}' | openssl passwd -6 -stdin")
        pw_hash = hash_out.strip()
        line = f"{email}|{{SHA512-CRYPT}}{pw_hash}\n"
        self.upload_text(line, accounts_file)
        return password

    def install_dms(
        self,
        root_domain: str,
        le_email: str,
        cf_api_token: str,
        ssl_type: str = "self-signed",
    ) -> None:
        if ssl_type not in ("self-signed", "letsencrypt"):
            raise ValueError(f"ssl_type must be 'self-signed' or 'letsencrypt', got {ssl_type!r}")

        workdir = self._workdir()
        self.run(
            f"mkdir -p {workdir}/docker-data/dms/config "
            f"{workdir}/docker-data/dms/mail-data "
            f"{workdir}/docker-data/dms/mail-state "
            f"{workdir}/docker-data/dms/mail-logs"
        )

        compose = (DMS_TEMPLATE_DIR / "docker-compose.yml").read_text()
        compose = compose.replace("__MAIL_HOSTNAME__", f"mail.{root_domain}")
        compose = compose.replace("__LE_EMAIL__", le_email)
        self.upload_text(compose, f"{workdir}/docker-compose.yml")

        env = (DMS_TEMPLATE_DIR / "mailserver.env").read_text()
        env = env.replace("__MAIL_HOSTNAME__", f"mail.{root_domain}")
        env = env.replace("__SSL_TYPE__", ssl_type)
        self.upload_text(env, f"{workdir}/mailserver.env")

        dovecot_cf = (DMS_TEMPLATE_DIR / "dovecot.cf").read_text()
        self.upload_text(dovecot_cf, f"{workdir}/docker-data/dms/config/dovecot.cf")

        # Stop any existing (possibly crash-looping) container before reconfiguring.
        self.sudo(f"sh -c 'cd {workdir} && docker compose down'", check=False)

        # Acquire certs before starting the container so docker-mailserver's
        # startup checks find them on disk.
        if ssl_type == "letsencrypt":
            self.acquire_letsencrypt_cert(f"mail.{root_domain}", le_email, cf_api_token)
        else:
            self.acquire_self_signed_cert(f"mail.{root_domain}")

        # Seed a postmaster account so Dovecot starts on first boot. Without at
        # least one account, docker-mailserver v15 shuts down after a 120s grace
        # window — and since step 6 (docker exec setup email add) can't run
        # until the container is healthy, we'd deadlock.
        self.seed_postmaster_account(root_domain)

        self.sudo(f"sh -c 'cd {workdir} && docker compose pull'")
        self.sudo(f"sh -c 'cd {workdir} && docker compose up -d'")
        self._wait_for_container("mailserver")
        self.wait_for_mailserver_ready("mailserver")

    def install_mta_sts(self, domain: str, mode: str = "testing", max_age: int = 86400) -> None:
        """Install Caddy on this VPS and configure it to serve the MTA-STS
        policy at https://mta-sts.<domain>/.well-known/mta-sts.txt.

        MTA-STS (RFC 8461) lets us declare 'inbound mail to this domain
        MUST use TLS'. Modern mailbox providers (Gmail, Outlook, Yahoo)
        treat it as a sender-quality signal.

        Caddy handles Let's Encrypt provisioning automatically. The
        mta-sts.<domain> A record must resolve to this VPS *before*
        this call runs — done in the configure_dns step earlier in deploy.

        Starts at mode=testing for safety (policy violations are reported
        via TLS-RPT but not enforced by receivers). Promote to mode=enforce
        after 2-4 weeks of clean operation by re-running with mode='enforce'
        or editing /etc/caddy/Caddyfile directly + `sudo systemctl reload caddy`.
        """
        # Install Caddy from the official cloudsmith repo. Each step is its
        # own sudo call — chaining via && would only elevate the first
        # command in the chain, and dpkg locks on a non-root caller.
        # All steps are individually idempotent so retry on partial failure
        # picks up cleanly.
        self.sudo("apt-get update")
        self.sudo("apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl gnupg")
        self.sudo(
            "bash -c \"curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' "
            "| gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg\""
        )
        self.sudo(
            "bash -c \"curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' "
            "> /etc/apt/sources.list.d/caddy-stable.list\""
        )
        self.sudo("apt-get update")
        self.sudo("apt-get install -y caddy")

        policy_lines = [
            "version: STSv1",
            f"mode: {mode}",
            f"mx: mail.{domain}",
            f"max_age: {max_age}",
        ]
        policy_body = "\n".join(policy_lines) + "\n"

        # Caddy block: serve the policy file on the canonical path,
        # 404 everything else. Caddy auto-acquires + renews the LE cert
        # for mta-sts.<domain>.
        caddyfile = f"""# Managed by ColdEmailInfra — do not edit by hand.
{{
    email ops@{domain}
}}

mta-sts.{domain} {{
    handle /.well-known/mta-sts.txt {{
        header Content-Type "text/plain; charset=utf-8"
        respond <<MTASTS
{policy_body.rstrip()}
MTASTS 200
    }}
    handle {{
        respond "Not found" 404
    }}
}}
"""
        # Upload Caddyfile to /etc/caddy/Caddyfile (requires root)
        self.upload_text(caddyfile, "/tmp/Caddyfile.new")
        self.sudo("mv /tmp/Caddyfile.new /etc/caddy/Caddyfile")
        self.sudo("chown root:root /etc/caddy/Caddyfile && chmod 644 /etc/caddy/Caddyfile")

        # Start + enable
        self.sudo("systemctl enable caddy")
        self.sudo("systemctl restart caddy")

        # Wait a few seconds and verify the policy is being served (LE cert
        # provisioning can take ~10-20s; we don't block on cert here, just
        # confirm Caddy is up).
        time.sleep(3)
        _, status, _ = self.sudo("systemctl is-active caddy", check=False)
        if "active" not in status:
            _, logs, _ = self.sudo("journalctl -u caddy --no-pager -n 30", check=False)
            raise RuntimeError(
                f"Caddy did not start. Status: {status.strip()}\nLast logs:\n{logs}"
            )

    def _wait_for_container(self, name: str, timeout: int = 300) -> None:
        start = time.time()
        last_status = ""
        while time.time() - start < timeout:
            _, out, _ = self.sudo(
                f"docker inspect -f '{{{{.State.Status}}}}' {name}",
                check=False,
            )
            last_status = out.strip()
            if last_status == "running":
                return
            if last_status in ("exited", "dead"):
                _, logs, _ = self.sudo(f"docker logs --tail 80 {name}", check=False)
                raise RuntimeError(
                    f"Container {name} is {last_status}. Last 80 log lines:\n{logs}"
                )
            time.sleep(5)
        _, logs, _ = self.sudo(f"docker logs --tail 80 {name}", check=False)
        raise TimeoutError(
            f"Container {name} did not reach running state within {timeout}s "
            f"(last seen: {last_status}). Last 80 log lines:\n{logs}"
        )

    def wait_for_mailserver_ready(self, name: str = "mailserver", timeout: int = 600) -> None:
        """Wait until the mailserver container's healthcheck reports 'healthy'.

        docker-mailserver's healthcheck verifies Postfix is listening on SMTP,
        which is the earliest point `setup email add` and friends are safe to run.
        Container running != mailserver ready; skipping this wait causes SIGKILL
        on docker exec as the internal config is still generating.
        """
        start = time.time()
        last = ""
        while time.time() - start < timeout:
            _, out, _ = self.sudo(
                f"docker inspect -f '{{{{.State.Status}}}} {{{{.State.Health.Status}}}}' {name}",
                check=False,
            )
            last = out.strip()
            if "exited" in last or "dead" in last:
                _, logs, _ = self.sudo(f"docker logs --tail 80 {name}", check=False)
                raise RuntimeError(f"Container {name}: {last}. Last 80 log lines:\n{logs}")
            if last.endswith(" healthy"):
                return
            time.sleep(10)
        _, logs, _ = self.sudo(f"docker logs --tail 80 {name}", check=False)
        raise TimeoutError(
            f"Container {name} did not become healthy within {timeout}s (last: {last}). "
            f"Last 80 log lines:\n{logs}"
        )

    def add_mailbox(self, email: str, password: str) -> None:
        safe_pw = password.replace("'", "'\\''")
        rc, out, err = self.sudo(
            f"docker exec mailserver setup email add '{email}' '{safe_pw}'",
            check=False,
        )
        if rc == 0:
            return
        combined = f"{out}\n{err}"
        if "already exists" in combined.lower():
            # Persisted from a prior partial run — sync the password to state.
            self.sudo(
                f"docker exec mailserver setup email update '{email}' '{safe_pw}'"
            )
            return
        raise RuntimeError(f"add_mailbox failed ({rc}) for {email}: {combined.strip()}")

    def setup_dkim(self, subdomain_fqdn: str, keysize: int = 2048) -> str:
        """Run DKIM key generation for a given subdomain FQDN; return the public key DNS value."""
        self.sudo(
            f"docker exec mailserver setup config dkim keysize {keysize} domain {subdomain_fqdn}",
            check=False,
        )
        remote_path = f"{self._workdir()}/docker-data/dms/config/opendkim/keys/{subdomain_fqdn}/mail.txt"
        _, out, _ = self.sudo(f"cat {remote_path}")
        return self._parse_dkim_txt(out)

    @staticmethod
    def _parse_dkim_txt(raw: str) -> str:
        inside_quotes = re.findall(r'"([^"]*)"', raw)
        if not inside_quotes:
            raise RuntimeError(f"Could not parse DKIM key from: {raw}")
        return "".join(inside_quotes)

    def close(self) -> None:
        if self.ssh is not None:
            self.ssh.close()
            self.ssh = None
