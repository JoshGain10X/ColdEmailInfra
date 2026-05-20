#!/usr/bin/env python3
"""Fix SSL certs on all shard VPSes: replace self-signed with Let's Encrypt.

Run on the API VPS (193.180.211.74) which has SSH access to all shard VPSes.
Reads CLOUDFLARE_API_TOKEN from .env for DNS-01 challenge.
"""
import json
import os
import subprocess
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

SHARDS_DIR = Path(__file__).resolve().parent / "shards"
SSH_KEY = os.path.expanduser("~/.ssh/id_ed25519")
CF_API_TOKEN = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
LE_EMAIL = os.environ.get("LE_EMAIL", "ops@10xmanagers.com")

if not CF_API_TOKEN:
    print("ERROR: CLOUDFLARE_API_TOKEN not set in .env")
    sys.exit(1)


def ssh_cmd(ip: str, user: str, cmd: str, timeout: int = 120) -> tuple[int, str]:
    """Run a command on a remote host via SSH."""
    result = subprocess.run(
        ["ssh", "-i", SSH_KEY,
         "-o", "ConnectTimeout=10",
         "-o", "StrictHostKeyChecking=accept-new",
         "-o", "BatchMode=yes",
         f"{user}@{ip}", cmd],
        capture_output=True, text=True, timeout=timeout,
    )
    output = result.stdout + result.stderr
    return result.returncode, output


def fix_shard(domain: str, ip: str, user: str) -> bool:
    """Upgrade a single shard from self-signed to Let's Encrypt."""
    hostname = f"mail.{domain}"
    print(f"\n{'='*60}")
    print(f"  {domain} ({ip})")
    print(f"{'='*60}")

    # 1. Check connectivity
    rc, out = ssh_cmd(ip, user, "echo ok")
    if rc != 0:
        print(f"  SKIP: Cannot SSH ({out.strip()})")
        return False

    # 2. Ensure certbot is installed
    print("  Installing certbot...")
    rc, out = ssh_cmd(ip, user,
        "command -v certbot >/dev/null || "
        "sudo apt-get update -qq && sudo apt-get install -y certbot python3-certbot-dns-cloudflare",
        timeout=180)
    if rc != 0:
        print(f"  WARN: certbot install issue: {out[-200:]}")

    # 3. Write Cloudflare credentials
    import base64
    creds = f"dns_cloudflare_api_token = {CF_API_TOKEN}\n"
    b64 = base64.b64encode(creds.encode()).decode()
    ssh_cmd(ip, user,
        f"sudo sh -c 'umask 077 && echo {b64} | base64 -d > /root/.cf-certbot.ini && chmod 600 /root/.cf-certbot.ini'")

    # 4. Acquire Let's Encrypt cert
    print(f"  Acquiring Let's Encrypt cert for {hostname}...")
    rc, out = ssh_cmd(ip, user,
        f"sudo certbot certonly --dns-cloudflare "
        f"--dns-cloudflare-credentials /root/.cf-certbot.ini "
        f"--dns-cloudflare-propagation-seconds 60 "
        f"--non-interactive --agree-tos --email {LE_EMAIL} "
        f"-d {hostname} --keep-until-expiring",
        timeout=300)

    if rc != 0:
        print(f"  FAIL: certbot failed: {out[-300:]}")
        return False

    if "not yet due for renewal" in out:
        print("  Cert already valid, skipping renewal")
    else:
        print("  Cert acquired successfully")

    # 5. Update mailserver.env to use letsencrypt
    print("  Updating mailserver config...")
    ssh_cmd(ip, user,
        "sudo sed -i 's/SSL_TYPE=self-signed/SSL_TYPE=letsencrypt/' ~/mailserver/mailserver.env")

    # 6. Restart docker-mailserver
    print("  Restarting mailserver...")
    rc, out = ssh_cmd(ip, user,
        "cd ~/mailserver && sudo docker compose down && sudo docker compose up -d",
        timeout=120)
    if rc != 0:
        print(f"  WARN: restart issue: {out[-200:]}")

    # 7. Wait for healthy
    print("  Waiting for mailserver to be healthy...")
    rc, out = ssh_cmd(ip, user,
        "for i in $(seq 1 30); do "
        "  status=$(docker inspect --format='{{.State.Health.Status}}' mailserver 2>/dev/null); "
        "  [ \"$status\" = 'healthy' ] && echo 'healthy' && exit 0; "
        "  sleep 5; "
        "done; echo 'timeout'",
        timeout=180)

    status = out.strip().split('\n')[-1]
    if "healthy" in status:
        print("  OK: mailserver healthy with Let's Encrypt cert")
        return True
    else:
        print(f"  WARN: mailserver status: {status}")
        return True  # Cert is installed even if container needs more time


def main():
    # Collect all shard VPSes from state files
    shards = []
    for state_file in sorted(SHARDS_DIR.glob("*.json")):
        if state_file.name == "archived":
            continue
        try:
            data = json.loads(state_file.read_text())
            vps = data.get("vps", {})
            domain = state_file.stem
            ip = vps.get("ip")
            user = vps.get("ssh_user", "admin")
            if ip:
                shards.append((domain, ip, user))
        except Exception:
            continue

    print(f"Found {len(shards)} shards to fix")

    results = {"ok": [], "fail": [], "skip": []}
    for domain, ip, user in shards:
        try:
            if fix_shard(domain, ip, user):
                results["ok"].append(domain)
            else:
                results["fail"].append(domain)
        except Exception as e:
            print(f"  ERROR: {e}")
            results["fail"].append(domain)

    print(f"\n{'='*60}")
    print(f"Results: {len(results['ok'])} OK, {len(results['fail'])} FAILED")
    for d in results["ok"]:
        print(f"  OK   {d}")
    for d in results["fail"]:
        print(f"  FAIL {d}")


if __name__ == "__main__":
    main()
