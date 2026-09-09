"""Bearer authentication for every route.

The brief says all endpoints require authentication, including health. This
service therefore exposes exactly one unauthenticated path, `/live`, which
returns a bare 200 with no body — it is the container's own liveness probe and
discloses nothing. Everything that reveals state, `/health` included, is behind
the token.
"""

from __future__ import annotations

import hmac

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.config import Settings, get_settings

_scheme = HTTPBearer(auto_error=False, description="Shared bearer token")


async def require_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_scheme),
    settings: Settings = Depends(get_settings),
) -> None:
    expected = settings.api_auth_token
    if not expected:
        # Refuse to run open rather than silently disabling auth.
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="API_AUTH_TOKEN is not configured; refusing to serve requests.",
        )

    presented = credentials.credentials if credentials else request.headers.get("x-api-key", "")
    if not presented or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid credentials.",
            headers={"WWW-Authenticate": "Bearer"},
        )
