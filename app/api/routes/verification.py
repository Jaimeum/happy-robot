"""Authority check, then identity check. In that order, always."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_audit, get_fmcsa, get_sessions, load_session, require_stage
from app.api.schemas import (
    CarrierVerificationResponse,
    OtpSentResponse,
    OtpVerifyResponse,
    SendOtpRequest,
    VerifyCarrierRequest,
    VerifyOtpRequest,
)
from app.config import Settings, get_settings
from app.domain import otp as otp_service
from app.domain.audit import AuditTrail, EventType
from app.domain.sessions import Outcome, SessionStore, Stage
from app.integrations.fmcsa import FmcsaClient, FmcsaUnavailable
from app.security.auth import require_token

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["verification"], dependencies=[Depends(require_token)])


@router.post("/carriers/verify", response_model=CarrierVerificationResponse)
async def verify_carrier(
    payload: VerifyCarrierRequest,
    sessions: SessionStore = Depends(get_sessions),
    fmcsa: FmcsaClient = Depends(get_fmcsa),
    audit: AuditTrail = Depends(get_audit),
) -> CarrierVerificationResponse:
    session = load_session(payload.call_id, sessions)

    try:
        authority = await fmcsa.verify(payload.mc_number)
    except FmcsaUnavailable as exc:
        logger.warning("fmcsa.unavailable call_id=%s error=%s", session.call_id, exc)
        audit.emit(session.call_id, EventType.AUTHORITY_CHECKED,
                   mc_number=payload.mc_number, available=False, error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "verification_unavailable",
                "message": "The authority database is not responding.",
                "agent_guidance": (
                    "Tell the carrier the licensing database is down at the moment, "
                    "take their number, and let them know a rep will call back. "
                    "Do not continue to load matching."
                ),
            },
        ) from exc

    session.mc_number = authority.mc_number
    session.carrier_name = authority.display_name
    session.dot_number = authority.dot_number

    audit.emit(session.call_id, EventType.AUTHORITY_CHECKED, **authority.as_audit())

    if not authority.authorised:
        session.outcome = Outcome.REJECTED_AUTHORITY
        session.terminate("no active operating authority")
        reason = authority.reasons[0] if authority.reasons else "authority check failed"
        return CarrierVerificationResponse(
            call_id=session.call_id,
            stage=session.stage_label,
            mc_number=authority.mc_number,
            verified=False,
            carrier_name=authority.display_name,
            dot_number=authority.dot_number,
            failure_reasons=authority.reasons,
            next_step="end_call",
            agent_guidance=(
                "This carrier does not have active operating authority on file, so we "
                "cannot offer them freight. Tell them politely that we are not able to "
                f"move forward right now because {reason.lower()}, suggest they contact "
                "FMCSA to sort it out, and close the call."
            ),
        )

    session.advance_to(Stage.AUTHORITY_VERIFIED)
    domicile = ", ".join(p for p in [authority.city, authority.state] if p) or None
    return CarrierVerificationResponse(
        call_id=session.call_id,
        stage=session.stage_label,
        mc_number=authority.mc_number,
        verified=True,
        carrier_name=authority.display_name,
        dot_number=authority.dot_number,
        domicile=domicile,
        power_units=authority.power_units,
        next_step="send_otp",
        agent_guidance=(
            f"Authority checks out for {authority.display_name}. Next, confirm their "
            "identity: ask whether they want the verification code by text or email, "
            "and read the destination back to them before sending."
        ),
    )


@router.post("/identity/otp/send", response_model=OtpSentResponse)
async def send_otp(
    payload: SendOtpRequest,
    sessions: SessionStore = Depends(get_sessions),
    audit: AuditTrail = Depends(get_audit),
    settings: Settings = Depends(get_settings),
) -> OtpSentResponse:
    session = load_session(payload.call_id, sessions)
    require_stage(session, Stage.AUTHORITY_VERIFIED, "Identity verification")

    _record_bypass_language(session, audit, payload.carrier_said, "otp_send")

    channel = otp_service.normalise_channel(payload.channel, payload.destination)
    try:
        challenge, issued = otp_service.issue(
            channel=channel,
            destination=payload.destination,
            ttl_seconds=settings.otp_ttl_seconds,
            length=settings.otp_length,
        )
    except otp_service.OtpError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"error": "invalid_destination", "message": str(exc)},
        ) from exc

    session.otp = challenge
    session.advance_to(Stage.OTP_SENT)

    # Delivery is pluggable. In this build the code is emitted to the audit
    # stream, where the ops dashboard surfaces it; production swaps in the
    # HappyRobot Email or Twilio SMS channel without touching this call site.
    audit.emit(session.call_id, EventType.OTP_SENT, mc_number=session.mc_number,
               channel=channel, destination_masked=issued.destination_masked,
               delivery="audit_stream", code=challenge.code)
    logger.info("otp.issued call_id=%s channel=%s to=%s",
                session.call_id, channel, issued.destination_masked)

    return OtpSentResponse(
        call_id=session.call_id,
        stage=session.stage_label,
        sent=True,
        channel=channel,
        destination_masked=issued.destination_masked,
        expires_in_seconds=issued.expires_in_seconds,
        attempts_allowed=settings.otp_max_attempts,
        agent_guidance=(
            f"A {settings.otp_length}-digit code is on its way to "
            f"{issued.destination_masked}. Ask them to read it back. It expires in "
            f"{issued.expires_in_seconds // 60} minutes."
        ),
    )


@router.post("/identity/otp/verify", response_model=OtpVerifyResponse)
async def verify_otp(
    payload: VerifyOtpRequest,
    sessions: SessionStore = Depends(get_sessions),
    audit: AuditTrail = Depends(get_audit),
    settings: Settings = Depends(get_settings),
) -> OtpVerifyResponse:
    session = load_session(payload.call_id, sessions)
    require_stage(session, Stage.OTP_SENT, "Code verification")

    _record_bypass_language(session, audit, payload.carrier_said, "otp_verify")

    result = otp_service.verify(session.otp, payload.code, settings.otp_max_attempts)
    attempts_used = session.otp.attempts_used if session.otp else 0
    remaining = max(0, settings.otp_max_attempts - attempts_used)

    if result == otp_service.VerifyResult.OK:
        session.advance_to(Stage.IDENTITY_VERIFIED)
        audit.emit(session.call_id, EventType.OTP_VERIFIED, mc_number=session.mc_number,
                   attempts_used=attempts_used)
        return OtpVerifyResponse(
            call_id=session.call_id, stage=session.stage_label, verified=True,
            result=result, attempts_remaining=remaining, may_retry=False,
            agent_guidance=(
                "Identity confirmed. Now ask what lane they are running and what "
                "equipment they have, then search for a load."
            ),
        )

    audit.emit(session.call_id, EventType.OTP_FAILED, mc_number=session.mc_number,
               result=result, attempts_used=attempts_used)

    may_retry = result == otp_service.VerifyResult.WRONG and remaining > 0
    if result == otp_service.VerifyResult.EXHAUSTED:
        # Burned. A fresh code is not offered on this call — that would turn the
        # attempt cap into a formality.
        session.outcome = Outcome.FAILED_IDENTITY
        session.terminate("identity verification attempts exhausted")

    guidance = {
        otp_service.VerifyResult.WRONG: (
            f"That code is not right. They have {remaining} attempt(s) left. "
            "Ask them to read it again, digit by digit."
        ),
        otp_service.VerifyResult.EXPIRED: (
            "That code has expired. Offer to send a fresh one. Do not move on to "
            "loads until a new code is verified."
        ),
        otp_service.VerifyResult.EXHAUSTED: (
            "They have used all their attempts. Tell them we cannot verify their "
            "identity on this call and a rep will follow up. Close the call — "
            "do not search loads."
        ),
        otp_service.VerifyResult.NOT_ISSUED: (
            "No code has been sent on this call yet. Send one first."
        ),
    }[result]

    return OtpVerifyResponse(
        call_id=session.call_id, stage=session.stage_label, verified=False,
        result=result, attempts_remaining=remaining, may_retry=may_retry,
        agent_guidance=guidance,
    )


def _record_bypass_language(session, audit: AuditTrail, said: str | None, where: str) -> None:
    """Log attempts to talk past the step.

    Detection is for visibility only. Whether the phrase is recognised or not
    makes no difference to what happens next — the stage machine is what
    decides, and it does not read free text.
    """
    markers = otp_service.detect_bypass_language(said)
    if not markers:
        return
    session.otp_bypass_attempts += 1
    audit.emit(session.call_id, EventType.OTP_BYPASS_ATTEMPT, mc_number=session.mc_number,
               markers=markers, where=where, blocked=True)
    logger.warning("otp.bypass_attempt call_id=%s markers=%s", session.call_id, markers)
