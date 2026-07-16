from __future__ import annotations

import os
import re
import secrets
import socket
import string
import time
from typing import Any

# Explicit submodule imports — webdock 1.0.1's __init__.py is empty, so
# `import webdock; webdock.Webdock` fails. Reaching into `webdock.webdock`
# and `webdock.exceptions` works on every 1.0.x release.
from webdock.exceptions import WebdockException
from webdock.webdock import Webdock as _WebdockSDK


class WebdockClient:
    """Webdock.io VPS API client.

    Thin wrapper over the official `webdock` Python SDK
    (https://github.com/webdock-io/python-sdk) that presents the same
    surface as ContaboClient so deploy_shard.py can swap providers
    without caring which underlies.

    All instance-returning methods produce a canonical shape:
        {"id": <slug>, "ip": <ipv4|None>, "status": <str>,
         "display_name": <str>}
    `id` is the server slug (Webdock's primary identifier).
    """

    def __init__(self, token: str | None = None):
        tok = (token or os.environ["WEBDOCK_API_TOKEN"]).strip()
        self._sdk = _WebdockSDK(tok)

    @staticmethod
    def _unwrap(resp: Any) -> Any:
        if isinstance(resp, dict) and "data" in resp:
            return resp["data"]
        return resp

    @staticmethod
    def _extract_ipv4(raw: dict) -> str | None:
        """Webdock's server response field for the primary IPv4 is documented
        as `ipv4` but has historically shifted between releases. Probe the
        common names so a doc mismatch doesn't silently hang wait_for_ready.
        """
        for key in ("ipv4", "ipv4Address", "ip", "mainIp", "ipAddress"):
            v = raw.get(key)
            if isinstance(v, str) and v:
                return v
        return None

    @staticmethod
    def _extract_ipv6(raw: dict) -> str | None:
        """IPv6 equivalent. Webdock returns ipv6 as a string like
        '2a0f:0f01:0207:573::0'. Filter out the literal placeholder
        '::0' which sometimes shows up before the v6 stack is fully
        assigned during provisioning.

        Also reject /64-base addresses whose host portion is all-zero
        (e.g. '2a0f:f01:208:5bd::'). Webdock reports the network base,
        which nothing binds to — advertising it as an AAAA points clients
        at a dead address (see api/jobs.py mail_host note / Jul 2026 outage).
        """
        import ipaddress
        placeholders = {"::0", "::", "0:0:0:0:0:0:0:0"}
        for key in ("ipv6", "ipv6Address", "mainIpv6"):
            v = raw.get(key)
            if not (isinstance(v, str) and v) or v in placeholders:
                continue
            try:
                addr = ipaddress.IPv6Address(v.split("/")[0])
            except ValueError:
                continue
            # host portion of the /64 all-zero -> network base, not a real host
            if (int(addr) & ((1 << 64) - 1)) == 0:
                continue
            return v
        return None

    @staticmethod
    def _normalise(raw: dict) -> dict:
        return {
            "id": raw.get("slug") or raw.get("id"),
            "ip": WebdockClient._extract_ipv4(raw),
            "ip6": WebdockClient._extract_ipv6(raw),
            "status": raw.get("status"),
            "display_name": raw.get("name"),
            # pendingDeletion=true means a DELETE has been accepted and the
            # server revokes at month-end - teardown treats this as destroyed.
            "pendingDeletion": raw.get("pendingDeletion"),
            "nextActionDate": raw.get("nextActionDate"),
        }

    # ------------------------------------------------------------------
    # SSH keys (Webdock: account/publicKeys)
    # ------------------------------------------------------------------

    def find_or_create_ssh_key(self, name: str, public_key: str) -> int:
        public_key_stripped = public_key.strip()
        existing = self._unwrap(self._sdk.get_pubkeys()) or []
        for key in existing:
            if key.get("name") == name or key.get("publicKey", "").strip() == public_key_stripped:
                return key["id"]
        created = self._unwrap(self._sdk.create_key(name, public_key))
        return created["id"]

    # ------------------------------------------------------------------
    # Compute servers
    # ------------------------------------------------------------------

    @staticmethod
    def _slugify(name: str) -> str:
        """Turn an FQDN like mail.foo.co.uk into a Webdock-legal slug.

        Webdock slugs must be lowercase alphanumeric + hyphens, <=63 chars.
        """
        s = re.sub(r"[^a-zA-Z0-9-]+", "-", name).strip("-").lower()
        return (s or "server")[:63]

    def create_instance(
        self,
        display_name: str,
        product_id: str,
        region: str,
        ssh_key_id: int,
        image_id: str,
        period: int = 1,  # unused on Webdock (pay-as-you-go by the hour)
    ) -> dict:
        """Provision a new server. Maps our cross-provider args to Webdock's shape:
          display_name -> name (also drives PTR auto-derivation)
          product_id   -> profileSlug (e.g. 'webdocknano-2024')
          region       -> locationId  (e.g. 'fi', 'nl', 'us', 'uk')
          image_id     -> imageSlug   (e.g. 'ubuntu-jammy-cloud')
          ssh_key_id   -> publicKeys=[ssh_key_id]
        """
        slug = self._slugify(display_name)
        payload = {
            "name": display_name,
            "slug": slug,
            "locationId": region,
            "profileSlug": product_id,
            "imageSlug": image_id,
            "publicKeys": [ssh_key_id],
        }
        resp = self._unwrap(self._sdk.provision_server(payload))
        return self._normalise(resp)

    def get_instance(self, instance_id: str) -> dict:
        """instance_id is the Webdock slug."""
        resp = self._unwrap(self._sdk.get_server(instance_id))
        return self._normalise(resp)

    def ensure_ssh_user(self, instance_id: str, ssh_key_id: int, username: str = "admin") -> dict:
        """Create a sudoer shell user on the VM with our SSH key attached.

        Webdock's cloud images (webdock-ubuntu-*-cloud) ship with no shell
        user at all — SSH fails with 'Permission denied (publickey)' on
        every possible username until you create one via the API or
        dashboard. The `publicKeys` array on POST /servers uploads the key
        to the account library but does not attach it to any VM user.

        Returns a dict of the form {"username": str, "password": str | None}.
        `password` is the generated sudo password on first creation (so the
        caller can bootstrap passwordless sudo over SSH by echoing it into
        `sudo -S`). If the user already existed — we cannot recover their
        password — returns None, and passwordless sudo must be enabled
        manually in the dashboard.
        """
        existing = self._unwrap(self._sdk.get_shellusers(instance_id)) or []
        for u in existing:
            if u.get("username") == username:
                return {"username": username, "password": None}
        # Webdock password policy: letters + digits only, no punctuation.
        alphabet = string.ascii_letters + string.digits
        password = "".join(secrets.choice(alphabet) for _ in range(32))
        self._sdk.create_shelluser(
            serverSlug=instance_id,
            username=username,
            password=password,
            group="sudo",
            shell="/bin/bash",
            publicKeys=[ssh_key_id],
        )
        return {"username": username, "password": password}

    def find_instance_by_display_name(self, display_name: str) -> dict | None:
        """Look up a server by its slug (derived from display_name).

        Webdock deletes are synchronous and release the slug, so unlike
        Contabo there's no 'cancelled-but-visible' state to filter out.
        """
        slug = self._slugify(display_name)
        try:
            return self.get_instance(slug)
        except WebdockException as exc:
            if "404" in str(exc):
                return None
            raise

    def wait_for_instance_ready(self, instance_id: str, timeout: int = 900) -> dict:
        """Poll Webdock until the server is provisioned and has an IP.

        Webdock provision is async; `provision_server` returns a 202 and
        the VPS takes 2-5 minutes to become reachable. Status transitions
        through 'provisioning' → 'running'.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            inst = self.get_instance(instance_id)
            if inst.get("status") == "running" and inst.get("ip"):
                return inst
            time.sleep(15)
        raise TimeoutError(f"Webdock server {instance_id} not ready within {timeout}s")

    def wait_for_ssh(self, ip: str, port: int = 22, timeout: int = 600) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((ip, port), timeout=5):
                    return
            except OSError:
                time.sleep(5)
        raise TimeoutError(f"SSH on {ip}:{port} not reachable within {timeout}s")

    # ------------------------------------------------------------------
    # Reverse DNS (Webdock derives PTR from the Server Identity's main domain)
    # ------------------------------------------------------------------

    def get_ptr(self, ip: str) -> str | None:
        """Webdock doesn't expose a 'current PTR' read endpoint, so fall back
        to a public DNS query. During the 30-60 min propagation window after
        set_ptr this will return None; that's fine — the deploy script's
        set_ptr path handles None as 'needs updating'.
        """
        try:
            import dns.resolver
            import dns.reversename
            rev = dns.reversename.from_address(ip)
            return str(dns.resolver.resolve(rev, "PTR")[0]).rstrip(".")
        except Exception:
            return None

    def set_ptr(self, instance_id: str, hostname: str) -> None:
        """Set the server's Server Identity main domain, which Webdock uses
        to derive rDNS (PTR).

        Webdock's PTR is NOT driven by the server's `name` field (that's a
        display label); it's driven by Server Identity → Main Domain. The
        endpoint isn't in the Python SDK, so we call it through the SDK's
        generic `make_request` helper.

        If the best-guess endpoint returns 4xx, we log a warning and
        continue — rDNS is cosmetic until outbound mail actually starts
        (days later via Bison warmup), and the user can fix it in the
        dashboard as a fallback.
        """
        payload = {"maindomain": hostname}
        try:
            self._sdk.make_request(
                f"servers/{instance_id}/identity",
                requestType="PATCH",
                data=payload,
            )
            return
        except WebdockException as exc:
            # Fall through to a second guess below; Webdock's naming has
            # shifted between "identity" and "mainDomain" over releases.
            first_error = str(exc)

        try:
            self._sdk.make_request(
                f"servers/{instance_id}/mainDomain",
                requestType="PATCH",
                data=payload,
            )
        except WebdockException as exc:
            raise RuntimeError(
                f"Could not set Server Identity via API for {instance_id} "
                f"(tried /identity -> {first_error}; /mainDomain -> {exc}). "
                f"Set it manually: Webdock dashboard -> {instance_id} -> "
                f"Server Identity -> Main Domain = {hostname}."
            )

    def destroy_instance(self, instance_id: str) -> None:
        """Hard delete. Webdock refunds unused prepaid credit on destroy —
        unlike Contabo, no rename-before-cancel hack is needed because the
        slug is released immediately.
        """
        try:
            self._sdk.delete_server(instance_id)
        except WebdockException as exc:
            # 404 = already deleted; treat as success
            if "404" in str(exc):
                return
            raise
