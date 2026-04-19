from __future__ import annotations

import io
import re
import time
from pathlib import Path

import paramiko


REPO_ROOT = Path(__file__).resolve().parents[2]
DMS_TEMPLATE_DIR = REPO_ROOT / "docker-mailserver"


class MailserverClient:
    def __init__(self, ip: str, ssh_key_path: str, user: str = "root"):
        self.ip = ip
        self.user = user
        self.ssh_key_path = Path(ssh_key_path).expanduser()
        self.ssh: paramiko.SSHClient | None = None

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
        self.run("command -v docker >/dev/null || (curl -fsSL https://get.docker.com | sh)")
        self.run("systemctl enable --now docker")

    def install_dms(self, root_domain: str, le_email: str) -> None:
        self.run("mkdir -p /opt/mailserver/docker-data/dms/config /opt/mailserver/docker-data/dms/mail-data /opt/mailserver/docker-data/dms/mail-state /opt/mailserver/docker-data/dms/mail-logs")

        compose = (DMS_TEMPLATE_DIR / "docker-compose.yml").read_text()
        compose = compose.replace("__MAIL_HOSTNAME__", f"mail.{root_domain}")
        compose = compose.replace("__LE_EMAIL__", le_email)
        self.upload_text(compose, "/opt/mailserver/docker-compose.yml")

        env = (DMS_TEMPLATE_DIR / "mailserver.env").read_text()
        env = env.replace("__MAIL_HOSTNAME__", f"mail.{root_domain}")
        self.upload_text(env, "/opt/mailserver/mailserver.env")

        self.run("cd /opt/mailserver && docker compose pull")
        self.run("cd /opt/mailserver && docker compose up -d")
        self._wait_for_container("mailserver")

    def _wait_for_container(self, name: str, timeout: int = 180) -> None:
        start = time.time()
        while time.time() - start < timeout:
            _, out, _ = self.run(
                f"docker inspect -f '{{{{.State.Health.Status}}}}' {name} 2>/dev/null || docker inspect -f '{{{{.State.Status}}}}' {name}",
                check=False,
            )
            status = out.strip()
            if status in ("healthy", "running"):
                return
            time.sleep(5)
        raise TimeoutError(f"Container {name} did not become healthy within {timeout}s")

    def add_mailbox(self, email: str, password: str) -> None:
        safe_pw = password.replace("'", "'\\''")
        self.run(f"docker exec mailserver setup email add '{email}' '{safe_pw}'")

    def setup_dkim(self, subdomain_fqdn: str, keysize: int = 2048) -> str:
        """Run DKIM key generation for a given subdomain FQDN; return the public key DNS value."""
        self.run(
            f"docker exec mailserver setup config dkim keysize {keysize} domain {subdomain_fqdn}",
            check=False,
        )
        remote_path = f"/opt/mailserver/docker-data/dms/config/opendkim/keys/{subdomain_fqdn}/mail.txt"
        _, out, _ = self.run(f"cat {remote_path}")
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
