"""Simple API key authentication middleware."""
from __future__ import annotations

import os

from fastapi import HTTPException, Security
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

_security = HTTPBearer()


def verify_api_key(
    credentials: HTTPAuthorizationCredentials = Security(_security),
) -> str:
    """Validate the bearer token against INFRA_API_KEY env var."""
    expected = os.environ.get("INFRA_API_KEY", "")
    if not expected:
        raise HTTPException(500, "INFRA_API_KEY not configured on server")
    if credentials.credentials != expected:
        raise HTTPException(401, "Invalid API key")
    return credentials.credentials
