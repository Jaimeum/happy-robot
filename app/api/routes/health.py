from __future__ import annotations

from fastapi import APIRouter, Depends, Response

from app.api.deps import get_tms
from app.api.schemas import HealthResponse
from app.config import Settings, get_settings
from app.security.auth import require_token
from app.tms.client import TmsClient

router = APIRouter(tags=["health"])


@router.get("/live", include_in_schema=False)
async def liveness() -> Response:
    """The only unauthenticated route. Empty body, discloses nothing."""
    return Response(status_code=204)


@router.get("/health", response_model=HealthResponse, dependencies=[Depends(require_token)])
async def health(
    tms: TmsClient = Depends(get_tms),
    settings: Settings = Depends(get_settings),
) -> HealthResponse:
    dependencies = {
        "tms": "unknown",
        "fmcsa": "configured" if settings.fmcsa_api_key else "missing_api_key",
    }
    try:
        await tms.echo("HEALTH")
        # DEBUG_ECHO bypasses fault injection, so this proves framing, transport
        # and auth only. It says nothing about the operational commands, and is
        # reported that way rather than as a green light for the whole system.
        dependencies["tms"] = "reachable (transport and auth only)"
    except Exception as exc:  # noqa: BLE001 - health must never raise
        dependencies["tms"] = f"unreachable: {type(exc).__name__}"

    return HealthResponse(
        status="ok",
        environment=settings.env,
        dependencies=dependencies,
        tms=tms.stats.snapshot(),
    )
