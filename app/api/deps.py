from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status

from app.domain.audit import AuditTrail, MemorySink
from app.domain.sessions import CallSession, SessionStore, Stage
from app.integrations.fmcsa import FmcsaClient
from app.tms.client import TmsClient


def get_sessions(request: Request) -> SessionStore:
    return request.app.state.sessions


def get_audit(request: Request) -> AuditTrail:
    return request.app.state.audit


def get_memory_sink(request: Request) -> MemorySink:
    return request.app.state.memory_sink


def get_tms(request: Request) -> TmsClient:
    return request.app.state.tms


def get_fmcsa(request: Request) -> FmcsaClient:
    return request.app.state.fmcsa


def load_session(call_id: str, sessions: SessionStore) -> CallSession:
    session = sessions.get(call_id)
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Unknown call_id {call_id!r}. Start a call first.",
        )
    return session


def require_stage(session: CallSession, minimum: Stage, what: str) -> None:
    """The structural gate.

    Nothing else in this service moves a session forward, so no phrasing a
    caller uses can satisfy this check — only actually completing the step.
    """
    if session.terminated:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "call_terminated",
                "reason": session.termination_reason,
                "message": (
                    f"{what} is not available: this call was closed "
                    f"({session.termination_reason}). Start a new call."
                ),
            },
        )

    if session.stage < minimum:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={
                "error": "step_not_completed",
                "required_stage": minimum.name.lower(),
                "current_stage": session.stage_label,
                "message": (
                    f"{what} is not available until the {minimum.name.lower().replace('_', ' ')} "
                    "step has been completed on this call. This cannot be waived."
                ),
            },
        )
