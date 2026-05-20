"""FastAPI server wrapping ColdEmailInfra scripts for CRM/skill integration.

Multi-tenant v2: every endpoint that triggers work takes a client_slug
(or implicitly resolves one from the target domain via the shard row).
Per-client credentials and settings come from the Cold Email Data
Supabase project via ClientContext.
"""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from supabase import create_client

# Make scripts/lib importable so we can use load_client_context_* helpers
_SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from auth import verify_api_key
from jobs import run_deploy, run_destroy, run_verify, run_load_to_bison, run_domain_sync, run_domain_register
from lib.client_context import load_client_context_by_slug, load_client_context_for_shard

load_dotenv(override=True)

app = FastAPI(title="ColdEmailInfra API", version="2.0.0")

# CORS — allow the CRM / Claude skill caller
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Supabase + client resolution helpers
# ---------------------------------------------------------------------------

def _sb():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def _resolve_client_id(slug: str) -> str:
    """Slug -> client_id. Raises 404 if not found."""
    sb = _sb()
    row = sb.table("clients").select("id").eq("slug", slug).execute()
    if not row.data:
        raise HTTPException(404, f"Client {slug!r} not found")
    return row.data[0]["id"]


def _resolve_client_id_from_domain(domain: str) -> str:
    """Look up the client_id that owns a given shard domain. Raises 404
    if no shard found.
    """
    sb = _sb()
    row = sb.table("infra_shards").select("client_id").eq("domain", domain).execute()
    if not row.data:
        raise HTTPException(404, f"No shard found for domain {domain!r}")
    if len(row.data) > 1:
        # Shouldn't happen with the (client_id, domain) unique constraint, but
        # surface clearly if it ever does
        raise HTTPException(
            409,
            f"Domain {domain!r} owned by {len(row.data)} clients — "
            "pass client_slug explicitly to disambiguate.",
        )
    return row.data[0]["client_id"]


def _create_job(
    job_type: str,
    client_id: str,
    domain: str,
    total_steps: int,
    created_by: Optional[str] = None,
) -> str:
    """Insert a new infra_jobs row and return its id."""
    sb = _sb()
    row = sb.table("infra_jobs").insert({
        "client_id": client_id,
        "type": job_type,
        "domain": domain,
        "status": "pending",
        "progress_step": 0,
        "progress_total": total_steps,
        "logs": [],
        "created_by": created_by,
    }).execute()
    return row.data[0]["id"]


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------

class DeployRequest(BaseModel):
    client_slug: str
    domain: str
    provider: str = "webdock"
    product_id: Optional[str] = None
    region: Optional[str] = None
    image_id: Optional[str] = None
    ssl_type: Optional[str] = None  # falls back to client_settings.ssl_type
    created_by: Optional[str] = None


class LoadToBisonRequest(BaseModel):
    workspace: Optional[str] = None  # workspace name; default = client's is_default workspace
    tag: str = "Custom SMTP"
    created_by: Optional[str] = None
    client_slug: Optional[str] = None  # override — inferred from shard if absent


class ActionRequest(BaseModel):
    created_by: Optional[str] = None
    client_slug: Optional[str] = None  # override — inferred from shard if absent


class DomainSyncRequest(BaseModel):
    client_slug: str  # which Cloudflare account to sync (via this client's CF account)
    default_client_slug_for_new: Optional[str] = None
    created_by: Optional[str] = None


class DomainCheckRequest(BaseModel):
    client_slug: str
    domain: str


class RegisterDomainRequest(BaseModel):
    client_slug: str
    domain: str
    created_by: Optional[str] = None


class AddBisonWorkspaceRequest(BaseModel):
    client_slug: str
    api_key: str
    purpose: Optional[str] = None  # 'cold_outbound' | 'warming' | etc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/deploy")
def deploy(req: DeployRequest, bg: BackgroundTasks, _: str = Depends(verify_api_key)):
    """Deploy a new shard for a client. Returns immediately with a job ID.

    client_slug is required. product_id/region/image_id/ssl_type are
    optional; if omitted they fall back to client_settings values.
    """
    client_id = _resolve_client_id(req.client_slug)
    provider = req.provider

    if provider == "contabo":
        # Contabo is the legacy fallback provider; env-driven defaults remain
        product_id = req.product_id or os.environ.get("CONTABO_PRODUCT_ID", "V91")
        region = req.region or os.environ.get("CONTABO_REGION", "EU")
        image_id = req.image_id or os.environ.get("CONTABO_IMAGE_ID", "d64d5c6c-9dda-4e38-8174-0ee282474d8a")
    # For webdock, run_deploy fills missing values from ClientContext

    job_id = _create_job("deploy", client_id, req.domain, total_steps=10, created_by=req.created_by)
    bg.add_task(
        run_deploy, job_id, client_id, req.domain,
        provider, req.product_id, req.region, req.image_id, req.ssl_type,
    )
    return {"job_id": job_id, "domain": req.domain, "client_slug": req.client_slug, "status": "pending"}


