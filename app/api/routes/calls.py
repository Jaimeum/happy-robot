from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_audit, get_memory_sink, get_sessions, load_session
from app.api.schemas import (
    CallTrailResponse,
    CloseCallRequest,
    StartCallRequest,
    StartCallResponse,
)
from app.domain.audit import AuditTrail, EventType, MemorySink
from app.domain.sessions import Outcome, SessionStore, Stage
from app.security.auth import require_token

router = APIRouter(prefix="/v1/calls", tags=["calls"], dependencies=[Depends(require_token)])


@router.post("/start", response_model=StartCallResponse, status_code=201)
async def start_call(
    payload: StartCallRequest,
    sessions: SessionStore = Depends(get_sessions),
    audit: AuditTrail = Depends(get_audit),
) -> StartCallResponse:
    session = sessions.get_or_create(payload.call_id)
    audit.emit(session.call_id, EventType.CALL_STARTED, channel=payload.channel)
    return StartCallResponse(
        call_id=session.call_id,
        stage=session.stage_label,
        agent_guidance=(
            "Greet the carrier, say which brokerage they have reached, and ask for "
            "their MC number so you can pull them up."
        ),
    )


@router.post("/close", response_model=CallTrailResponse)
async def close_call(
    payload: CloseCallRequest,
    sessions: SessionStore = Depends(get_sessions),
    audit: AuditTrail = Depends(get_audit),
    memory: MemorySink = Depends(get_memory_sink),
) -> CallTrailResponse:
    session = load_session(payload.call_id, sessions)
    if session.outcome == Outcome.IN_PROGRESS:
        session.outcome = Outcome.ABANDONED
    session.terminate(payload.reason or "call ended")
    audit.emit(session.call_id, EventType.CALL_CLOSED, mc_number=session.mc_number,
               outcome=session.outcome, reason=payload.reason,
               negotiations=[n.as_audit() for n in session.negotiations.values()])
    return _trail(session, memory)


@router.get("/{call_id}", response_model=CallTrailResponse)
async def call_trail(
    call_id: str,
    sessions: SessionStore = Depends(get_sessions),
    memory: MemorySink = Depends(get_memory_sink),
) -> CallTrailResponse:
    session = load_session(call_id, sessions)
    return _trail(session, memory)


# The trail is the one place a full call history is served over HTTP, and the
# workflow is the most likely caller. Ceiling-bearing keys are stripped here so
# that even an operator-facing endpoint cannot become the leak. Complete records
# including the ceiling stay in the structured audit log and in Twin.
_CEILING_KEYS = ("max_rate", "margin_protected")


def _redact(value):
    """Strip ceiling-bearing keys at any depth, including nested round records."""
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items() if k not in _CEILING_KEYS}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _trail(session, memory: MemorySink) -> CallTrailResponse:
    return CallTrailResponse(
        call_id=session.call_id,
        stage=session.stage_label,
        outcome=session.outcome,
        mc_number=session.mc_number,
        carrier_name=session.carrier_name,
        agreed_rate=session.agreed_rate,
        booking_reference=session.booking_ref,
        handoff_reference=session.handoff_ref,
        events=[_redact(e.as_dict()) for e in memory.for_call(session.call_id)],
    )
