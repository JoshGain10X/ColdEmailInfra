"""FastAPI server wrapping ColdEmailInfra scripts for CRM integration.

Endpoints trigger background jobs that import and run existing scripts/lib/
functions directly, with progress tracked in Supabase for real-time UI updates.
"""
import os
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from supabase import create_client

from auth import verify_api_key
from jobs import run_deploy, run_destroy, run_verify, run_load_to_bison, run_domain_sync, run_domain_register

load_dotenv(override=True)

app = FastAPI(title="ColdEmailInfra API", version="1.0.0")

# CORS — allow the CRM frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=os.environ.get("CORS_ORIGINS", "*").split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------

def _sb():
    return create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])


def _create_job(job_type: str, domain: str, total_steps: int, created_by: Optional[str] = None) -> str:
    """Insert a new infra_jobs row and return its id."""
    sb = _sb()
    row = sb.table("infra_jobs").insert({
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
    domain: str
    provider: str = "webdock"
    product_id: Optional[str] = None
    region: Optional[str] = None
    image_id: Optional[str] = None
    ssl_type: str = "self-signed"
    created_by: Optional[str] = None


class LoadToBisonRequest(BaseModel):
    workspace: Optional[str] = None
    tag: str = "Custom SMTP"
    created_by: Optional[str] = None


class ActionRequest(BaseModel):
    created_by: Optional[str] = None


class RegisterDomainRequest(BaseModel):
    domain: str
    created_by: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/api/deploy")
def deploy(req: DeployRequest, bg: BackgroundTasks, _: str = Depends(verify_api_key)):
    """Deploy a new shard. Returns immediately with a job ID for tracking."""
    provider = req.provider

    # Resolve defaults from env (mirrors deploy_shard.py CLI logic)
    if provider == "contabo":
        product_id = req.product_id or os.environ.get("CONTABO_PRODUCT_ID", "V91")
        region = req.region or os.environ.get("CONTABO_REGION", "EU")
        image_id = req.image_id or os.environ.get("CONTABO_IMAGE_ID", "d64d5c6c-9dda-4e38-8174-0ee282474d8a")
    else:
        product_id = req.product_id or os.environ.get("WEBDOCK_PROFILE_SLUG")
        region = req.region or os.environ.get("WEBDOCK_LOCATION_ID")
        image_id = req.image_id or os.environ.get("WEBDOCK_IMAGE_SLUG")
        missing = [n for n, v in [("product_id", product_id), ("region", region), ("image_id", image_id)] if not v]
        if missing:
            raise HTTPException(400, f"Missing required fields for Webdock: {', '.join(missing)}. "
                                     "Set WEBDOCK_PROFILE_SLUG / WEBDOCK_LOCATION_ID / WEBDOCK_IMAGE_SLUG in .env "
                                     "or pass them in the request.")

    job_id = _create_job("deploy", req.domain, total_steps=10, created_by=req.created_by)
    bg.add_task(run_deploy, job_id, req.domain, provider, product_id, region, image_id, req.ssl_type)
    return {"job_id": job_id, "domain": req.domain, "status": "pending"}


@app.post("/api/destroy/{domain}")
def destroy(domain: str, bg: BackgroundTasks, req: Optional[ActionRequest] = None, _: str = Depends(verify_api_key)):
    """Destroy a shard (VPS + DNS). Returns immediately with a job ID."""
    created_by = req.created_by if req else None
    job_id = _create_job("destroy", domain, total_steps=4, created_by=created_by)
    bg.add_task(run_destroy, job_id, domain)
    return {"job_id": job_id, "domain": domain, "status": "pending"}


@app.post("/api/verify/{domain}")
def verify(domain: str, bg: BackgroundTasks, req: Optional[ActionRequest] = None, _: str = Depends(verify_api_key)):
    """Run verification checks on a deployed shard."""
    created_by = req.created_by if req else None
    job_id = _create_job("verify", domain, total_steps=5, created_by=created_by)
    bg.add_task(run_verify, job_id, domain)
    return {"job_id": job_id, "domain": domain, "status": "pending"}


@app.post("/api/load-to-bison/{domain}")
def load_to_bison(domain: str, bg: BackgroundTasks, req: Optional[LoadToBisonRequest] = None, _: str = Depends(verify_api_key)):
    """Bulk-upload shard mailboxes to Email Bison."""
    workspace = req.workspace if req else None
    tag = req.tag if req else "Custom SMTP"
    created_by = req.created_by if req else None
    job_id = _create_job("load_bison", domain, total_steps=6, created_by=created_by)
    bg.add_task(run_load_to_bison, job_id, domain, workspace, tag)
    return {"job_id": job_id, "domain": domain, "status": "pending"}


@app.get("/api/shards")
def list_shards(_: str = Depends(verify_api_key)):
    """List all shards from the infra_shards table."""
    sb = _sb()
    result = sb.table("infra_shards").select("*").order("created_at", desc=True).execute()
    return {"shards": result.data}


@app.get("/api/shards/{domain}")
def get_shard(domain: str, _: str = Depends(verify_api_key)):
    """Get details for a single shard."""
    sb = _sb()
    result = sb.table("infra_shards").select("*").eq("domain", domain).execute()
    if not result.data:
        raise HTTPException(404, f"Shard {domain} not found")
    return {"shard": result.data[0]}


@app.get("/api/shards/{domain}/csv")
def download_shard_csv(domain: str, _: str = Depends(verify_api_key)):
    """Generate a signed URL for the shard's CSV and return it."""
    sb = _sb()
    result = sb.table("infra_shards").select("csv_storage_path").eq("domain", domain).execute()
    if not result.data or not result.data[0].get("csv_storage_path"):
        raise HTTPException(404, f"No CSV found for shard {domain}")
    path = result.data[0]["csv_storage_path"]
    signed = sb.storage.from_("shard-csvs").create_signed_url(path, 300)
    if not signed or not signed.get("signedURL"):
        raise HTTPException(500, "Failed to generate download URL")
    return {"url": signed["signedURL"], "filename": f"{domain}_bison.csv"}


@app.get("/api/jobs")
def list_jobs(limit: int = 50, _: str = Depends(verify_api_key)):
    """List recent jobs."""
    sb = _sb()
    result = (
        sb.table("infra_jobs")
        .select("*")
        .order("created_at", desc=True)
        .limit(limit)
        .execute()
    )
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
def list_domains(_: str = Depends(verify_api_key)):
    """List all domains from the infra_domains cache table."""
    sb = _sb()
    result = sb.table("infra_domains").select("*").order("domain").execute()
    return {"domains": result.data}


@app.post("/api/domains/refresh")
def refresh_domains(bg: BackgroundTasks, req: Optional[ActionRequest] = None, _: str = Depends(verify_api_key)):
    """Trigger a background sync of all Cloudflare zones into infra_domains."""
    created_by = req.created_by if req else None
    job_id = _create_job("domain_sync", "all", total_steps=3, created_by=created_by)
    bg.add_task(run_domain_sync, job_id)
    return {"job_id": job_id, "status": "pending"}


@app.post("/api/domains/check")
def check_domain(req: RegisterDomainRequest, _: str = Depends(verify_api_key)):
    """Check if a domain is available for registration via Cloudflare Registrar."""
    from lib.cloudflare import CloudflareClient
    cf = CloudflareClient()

    # First check if we already own it
    zone_id = cf.get_zone_id(req.domain)
    if zone_id:
        return {"domain": req.domain, "available": False, "reason": "Already on your Cloudflare account"}

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
    """Register a domain via Cloudflare Registrar. Returns a job ID for tracking."""
    job_id = _create_job("domain_register", req.domain, total_steps=4, created_by=req.created_by)
    bg.add_task(run_domain_register, job_id, req.domain)
    return {"job_id": job_id, "domain": req.domain, "status": "pending"}


@app.get("/api/health")
def health():
    """Unauthenticated health check."""
    return {"status": "ok"}