@app.post("/api/destroy/{domain}")
def destroy(domain: str, bg: BackgroundTasks, req: Optional[ActionRequest] = None, _: str = Depends(verify_api_key)):
    """Destroy a shard. Client inferred from the shard row unless client_slug
    is passed explicitly in the body (rare disambiguation case).
    """
    created_by = req.created_by if req else None
    if req and req.client_slug:
        client_id = _resolve_client_id(req.client_slug)
    else:
        client_id = _resolve_client_id_from_domain(domain)
    job_id = _create_job("destroy", client_id, domain, total_steps=4, created_by=created_by)
    bg.add_task(run_destroy, job_id, client_id, domain)
    return {"job_id": job_id, "domain": domain, "status": "pending"}


@app.post("/api/verify/{domain}")
def verify(domain: str, bg: BackgroundTasks, req: Optional[ActionRequest] = None, _: str = Depends(verify_api_key)):
    """Run verification checks on a deployed shard."""
    created_by = req.created_by if req else None
    if req and req.client_slug:
        client_id = _resolve_client_id(req.client_slug)
    else:
        client_id = _resolve_client_id_from_domain(domain)
    job_id = _create_job("verify", client_id, domain, total_steps=5, created_by=created_by)
    bg.add_task(run_verify, job_id, client_id, domain)
    return {"job_id": job_id, "domain": domain, "status": "pending"}


@app.post("/api/load-to-bison/{domain}")
def load_to_bison(domain: str, bg: BackgroundTasks, req: Optional[LoadToBisonRequest] = None, _: str = Depends(verify_api_key)):
    """Bulk-upload shard mailboxes to the client's Bison workspace.

    Workspace selection: if `workspace` is in the body, that named workspace
    is targeted; otherwise the client's is_default workspace.
    """
    workspace = req.workspace if req else None
    tag = req.tag if req else "Custom SMTP"
    created_by = req.created_by if req else None
    if req and req.client_slug:
        client_id = _resolve_client_id(req.client_slug)
    else:
        client_id = _resolve_client_id_from_domain(domain)
    job_id = _create_job("load_bison", client_id, domain, total_steps=6, created_by=created_by)
    bg.add_task(run_load_to_bison, job_id, client_id, domain, workspace, tag)
    return {"job_id": job_id, "domain": domain, "status": "pending"}


@app.get("/api/shards")
def list_shards(client_slug: Optional[str] = None, _: str = Depends(verify_api_key)):
    """List shards. Pass ?client_slug=... to filter to one client."""
    sb = _sb()
    query = sb.table("infra_shards").select("*")
    if client_slug:
        query = query.eq("client_id", _resolve_client_id(client_slug))
    result = query.order("created_at", desc=True).execute()
    return {"shards": result.data}


@app.get("/api/shards/{domain}")
def get_shard(domain: str, client_slug: Optional[str] = None, _: str = Depends(verify_api_key)):
    """Get details for a single shard. ?client_slug=... if domain is ambiguous."""
    sb = _sb()
    query = sb.table("infra_shards").select("*").eq("domain", domain)
    if client_slug:
        query = query.eq("client_id", _resolve_client_id(client_slug))
    result = query.execute()
    if not result.data:
        raise HTTPException(404, f"Shard {domain} not found")
    if len(result.data) > 1:
        raise HTTPException(409, f"Multiple shards for {domain}; add ?client_slug=...")
    return {"shard": result.data[0]}


@app.get("/api/shards/{domain}/csv")
def download_shard_csv(domain: str, client_slug: Optional[str] = None, _: str = Depends(verify_api_key)):
    """Generate a signed URL for the shard's CSV."""
    sb = _sb()
    query = sb.table("infra_shards").select("csv_storage_path").eq("domain", domain)
    if client_slug:
        query = query.eq("client_id", _resolve_client_id(client_slug))
    result = query.execute()
    if not result.data or not result.data[0].get("csv_storage_path"):
        raise HTTPException(404, f"No CSV found for shard {domain}")
    path = result.data[0]["csv_storage_path"]
    signed = sb.storage.from_("shard-csvs").create_signed_url(path, 300)
    if not signed or not signed.get("signedURL"):
        raise HTTPException(500, "Failed to generate download URL")
    return {"url": signed["signedURL"], "filename": f"{domain}_bison.csv"}


