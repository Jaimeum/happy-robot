from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.api.routes import calls, deals, health, loads, ops, verification
from app.config import get_settings
from app.domain.audit import AuditTrail, LogSink, MemorySink
from app.domain.sessions import SessionStore
from app.integrations.fmcsa import FmcsaClient
from app.security.leak_guard import MaxRateLeakGuard
from app.tms.client import TmsClient

DESCRIPTION = """
Integration layer between the HappyRobot voice platform and HappyRobot
Logistics' systems of record.

Wraps a Legacy TMS that speaks a fixed-width protocol over a raw TCP socket,
adds FMCSA authority verification and an OTP identity gate, and enforces the
brokerage's rate ceiling and three-round negotiation limit in code rather than
in a prompt.

Every route requires a bearer token. The single exception is `/live`, which
returns an empty 204 for the container's own liveness probe.
"""


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-8s %(name)s %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    configure_logging(settings.log_level)

    memory_sink = MemorySink(capacity=settings.audit_buffer_size)
    app.state.memory_sink = memory_sink
    app.state.audit = AuditTrail([LogSink(), memory_sink])
    app.state.sessions = SessionStore(ttl_seconds=settings.session_ttl_seconds)
    app.state.tms = TmsClient(settings)
    app.state.fmcsa = FmcsaClient(settings)

    logger = logging.getLogger("startup")
    logger.info("carrier bridge starting env=%s tms=%s:%s",
                settings.env, settings.tms_host, settings.tms_port)
    if not settings.api_auth_token:
        logger.error("API_AUTH_TOKEN is empty — every request will be refused.")
    if settings.is_production:
        # Call state (the live OTP, the round counter) is held in this process.
        # A second replica would strand calls mid-flight with an unknown call_id,
        # and it would fail silently from the carrier's point of view.
        logger.warning(
            "Session state is in-process: run exactly ONE replica until the store "
            "is externalised. Scale out will break live calls."
        )
    yield


# The voice agent fills tool parameters from what it just heard, and the webhook
# nodes send every one as a JSON string. A parameter the agent did not capture
# therefore arrives as "" rather than absent, and FastAPI's default 422 is a raw
# Pydantic dump. The agent gets a validation blob, no instruction, and nothing to
# say to the carrier — so it stalls or improvises. Every other error path here
# carries agent_guidance; this one is the gap.
_PARAM_REPAIR = {
    "mc_number": "ask for their MC number and read it back to confirm",
    "code": "ask them to read back the six-digit code you sent",
    "carrier_offer": "ask what rate they need, and send it as a plain number",
    "load_id": "confirm which load they are asking about",
    "destination": "ask which email or phone number should receive the code",
    "origin_state": "ask which state they are loading out of",
    "equipment_type": "ask what they pull — dry van, reefer, flatbed, step deck or power only",
}


async def handle_validation_error(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    fields = []
    for error in exc.errors():
        parts = [str(p) for p in error.get("loc", ()) if p not in ("body", "query")]
        if parts:
            fields.append(parts[-1])

    # A missing call_id is not something the carrier can answer. It means the
    # agent lost the id from start_call, so tell it to recover rather than ask.
    if "call_id" in fields:
        guidance = (
            "Internal: the call_id was missing. Call start_call and reuse the "
            "call_id it returns for every later tool. Do not mention this to the "
            "carrier — keep the conversation going."
        )
    else:
        repairs = [_PARAM_REPAIR[f] for f in dict.fromkeys(fields) if f in _PARAM_REPAIR]
        if repairs:
            guidance = (
                "Missing detail before this step can run: "
                + "; ".join(repairs)
                + ". Ask naturally, one question at a time, then call this tool again."
            )
        else:
            guidance = (
                "Something needed for this step was missing or malformed. Ask the "
                "carrier to repeat the last detail, then call this tool again."
            )

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error": "invalid_parameters",
            "message": "One or more tool parameters were missing or malformed.",
            "fields": list(dict.fromkeys(fields)),
            "agent_guidance": guidance,
        },
    )


def create_app() -> FastAPI:
    settings = get_settings()
    # Swagger and the OpenAPI schema are served by FastAPI itself, outside the
    # router dependencies, so they cannot be put behind the bearer token without
    # breaking the browser UI. They publish the whole attack surface, so they are
    # simply not served in production. Locally they are the fastest way in.
    docs_urls: dict[str, str | None] = (
        {"docs_url": None, "redoc_url": None, "openapi_url": None}
        if settings.is_production
        else {"docs_url": "/docs", "redoc_url": "/redoc", "openapi_url": "/openapi.json"}
    )

    app = FastAPI(
        title="HappyRobot Logistics — Carrier Sales Bridge",
        description=DESCRIPTION,
        version="0.1.0",
        lifespan=lifespan,
        **docs_urls,
    )
    app.add_middleware(MaxRateLeakGuard)
    app.add_exception_handler(RequestValidationError, handle_validation_error)

    app.include_router(health.router)
    app.include_router(calls.router)
    app.include_router(verification.router)
    app.include_router(loads.router)
    app.include_router(deals.router)
    app.include_router(ops.router)
    return app


app = create_app()
