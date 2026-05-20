"""Multi-tenant client context loader.

A ClientContext bundles everything a deploy/verify/destroy/load_bison job
needs for one client: ready-made Webdock and Cloudflare clients, all
configured Bison workspaces, signature formulas, and deploy defaults
(region, plan, mailbox count, redirect URL).

Background job functions in api/jobs.py construct one of these at the
start of each run and pass it through to the underlying scripts.lib/*
modules. The lib clients already accept explicit credentials, so no
change to their interfaces is needed.

Credentials are stored in Supabase Vault and decrypted on-demand via
the public.get_decrypted_secret(uuid) RPC.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

from supabase import Client, create_client

from lib.bison_api import BisonClient
from lib.cloudflare import CloudflareClient
from lib.webdock import WebdockClient


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class BisonWorkspace:
    id: str
    name: str
    workspace_id: Optional[str]
    purpose: Optional[str]
    base_url: str
    is_default: bool
    _api_key: str = field(repr=False)

    def client(self) -> BisonClient:
        return BisonClient(token=self._api_key, base_url=self.base_url)


@dataclass
class SignatureFormula:
    style: str  # 'html' | 'plaintext'
    company_names: list[str]
    titles: list[str]
    quotes: list[str]
    optouts: list[str]
    include_pronouns_rate: float
    include_quote_rate: float
    include_email_rate: float
    format_variants: int


@dataclass
class ClientContext:
    client_id: str
    slug: str
    name: str
    redirect_url: Optional[str]
    vps_region: str
    vps_plan: str
    vps_image_slug: Optional[str]
    mailbox_count: int
    subdomain_count: int
    default_daily_limit: int
    dmarc_rua: str
    le_email: str
    ssl_type: str
    cloudflare: CloudflareClient
    webdock: Optional[WebdockClient]
    workspaces: list[BisonWorkspace]
    default_workspace: Optional[BisonWorkspace]
    default_signature: SignatureFormula
    workspace_signature_overrides: dict[str, SignatureFormula]

    def workspace(self, name: Optional[str] = None) -> BisonWorkspace:
        if name:
            for w in self.workspaces:
                if w.name == name:
                    return w
            raise ValueError(
                f"Bison workspace {name!r} not found for client {self.slug!r}. "
                f"Configured: {[w.name for w in self.workspaces]}"
            )
        if not self.default_workspace:
            raise ValueError(
                f"Client {self.slug!r} has no default Bison workspace. "
                "Set one via UPDATE client_bison_workspaces SET is_default=true WHERE ..."
            )
        return self.default_workspace

    def signature_for_workspace(self, workspace_id: str) -> SignatureFormula:
        return self.workspace_signature_overrides.get(workspace_id, self.default_signature)


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _supabase() -> Client:
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_KEY"],
    )


def _decrypt(sb: Client, secret_id: Optional[str]) -> Optional[str]:
    if not secret_id:
        return None
    result = sb.rpc("get_decrypted_secret", {"secret_id": secret_id}).execute()
    return result.data


def _build_signature(row: dict) -> SignatureFormula:
    return SignatureFormula(
        style=row["style"],
        company_names=row.get("company_names") or [],
        titles=row.get("titles") or [],
        quotes=row.get("quotes") or [],
        optouts=row.get("optouts") or [],
        include_pronouns_rate=float(row.get("include_pronouns_rate") or 0),
        include_quote_rate=float(row.get("include_quote_rate") or 0),
        include_email_rate=float(row.get("include_email_rate") or 0),
        format_variants=int(row.get("format_variants") or 1),
    )


def _load_by_query(sb: Client, where_column: str, where_value: str) -> ClientContext:
    # Client + Cloudflare account
    client_row = (
        sb.table("clients")
        .select(
            "id, slug, name, website_url, "
            "cloudflare_account:cloudflare_accounts!cloudflare_account_id(slug, api_token_secret_id, account_id)"
        )
        .eq(where_column, where_value)
        .single()
        .execute()
        .data
    )
    if not client_row:
        raise LookupError(f"No client with {where_column}={where_value!r}")

    cf_secret_id = client_row["cloudflare_account"]["api_token_secret_id"]
    cf_token = _decrypt(sb, cf_secret_id)
    if not cf_token:
        raise RuntimeError(
            f"Cloudflare token for account {client_row['cloudflare_account']['slug']!r} not set "
            "(client_credentials.api_token_secret_id is NULL or vault decryption returned NULL)"
        )
    cf = CloudflareClient(token=cf_token, account_id=client_row["cloudflare_account"]["account_id"])

    # Webdock (optional; some clients don't have a Webdock yet)
    cred_row = (
        sb.table("client_credentials")
        .select("webdock_api_token_secret_id")
        .eq("client_id", client_row["id"])
        .execute()
        .data
    )
    webdock = None
    if cred_row and cred_row[0].get("webdock_api_token_secret_id"):
        webdock_token = _decrypt(sb, cred_row[0]["webdock_api_token_secret_id"])
        if webdock_token:
            webdock = WebdockClient(token=webdock_token)

    # Settings
    settings = (
        sb.table("client_settings")
        .select("*")
        .eq("client_id", client_row["id"])
        .single()
        .execute()
        .data
    )

    # Bison workspaces (filter status='active' so paused/archived are hidden)
    workspace_rows = (
        sb.table("client_bison_workspaces")
        .select("id, workspace_name, workspace_id, purpose, base_url, is_default, api_key_secret_id, status")
        .eq("client_id", client_row["id"])
        .eq("status", "active")
        .execute()
        .data
    ) or []

    workspaces: list[BisonWorkspace] = []
    default_workspace: Optional[BisonWorkspace] = None
    for row in workspace_rows:
        token = _decrypt(sb, row["api_key_secret_id"])
        if not token:
            # Skip workspaces with no key — surfaces clearly when ctx.workspace() is called
            continue
        ws = BisonWorkspace(
            id=row["id"],
            name=row["workspace_name"],
            workspace_id=row.get("workspace_id"),
            purpose=row.get("purpose"),
            base_url=row["base_url"],
            is_default=row["is_default"],
            _api_key=token,
        )
        workspaces.append(ws)
        if ws.is_default:
            default_workspace = ws

    # Signature formulas (default + per-workspace overrides)
    formula_rows = (
        sb.table("signature_formulas")
        .select("*")
        .eq("client_id", client_row["id"])
        .execute()
        .data
    ) or []

    default_signature: Optional[SignatureFormula] = None
    overrides: dict[str, SignatureFormula] = {}
    for row in formula_rows:
        sig = _build_signature(row)
        ws_id = row.get("client_bison_workspace_id")
        if ws_id is None:
            default_signature = sig
        else:
            overrides[ws_id] = sig

    if default_signature is None:
        # Fallback: build an empty plaintext default so jobs don't NPE
        default_signature = SignatureFormula(
            style="plaintext",
            company_names=[client_row["name"]],
            titles=[],
            quotes=[],
            optouts=[],
            include_pronouns_rate=0,
            include_quote_rate=0,
            include_email_rate=0,
            format_variants=1,
        )

    # Derive a host-like string for dmarc_rua/le_email defaults when not explicitly set.
    # website_url may be NULL on the row — coerce via `or ''` before stripping.
    website_host = (
        (client_row.get("website_url") or "")
        .replace("https://", "")
        .replace("http://", "")
        .rstrip("/")
    )
    default_email_host = website_host or client_row["slug"]

    return ClientContext(
        client_id=client_row["id"],
        slug=client_row["slug"],
        name=client_row["name"],
        redirect_url=settings.get("redirect_url"),
        vps_region=settings.get("vps_region") or "dk",
        vps_plan=settings.get("vps_plan") or "webdocknano",
        vps_image_slug=settings.get("vps_image_slug"),
        mailbox_count=int(settings.get("mailbox_count") or 100),
        subdomain_count=int(settings.get("subdomain_count") or 20),
        default_daily_limit=int(settings.get("default_daily_limit") or 10),
        dmarc_rua=settings.get("dmarc_rua") or f"dmarc@{default_email_host}",
        le_email=settings.get("le_email") or f"ops@{default_email_host}",
        ssl_type=settings.get("ssl_type") or "letsencrypt",
        cloudflare=cf,
        webdock=webdock,
        workspaces=workspaces,
        default_workspace=default_workspace,
        default_signature=default_signature,
        workspace_signature_overrides=overrides,
    )


def load_client_context_by_slug(slug: str) -> ClientContext:
    return _load_by_query(_supabase(), "slug", slug)


def load_client_context_by_id(client_id: str) -> ClientContext:
    return _load_by_query(_supabase(), "id", client_id)


def load_client_context_for_shard(domain: str) -> ClientContext:
    """For domain-keyed routes like /destroy/{domain}: look up the shard's
    client_id, then load that client's context.
    """
    sb = _supabase()
    shard = (
        sb.table("infra_shards")
        .select("client_id")
        .eq("domain", domain)
        .execute()
        .data
    )
    if not shard:
        raise LookupError(f"No shard for domain {domain!r}")
    return _load_by_query(sb, "id", shard[0]["client_id"])
