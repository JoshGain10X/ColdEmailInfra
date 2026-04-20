from __future__ import annotations

import io
import re
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

    def install_docker(self) -> None:
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

    def install_dms(self, root_domain: str, le_email: str, cf_api_token: str) -> None:
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
        self.upload_text(env, f"{workdir}/mailserver.env")

        # Stop any existing (possibly crash-looping) container before reconfiguring.
        self.sudo(f"sh -c 'cd {workdir} && docker compose down'", check=False)

        # Acquire Let's Encrypt cert via DNS-01 (no port 80 needed, works even
        # if the hostname's A record isn't yet pointing at this VPS).
        self.acquire_letsencrypt_cert(f"mail.{root_domain}", le_email, cf_api_token)

        self.sudo(f"sh -c 'cd {workdir} && docker compose pull'")
        self.sudo(f"sh -c 'cd {workdir} && docker compose up -d'")
        self._wait_for_container("mailserver")
        self.wait_for_mailserver_ready("mailserver")

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
        self.sudo(f"docker exec mailserver setup email add '{email}' '{safe_pw}'")

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
