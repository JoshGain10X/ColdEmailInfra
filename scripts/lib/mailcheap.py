import os
import socket
import time
from typing import Any

import requests


class MailcheapClient:
    """Thin wrapper over the Mailcheap provisioning API.

    Mailcheap's public API surface is documented at https://api.mailcheap.co/.
    Endpoint shapes below reflect the v1 REST layout; adjust `MAILCHEAP_API_URL`
    via env if the account uses a different base.
    """

    def __init__(self, api_key: str | None = None, api_url: str | None = None):
        self.api_key = api_key or os.environ["MAILCHEAP_API_KEY"]
        self.api_url = (api_url or os.environ.get("MAILCHEAP_API_URL", "https://api.mailcheap.co/v1")).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        })

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        resp = self.session.request(method, f"{self.api_url}{path}", timeout=60, **kwargs)
        resp.raise_for_status()
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    def create_ssh_key(self, name: str, public_key: str) -> str:
        body = self._request("POST", "/ssh-keys", json={"name": name, "public_key": public_key})
        return body["id"]

    def find_or_create_ssh_key(self, name: str, public_key: str) -> str:
        keys = self._request("GET", "/ssh-keys").get("data", [])
        for k in keys:
            if k.get("public_key", "").strip() == public_key.strip():
                return k["id"]
        return self.create_ssh_key(name, public_key)

    def create_vps(self, hostname: str, plan: str, region: str, ssh_key_id: str, image: str = "ubuntu-22.04") -> dict:
        payload = {
            "hostname": hostname,
            "plan": plan,
            "region": region,
            "image": image,
            "ssh_keys": [ssh_key_id],
        }
        body = self._request("POST", "/servers", json=payload)
        return body.get("data", body)

    def get_vps(self, vps_id: str) -> dict:
        body = self._request("GET", f"/servers/{vps_id}")
        return body.get("data", body)

    def wait_for_vps_ready(self, vps_id: str, timeout: int = 600) -> dict:
        start = time.time()
        while time.time() - start < timeout:
            vps = self.get_vps(vps_id)
            if vps.get("status") == "active" and vps.get("ipv4_address"):
                return vps
            time.sleep(10)
        raise TimeoutError(f"VPS {vps_id} did not become ready within {timeout}s")

    def wait_for_ssh(self, ip: str, port: int = 22, timeout: int = 600) -> None:
        start = time.time()
        while time.time() - start < timeout:
            try:
                with socket.create_connection((ip, port), timeout=5):
                    return
            except OSError:
                time.sleep(5)
        raise TimeoutError(f"SSH on {ip}:{port} not reachable within {timeout}s")

    def set_ptr(self, vps_id: str, hostname: str) -> None:
        self._request("PATCH", f"/servers/{vps_id}", json={"reverse_dns": hostname})

    def destroy_vps(self, vps_id: str) -> None:
        self._request("DELETE", f"/servers/{vps_id}")
