from __future__ import annotations

import os
import socket
import time
import uuid
from typing import Any

import requests


AUTH_URL = "https://auth.contabo.com/auth/realms/contabo/protocol/openid-connect/token"
API_BASE = "https://api.contabo.com/v1"


class ContaboClient:
    """Contabo Cloud VPS API client.

    Uses OAuth2 password grant (client_credentials + username/password).
    Docs: https://api.contabo.com/
    """

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        api_user: str | None = None,
        api_password: str | None = None,
    ):
        self.client_id = client_id or os.environ["CONTABO_CLIENT_ID"]
        self.client_secret = client_secret or os.environ["CONTABO_CLIENT_SECRET"]
        self.api_user = api_user or os.environ["CONTABO_API_USER"]
        self.api_password = api_password or os.environ["CONTABO_API_PASSWORD"]
        self._token: str | None = None
        self._token_expires: float = 0.0
        self.session = requests.Session()

    def _token_valid(self) -> bool:
        return self._token is not None and time.time() < self._token_expires - 30

    def _refresh_token(self) -> None:
        resp = requests.post(
            AUTH_URL,
            data={
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "username": self.api_user,
                "password": self.api_password,
                "grant_type": "password",
            },
            timeout=30,
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        self._token_expires = time.time() + int(body.get("expires_in", 300))

    def _headers(self) -> dict[str, str]:
        if not self._token_valid():
            self._refresh_token()
        return {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
            "x-request-id": str(uuid.uuid4()),
        }

    def _request(self, method: str, path: str, **kwargs) -> dict[str, Any]:
        resp = self.session.request(
            method,
            f"{API_BASE}{path}",
            headers={**self._headers(), **kwargs.pop("headers", {})},
            timeout=60,
            **kwargs,
        )
        if resp.status_code == 204 or not resp.content:
            resp.raise_for_status()
            return {}
        if resp.status_code >= 400:
            raise requests.HTTPError(f"{resp.status_code} {resp.text}", response=resp)
        return resp.json()

    # ------------------------------------------------------------------
    # SSH keys ("secrets" in Contabo's API)
    # ------------------------------------------------------------------

    def find_or_create_ssh_key(self, name: str, public_key: str) -> int:
        body = self._request("GET", "/secrets", params={"type": "ssh", "size": 100})
        for s in body.get("data", []):
            if s.get("value", "").strip() == public_key.strip():
                return s["secretId"]
        body = self._request("POST", "/secrets", json={
            "name": name,
            "type": "ssh",
            "value": public_key,
        })
        return body["data"][0]["secretId"]

    # ------------------------------------------------------------------
    # Compute instances
    # ------------------------------------------------------------------

    def create_instance(
        self,
        display_name: str,
        product_id: str,
        region: str,
        ssh_key_id: int,
        image_id: str,
        period: int = 1,
    ) -> dict:
        payload = {
            "displayName": display_name,
            "productId": product_id,
            "region": region,
            "period": period,
            "imageId": image_id,
            "sshKeys": [ssh_key_id],
        }
        body = self._request("POST", "/compute/instances", json=payload)
        return body["data"][0]

    def get_instance(self, instance_id: int) -> dict:
        body = self._request("GET", f"/compute/instances/{instance_id}")
        return body["data"][0]

    def wait_for_instance_ready(self, instance_id: int, timeout: int = 900) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            inst = self.get_instance(instance_id)
            status = inst.get("status")
            ip = (inst.get("ipConfig", {}).get("v4", {}) or {}).get("ip")
            if status == "running" and ip:
                return inst
            time.sleep(15)
        raise TimeoutError(f"Instance {instance_id} not ready within {timeout}s")

    def wait_for_ssh(self, ip: str, port: int = 22, timeout: int = 600) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((ip, port), timeout=5):
                    return
            except OSError:
                time.sleep(5)
        raise TimeoutError(f"SSH on {ip}:{port} not reachable within {timeout}s")

    def set_ptr(self, instance_id: int, hostname: str) -> None:
        """Set reverse DNS on the instance's primary IPv4."""
        inst = self.get_instance(instance_id)
        v4 = inst.get("ipConfig", {}).get("v4", {})
        ip = v4.get("ip")
        if not ip:
            raise RuntimeError(f"Instance {instance_id} has no IPv4 address yet")
        self._request(
            "PATCH",
            f"/compute/instances/{instance_id}",
            json={"displayName": inst.get("displayName")},
        )
        self._request(
            "PUT",
            f"/compute/instances/{instance_id}/v1/reverse-dns",
            json={"ipv4": {"ip": ip, "ptr": hostname}},
        )

    def destroy_instance(self, instance_id: int) -> None:
        self._request("DELETE", f"/compute/instances/{instance_id}")
