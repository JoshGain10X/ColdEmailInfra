#!/usr/bin/env python3
"""Deploy the ColdEmailInfra FastAPI server to a Webdock VPS.

Provisions a VPS, sets up Docker + Caddy (HTTPS reverse proxy),
clones the repo, builds the Docker image, and starts the API.

Accessible at https://infra-api.10xmanagers.com when complete.

Usage:
    python3 scripts/deploy_api_server.py
    python3 scripts/deploy_api_server.py --destroy   # tear down
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import click
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.cloudflare import CloudflareClient
from lib.mailserver import MailserverClient
from lib.webdock import WebdockClient

DISPLAY_NAME = "infra-api"
HOSTNAME = "infra-api.10xmanagers.com"
ZONE_DOMAIN = "10xmanagers.com"


def _load_env() -> None:
    load_dotenv(override=True)
    required = [
        "WEBDOCK_API_TOKEN",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "INFRA_API_KEY",
    ]
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        raise click.ClickException(f"Missing env vars: {', '.join(missing)}")


def _read_env_for_vps() -> str:
    """Build the .env file content to write on the VPS."""
    # Keys to forward from local .env to the VPS
    keys = [
        "INFRA_API_KEY",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_KEY",
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ACCOUNT_ID",
        "WEBDOCK_API_TOKEN",
        "WEBDOCK_PROFILE_SLUG",
        "WEBDOCK_LOCATION_ID",
        "WEBDOCK_IMAGE_SLUG",
        "BISON_API_TOKENS",
        "BISON_API_BASE",
        "REDIRECT_TARGET",
        "DMARC_RUA",
        "LE_EMAIL",
        "SSL_TYPE",
        "DEFAULT_PROVIDER",
        "BLOCKLIST_WEBHOOK_URL",
    ]
    lines = []
    for key in keys:
        val = os.environ.get(key, "")
        if val:
            lines.append(f"{key}={val}")

    # SSH keys on the VPS will be at ~/.ssh/id_ed25519
    lines.append("SSH_PUBLIC_KEY_PATH=~/.ssh/id_ed25519.pub")
    lines.append("SSH_PRIVATE_KEY_PATH=~/.ssh/id_ed25519")

    # CORS — allow the CRM frontend
    cors = os.environ.get("CORS_ORIGINS", "*")
    lines.append(f"CORS_ORIGINS={cors}")

    return "\n".join(lines) + "\n"


def step_provision(wb: WebdockClient) -> tuple[str, str, str | None]:
    """Provision VPS. Returns (instance_id, ip, bootstrap_password)."""
    click.echo("[1/6] Provisioning Webdock VPS...")

    # Check for existing instance (try multiple slug patterns Webdock may assign)
    existing = None
    for slug_try in ["infraapi", "infraapi1", "infra-api"]:
        try:
            inst = wb.get_instance(slug_try)
            if inst.get("status") == "running" and inst.get("ip"):
                existing = inst
                break
        except Exception:
            pass
    if not existing:
        existing = wb.find_instance_by_display_name(DISPLAY_NAME)
    if existing and existing.get("ip"):
        click.echo(f"  Found existing instance {existing['id']} at {existing['ip']}")
        return existing["id"], existing["ip"], None

    # Upload SSH key
    ssh_pub_path = Path(os.environ.get("SSH_PUBLIC_KEY_PATH", "~/.ssh/id_ed25519.pub")).expanduser()
    public_key = ssh_pub_path.read_text().strip()
    ssh_key_id = wb.find_or_create_ssh_key("infra-api", public_key)

    product_id = os.environ.get("WEBDOCK_PROFILE_SLUG", "vps-epyc-advanced-2025")
    region = os.environ.get("WEBDOCK_LOCATION_ID", "dk")
    image_id = os.environ.get("WEBDOCK_IMAGE_SLUG", "webdock-ubuntu-jammy-cloud")

    if existing:
        instance_id = existing["id"]
        click.echo(f"  Found existing instance {instance_id}, waiting for ready...")
    else:
        click.echo(f"  Creating instance (profile={product_id}, region={region})...")
        inst = wb.create_instance(DISPLAY_NAME, product_id, region, ssh_key_id, image_id)
        instance_id = inst["id"]

    click.echo(f"  Instance {instance_id} created, waiting for IP...")
    inst = wb.wait_for_instance_ready(instance_id)
    ip = inst["ip"]
    click.echo(f"  VPS ready at {ip}")

    # Create shell user
    creds = wb.ensure_ssh_user(instance_id, ssh_key_id, username="admin")
    click.echo(f"  SSH user 'admin' ready")

    # Wait for SSH port
    click.echo(f"  Waiting for SSH on {ip}...")
    wb.wait_for_ssh(ip)

    return instance_id, ip, creds.get("password")


def step_dns(ip: str) -> None:
    """Create A record for infra-api.10xmanagers.com."""
    click.echo("[2/6] Creating DNS record...")
    cf = CloudflareClient()
    zone_id = cf.get_zone_id(ZONE_DOMAIN)
    if not zone_id:
        raise click.ClickException(f"Zone {ZONE_DOMAIN} not found in Cloudflare")

    # Not proxied — Caddy handles TLS directly
    cf.upsert_record(zone_id, "A", HOSTNAME, ip, proxied=False)
    click.echo(f"  A record: {HOSTNAME} -> {ip} (not proxied)")


def step_bootstrap(ip: str, password: str | None) -> MailserverClient:
    """SSH in, bootstrap sudo, wait for cloud-init, install Docker + Caddy."""
    click.echo("[3/6] Bootstrapping server...")
    ssh_key = os.environ.get("SSH_PRIVATE_KEY_PATH", "~/.ssh/id_ed25519")
    ms = MailserverClient(ip, ssh_key, user="admin")
    ms.connect()

    if password:
        click.echo("  Bootstrapping passwordless sudo...")
        ms.bootstrap_passwordless_sudo(password)

    click.echo("  Waiting for cloud-init...")
    ms.wait_for_cloud_init()

    # Install Docker
    click.echo("  Installing Docker...")
    ms.run("command -v docker >/dev/null || (curl -fsSL https://get.docker.com | sudo -n sh)")
    ms.sudo("systemctl enable --now docker")
    ms.sudo(f"usermod -aG docker admin", check=False)

    # Install Caddy
    click.echo("  Installing Caddy...")
    ms.sudo("apt-get update -qq", check=False)
    ms.sudo("apt-get install -y debian-keyring debian-archive-keyring apt-transport-https curl")
    ms.run(
        "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' "
        "| sudo gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg --yes"
    )
    ms.run(
        "curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' "
        "| sudo tee /etc/apt/sources.list.d/caddy-stable.list"
    )
    ms.sudo("apt-get update -qq")
    ms.sudo("apt-get install -y caddy")

    # Install git (for cloning the repo)
    ms.sudo("apt-get install -y git", check=False)

    return ms


def step_deploy_app(ms: MailserverClient) -> None:
    """Clone repo, write .env, generate SSH key, build and run Docker container."""
    click.echo("[4/6] Deploying application...")

    home = ms._home_dir()
    app_dir = f"{home}/ColdEmailInfra"

    # Clone or pull the repo
    rc, _, _ = ms.run(f"test -d {app_dir}/.git", check=False)
    if rc == 0:
        click.echo("  Pulling latest code...")
        ms.run(f"cd {app_dir} && git pull")
    else:
        click.echo("  Cloning repository...")
        # Use HTTPS clone (no deploy key needed for public-ish repos, or
        # the user can set up a deploy key). Try the org repo first.
        repo_url = "https://github.com/JoshGain10X/ColdEmailInfra.git"
        ms.run(f"git clone {repo_url} {app_dir}")

    # Write .env
    click.echo("  Writing .env file...")
    env_content = _read_env_for_vps()
    ms.upload_text(env_content, f"{app_dir}/.env")

    # Generate SSH keypair on VPS (for API to SSH into shard VPSes)
    rc, _, _ = ms.run(f"test -f {home}/.ssh/id_ed25519", check=False)
    if rc != 0:
        click.echo("  Generating SSH keypair on VPS...")
        ms.run(f'ssh-keygen -t ed25519 -f {home}/.ssh/id_ed25519 -N ""')

    # Build Docker image (Dockerfile is in api/ but context is project root)
    click.echo("  Building Docker image (this may take a few minutes)...")
    ms.sudo(f"sh -c 'cd {app_dir} && docker build -f api/Dockerfile -t coldemail-api .'")

    # Stop existing container if any
    ms.sudo("docker rm -f coldemail-api 2>/dev/null", check=False)

    # Run container — bind to localhost only, Caddy will proxy
    click.echo("  Starting container...")
    ms.sudo(
        f"docker run -d --name coldemail-api "
        f"--restart unless-stopped "
        f"--env-file {app_dir}/.env "
        f"-v {home}/.ssh:/root/.ssh:ro "
        f"-p 127.0.0.1:8000:8000 "
        f"coldemail-api"
    )

    # Wait for container to be running
    for _ in range(30):
        _, out, _ = ms.sudo("docker inspect -f '{{.State.Status}}' coldemail-api", check=False)
        if out.strip() == "running":
            break
        time.sleep(2)
    else:
        _, logs, _ = ms.sudo("docker logs --tail 40 coldemail-api", check=False)
        raise click.ClickException(f"Container not running. Logs:\n{logs}")

    click.echo("  Container running")


def step_caddy(ms: MailserverClient) -> None:
    """Configure Caddy as HTTPS reverse proxy."""
    click.echo("[5/6] Configuring Caddy reverse proxy...")

    caddyfile = f"""{HOSTNAME} {{
    reverse_proxy 127.0.0.1:8000
}}
"""
    ms.upload_text(caddyfile, "/tmp/Caddyfile")
    ms.sudo("cp /tmp/Caddyfile /etc/caddy/Caddyfile")
    ms.sudo("systemctl reload caddy")

    # Give Caddy a moment to provision the TLS cert
    click.echo("  Caddy reloaded — TLS cert will be provisioned automatically")


def step_verify(ip: str) -> None:
    """Hit the health endpoint to verify the deployment."""
    click.echo("[6/6] Verifying deployment...")
    import requests

    # Try direct IP first (bypasses DNS propagation)
    for attempt in range(10):
        try:
            resp = requests.get(f"http://{ip}:8000/api/health", timeout=5)
            if resp.status_code == 200:
                click.echo(f"  Direct health check passed: {resp.json()}")
                break
        except Exception:
            pass
        time.sleep(3)
    else:
        click.echo("  WARNING: Direct health check failed (container may still be starting)")

    # Try via HTTPS (may fail if DNS hasn't propagated yet)
    try:
        resp = requests.get(f"https://{HOSTNAME}/api/health", timeout=10)
        click.echo(f"  HTTPS health check: {resp.status_code} {resp.json()}")
    except Exception as exc:
        click.echo(f"  HTTPS not yet reachable ({exc}) — DNS may still be propagating")
        click.echo(f"  Try: curl https://{HOSTNAME}/api/health")

    click.echo("")
    click.echo("=" * 60)
    click.echo(f"  API URL  : https://{HOSTNAME}")
    click.echo(f"  VPS IP   : {ip}")
    click.echo(f"  Health   : https://{HOSTNAME}/api/health")
    click.echo("")
    click.echo("  Set in Lovable:")
    click.echo(f"    VITE_INFRA_API_URL = https://{HOSTNAME}")
    click.echo(f"    VITE_INFRA_API_KEY = <your INFRA_API_KEY>")
    click.echo("=" * 60)


def do_destroy() -> None:
    """Tear down the API server VPS and DNS record."""
    _load_env()
    wb = WebdockClient()
    cf = CloudflareClient()

    existing = wb.find_instance_by_display_name(DISPLAY_NAME)
    if existing:
        click.echo(f"Destroying VPS {existing['id']}...")
        wb.destroy_instance(existing["id"])
        click.echo("  VPS destroyed")
    else:
        click.echo("No VPS found with that name")

    zone_id = cf.get_zone_id(ZONE_DOMAIN)
    if zone_id:
        records = cf.list_records(zone_id, name=HOSTNAME)
        for r in records:
            click.echo(f"Deleting DNS record: {r['type']} {r['name']} -> {r.get('content')}")
            cf._request("DELETE", f"/zones/{zone_id}/dns_records/{r['id']}")
    click.echo("Done")


@click.command()
@click.option("--destroy", is_flag=True, help="Tear down the API server VPS and DNS")
def main(destroy: bool) -> None:
    if destroy:
        do_destroy()
        return

    _load_env()
    wb = WebdockClient()

    instance_id, ip, password = step_provision(wb)
    step_dns(ip)
    ms = step_bootstrap(ip, password)
    try:
        step_deploy_app(ms)
        step_caddy(ms)
    finally:
        ms.close()
    step_verify(ip)


if __name__ == "__main__":
    main()