@app.get("/api/jobs")
def list_jobs(client_slug: Optional[str] = None, limit: int = 50, _: str = Depends(verify_api_key)):
    """List recent jobs. Filter to one client with ?client_slug=..."""
    sb = _sb()
    query = sb.table("infra_jobs").select("*")
    if client_slug:
        query = query.eq("client_id", _resolve_client_id(client_slug))
    result = query.order("created_at", desc=True).limit(limit).execute()
    return {"jobs": result.data}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, _: str = Depends(verify_api_key)):
    """Get a single job with full logs."""
    sb = _sb()
    result = sb.table("infra_jobs").select("*").eq("id", job_id).execute()
    if not result.data:
        raise HTTPException(404, f"Job {job_id} not found")
    return {"job": result.data[0]}


@app.get("/api/domains")
def list_domains(client_slug: Optional[str] = None, _: str = Depends(verify_api_key)):
    """List domains. ?client_slug=... to filter to one client."""
    sb = _sb()
    query = sb.table("infra_domains").select("*")
    if client_slug:
        query = query.eq("client_id", _resolve_client_id(client_slug))
    result = query.order("domain").execute()
    return {"domains": result.data}


@app.post("/api/domains/refresh")
def refresh_domains(req: DomainSyncRequest, bg: BackgroundTasks, _: str = Depends(verify_api_key)):
    """Background sync of one Cloudflare account's zones into infra_domains.

    The client_slug determines which CF account is queried (via that client's
    CF account FK). New zones found are attributed to default_client_slug_for_new
    (or to client_slug if not specified).
    """
    client_id = _resolve_client_id(req.client_slug)
    default_new_id = (
        _resolve_client_id(req.default_client_slug_for_new)
        if req.default_client_slug_for_new else client_id
    )
    job_id = _create_job("domain_sync", client_id, "all", total_steps=3, created_by=req.created_by)
    bg.add_task(run_domain_sync, job_id, client_id, default_new_id)
    return {"job_id": job_id, "status": "pending"}


@app.post("/api/domains/check")
def check_domain(req: DomainCheckRequest, _: str = Depends(verify_api_key)):
    """Availability check against the client's Cloudflare account."""
    ctx = load_client_context_by_slug(req.client_slug)
    cf = ctx.cloudflare

    zone_id = cf.get_zone_id(req.domain)
    if zone_id:
        return {"domain": req.domain, "available": False, "reason": "Already on this Cloudflare account"}

    try:
        avail = cf.registrar_check_availability(req.domain)
        result = {"domain": req.domain, "available": avail.get("available", False)}
        for key in ("price", "price_unknown", "reason", "note"):
            if key in avail:
                result[key] = avail[key]
        return result
    except Exception as exc:
        raise HTTPException(400, f"Availability check failed: {exc}")


@app.post("/api/domains/register")
def register_domain(req: RegisterDomainRequest, bg: BackgroundTasks, _: str = Depends(verify_api_key)):
    """Register a domain via the client's Cloudflare Registrar account."""
    client_id = _resolve_client_id(req.client_slug)
    job_id = _create_job("domain_register", client_id, req.domain, total_steps=4, created_by=req.created_by)
    bg.add_task(run_domain_register, job_id, client_id, req.domain)
    return {"job_id": job_id, "domain": req.domain, "client_slug": req.client_slug, "status": "pending"}


# ---------------------------------------------------------------------------
# Bison Workspace Management
# ---------------------------------------------------------------------------

@app.get("/api/bison/workspaces")
def list_bison_workspaces(client_slug: Optional[str] = None, _: str = Depends(verify_api_key)):
    """List Bison workspaces. Never returns API keys.

    Pass ?client_slug=... to filter to one client.
    """
    sb = _sb()
    query = sb.table("client_bison_workspaces").select(
        "id, client_id, workspace_name, workspace_id, base_url, purpose, is_default, status, created_at"
    )
    if client_slug:
        query = query.eq("client_id", _resolve_client_id(client_slug))
    result = query.order("created_at").execute()
    return {"workspaces": result.data}


@app.post("/api/bison/workspaces")
def add_bison_workspace(req: AddBisonWorkspaceRequest, _: str = Depends(verify_api_key)):
    """Add a Bison workspace for a client.

    Validates the api_key against Bison's API, then stores the key in
    Vault and inserts a client_bison_workspaces row.
    """
    import subprocess
    import json as _json

    client_id = _resolve_client_id(req.client_slug)
    base_url = os.environ.get("BISON_API_BASE", "https://send.spamproofed.com").rstrip("/")
    cmd = [
        "curl", "-s", "-X", "GET", f"{base_url}/api/workspaces/v1.1",
        "-H", f"Authorization: Bearer {req.api_key}",
        "-H", "Accept: application/json",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    try:
        body = _json.loads(result.stdout)
    except Exception:
        raise HTTPException(400, "Invalid response from Bison API — check the key")

    workspaces = body.get("data") or []
    if len(workspaces) == 0:
        raise HTTPException(400, "API key returned no workspaces — invalid key")
    if len(workspaces) > 1:
        raise HTTPException(400, "This is a super-admin key (multiple workspaces). Use a per-workspace key.")

    ws = workspaces[0]
    ws_name = ws.get("name", "Unknown")
    ws_id = ws.get("id") or ws.get("_id")

    sb = _sb()
    existing = (
        sb.table("client_bison_workspaces")
        .select("id")
        .eq("client_id", client_id)
        .eq("workspace_name", ws_name)
        .execute()
    )
    if existing.data:
        raise HTTPException(409, f"Workspace '{ws_name}' already exists for this client")

    # Store the key in Vault via the create_vault_secret RPC, then reference
    # the returned uuid on the row.
    secret_name = f"bison_{req.client_slug}_{ws_name}".replace(" ", "_").replace("/", "_").lower()
    secret_id = sb.rpc("create_vault_secret", {
        "secret_value": req.api_key,
        "secret_name": secret_name,
    }).execute().data

    row = sb.table("client_bison_workspaces").insert({
        "client_id": client_id,
        "workspace_name": ws_name,
        "workspace_id": str(ws_id) if ws_id else None,
        "api_key_secret_id": secret_id,
        "base_url": base_url,
        "purpose": req.purpose,
        "is_default": False,
    }).execute()

    return {"workspace": {
        "id": row.data[0]["id"],
        "client_id": client_id,
        "workspace_name": ws_name,
        "workspace_id": str(ws_id) if ws_id else None,
        "is_default": False,
    }}


@app.delete("/api/bison/workspaces/{workspace_id}")
def delete_bison_workspace(workspace_id: str, _: str = Depends(verify_api_key)):
    """Remove a Bison workspace configuration. The Vault secret is left
    in place — operator can vacuum it later if desired."""
    sb = _sb()
    existing = sb.table("client_bison_workspaces").select("id").eq("id", workspace_id).execute()
    if not existing.data:
        raise HTTPException(404, "Workspace not found")
    sb.table("client_bison_workspaces").delete().eq("id", workspace_id).execute()
    return {"ok": True}


@app.put("/api/bison/workspaces/{workspace_id}/default")
def set_default_bison_workspace(workspace_id: str, _: str = Depends(verify_api_key)):
    """Set a workspace as its client's default. Other workspaces for that
    client are demoted automatically (uniq_default_bison_workspace_per_client
    enforces only one default per client)."""
    sb = _sb()
    row = sb.table("client_bison_workspaces").select("client_id").eq("id", workspace_id).execute()
    if not row.data:
        raise HTTPException(404, "Workspace not found")
    client_id = row.data[0]["client_id"]
    # Demote all of this client's current defaults
    sb.table("client_bison_workspaces").update({"is_default": False}).eq("client_id", client_id).eq("is_default", True).execute()
    sb.table("client_bison_workspaces").update({"is_default": True}).eq("id", workspace_id).execute()
    return {"ok": True}


@app.get("/api/clients")
def list_clients(_: str = Depends(verify_api_key)):
    """List clients with brief summary stats."""
    sb = _sb()
    result = (
        sb.table("clients")
        .select("id, slug, name, status, website_url, onboarded_at, created_at")
        .order("created_at")
        .execute()
    )
    return {"clients": result.data}


@app.get("/api/health")
def health():
    """Unauthenticated health check."""
    return {"status": "ok", "version": "2.0.0"}
