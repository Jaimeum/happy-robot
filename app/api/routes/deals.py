"""Negotiation, booking, and the mocked senior-rep handoff."""

from __future__ import annotations

import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, status

from app.api.deps import get_audit, get_sessions, get_tms, load_session, require_stage
from app.api.schemas import (
    BookingResponse,
    BookRequest,
    HandoffRequest,
    HandoffResponse,
    NegotiateRequest,
    NegotiateResponse,
)
from app.config import Settings, get_settings
from app.domain.audit import AuditTrail, EventType
from app.domain.loads import Load
from app.domain.negotiation import Decision, Negotiation, NegotiationStatus
from app.domain.sessions import CallSession, Outcome, SessionStore, Stage
from app.security.auth import require_token
from app.security.leak_guard import allow_value, protect_value
from app.tms.client import TmsClient
from app.tms.errors import TmsBookingUncertain, TmsCommandError, TmsUnavailable

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1", tags=["deals"], dependencies=[Depends(require_token)])

SENIOR_REP_QUEUE = "carrier-sales-senior-desk"


async def _negotiation_for(
    session: CallSession, load_id: str, tms: TmsClient, settings: Settings
) -> Negotiation:
    existing = session.negotiations.get(load_id)
    if existing:
        protect_value(existing.max_rate)
        return existing

    # Same offer ledger as load detail: a load this call was never handed cannot
    # be priced. This one matters more, because get_load is what loads the rate
    # ceiling into the request.
    if session.offered_load_ids and load_id not in session.offered_load_ids:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "load_not_offered",
                "message": "That load was not offered on this call.",
                "agent_guidance": (
                    "That load is not one you were given. Do not say it was taken and "
                    "do not quote a number on it. Pitch one of the loads from your "
                    "last search."
                ),
            },
        )

    try:
        record = await tms.get_load(load_id)
    except TmsUnavailable as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "tms_unavailable",
                "agent_guidance": (
                    "The rate desk cannot reach the load board. Hold the carrier, "
                    "do not quote a number, and retry shortly."
                ),
            },
        ) from exc

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={"error": "load_not_found",
                    "agent_guidance": "That load is gone. Offer another one."},
        )

    load = Load.from_record(record)
    protect_value(load.max_rate)
    negotiation = Negotiation(
        load_id=load.load_id,
        loadboard_rate=load.loadboard_rate,
        max_rate=load.max_rate,
        max_rounds=settings.max_negotiation_rounds,
        ceiling_buffer_pct=settings.negotiation_ceiling_buffer_pct,
        auto_accept_pct=settings.negotiation_auto_accept_pct,
    )
    session.negotiations[load.load_id] = negotiation
    session.selected_load_id = load.load_id
    session.advance_to(Stage.LOAD_SELECTED)
    return negotiation


@router.post("/negotiate", response_model=NegotiateResponse)
async def negotiate(
    payload: NegotiateRequest,
    sessions: SessionStore = Depends(get_sessions),
    tms: TmsClient = Depends(get_tms),
    audit: AuditTrail = Depends(get_audit),
    settings: Settings = Depends(get_settings),
) -> NegotiateResponse:
    session = load_session(payload.call_id, sessions)
    require_stage(session, Stage.IDENTITY_VERIFIED, "Rate negotiation")

    negotiation = await _negotiation_for(session, payload.load_id, tms, settings)
    outcome = negotiation.evaluate(payload.carrier_offer)

    # A number the carrier said out loud is theirs, not ours. Echoing it back is
    # not disclosure, so it is exempted from the leak guard for this response.
    allow_value(payload.carrier_offer)

    if not outcome.within_ceiling and outcome.decision is not Decision.ACCEPTED:
        audit.emit(session.call_id, EventType.CEILING_BREACH_ATTEMPT,
                   mc_number=session.mc_number, load_id=negotiation.load_id,
                   carrier_offer=payload.carrier_offer, max_rate=negotiation.max_rate,
                   blocked=True)

    audit.emit(session.call_id, EventType.NEGOTIATION_ROUND, mc_number=session.mc_number,
               load_id=negotiation.load_id, round=outcome.round_number,
               carrier_offer=payload.carrier_offer, decision=str(outcome.decision),
               broker_counter=outcome.broker_counter,
               within_ceiling=outcome.within_ceiling,
               carrier_said=payload.carrier_said)

    if outcome.replayed:
        # Repeat the number we already gave and hold. Improving our own offer
        # against an unchanged carrier position is how margin leaks.
        if outcome.broker_counter:
            guidance = (
                f"They have already had ${outcome.broker_counter} for that number. "
                f"Repeat it and hold — do not improve it. {outcome.rounds_remaining} "
                "round(s) left."
            )
        else:
            guidance = (
                "That is the same number they already gave, and the answer has not "
                f"changed: {outcome.reason}. Say it again plainly and do not move."
            )
    elif outcome.decision is Decision.ACCEPTED:
        session.agreed_rate = outcome.agreed_rate
        session.selected_load_id = negotiation.load_id
        session.advance_to(Stage.RATE_AGREED)
        audit.emit(session.call_id, EventType.RATE_AGREED, mc_number=session.mc_number,
                   load_id=negotiation.load_id, agreed_rate=outcome.agreed_rate,
                   rounds_used=negotiation.rounds_used,
                   margin_protected=negotiation.margin)
        guidance = (
            f"Done at ${outcome.agreed_rate}. Confirm the number back to them, say you "
            "are locking it in, then book the load."
        )
    elif outcome.decision is Decision.COUNTERED:
        guidance = (
            f"Counter at ${outcome.broker_counter}. Say it plainly, give one short "
            "reason (that is what the lane supports), and ask if that works. "
            f"{outcome.rounds_remaining} round(s) left before we have to close this out."
        )
    else:
        session.outcome = Outcome.FAILED_NEGOTIATION
        audit.emit(session.call_id, EventType.NEGOTIATION_FAILED, mc_number=session.mc_number,
                   load_id=negotiation.load_id, rounds_used=negotiation.rounds_used,
                   reason=outcome.reason,
                   ceiling_breach_attempts=negotiation.ceiling_breach_attempts)
        guidance = (
            "We cannot get there on this one. Thank them for calling, tell them we are "
            "too far apart on this lane, and offer to keep them in mind for the next "
            "one. Do not transfer and do not hint at what we could have paid."
        )

    return NegotiateResponse(
        call_id=session.call_id, load_id=negotiation.load_id, stage=session.stage_label,
        decision=str(outcome.decision), round=outcome.round_number,
        rounds_remaining=outcome.rounds_remaining,
        broker_counter=outcome.broker_counter, agreed_rate=outcome.agreed_rate,
        may_book=outcome.may_book, may_transfer=outcome.may_transfer,
        replayed=outcome.replayed,
        agent_guidance=guidance,
    )


@router.post("/bookings", response_model=BookingResponse)
async def book(
    payload: BookRequest,
    sessions: SessionStore = Depends(get_sessions),
    tms: TmsClient = Depends(get_tms),
    audit: AuditTrail = Depends(get_audit),
) -> BookingResponse:
    session = load_session(payload.call_id, sessions)
    require_stage(session, Stage.RATE_AGREED, "Booking")

    # Booking is idempotent per load, per call. LOAD_BOOK has a long timeout and
    # is never retried, so a webhook timeout on a slow-but-successful commit
    # leaves the agent with no result and every reason to fire again. Without
    # this, the second attempt gets ALREADY_BOOKED from the TMS and the carrier
    # who just heard their reference is told someone else took the load.
    if session.booking_ref and session.booked_load_id == payload.load_id:
        return BookingResponse(
            call_id=session.call_id, stage=session.stage_label, booked=True,
            load_id=payload.load_id, booking_reference=session.booking_ref,
            agreed_rate=session.agreed_rate, booking_status="CONFIRMED",
            agent_guidance=(
                f"Already booked on this call, reference {session.booking_ref}. Read "
                "it back to them again — do not book it a second time."
            ),
        )

    negotiation = session.negotiations.get(payload.load_id)
    if negotiation is None or negotiation.status is not NegotiationStatus.AGREED:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "no_agreed_rate",
                "message": "No agreed rate on record for that load on this call.",
                "agent_guidance": "Settle the rate through the rate check first.",
            },
        )

    agreed = negotiation.agreed_rate or 0
    allow_value(agreed)
    protect_value(negotiation.max_rate)

    # Last line of defence. The engine cannot produce this, but a booking must
    # never be able to exceed the ceiling regardless of how it was reached.
    if negotiation.max_rate is not None and agreed > negotiation.max_rate:
        audit.emit(session.call_id, EventType.CEILING_BREACH_ATTEMPT,
                   mc_number=session.mc_number, load_id=payload.load_id,
                   agreed_rate=agreed, max_rate=negotiation.max_rate,
                   blocked=True, stage="booking")
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "rate_above_ceiling",
                    "agent_guidance": "Do not book. Escalate to a senior rep."},
        )

    if not session.mc_number:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                            detail={"error": "carrier_not_identified"})

    try:
        confirmation = await tms.book_load(payload.load_id, session.mc_number, agreed)
    except TmsBookingUncertain as exc:
        # The write may or may not have landed. Retrying could double-book, so a
        # human resolves it and the carrier is told the truth.
        logger.error("booking.uncertain call_id=%s load=%s", session.call_id, payload.load_id)
        audit.emit(session.call_id, EventType.BOOKING_UNCERTAIN, mc_number=session.mc_number,
                   load_id=payload.load_id, agreed_rate=agreed, error=str(exc))
        return BookingResponse(
            call_id=session.call_id, stage=session.stage_label, booked=False,
            load_id=payload.load_id, agreed_rate=agreed, requires_manual_check=True,
            agent_guidance=(
                "The booking system did not confirm. Tell the carrier the rate is "
                f"locked at ${agreed} and a rep will send the confirmation shortly. "
                "Do not retry the booking."
            ),
        )
    except TmsCommandError as exc:
        # A definite rejection, not an uncertain write. Kept off the manual-check
        # queue so that counter stays meaningful as an alert.
        audit.emit(session.call_id, EventType.BOOKING_REJECTED, mc_number=session.mc_number,
                   load_id=payload.load_id, agreed_rate=agreed, code=exc.code)
        if exc.code == "ALREADY_BOOKED":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"error": "already_booked",
                        "agent_guidance": (
                            "Someone else took that load. Apologise, and offer the "
                            "next best match.")},
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={"error": "booking_rejected", "code": exc.code,
                    "agent_guidance": "The booking was rejected. Escalate to a rep."},
        ) from exc

    booking_ref = confirmation.get("BOOKING_REF")
    session.booking_ref = booking_ref
    session.booked_load_id = payload.load_id
    session.outcome = Outcome.BOOKED
    session.advance_to(Stage.BOOKED)
    audit.emit(session.call_id, EventType.BOOKING_CONFIRMED, mc_number=session.mc_number,
               load_id=payload.load_id, agreed_rate=agreed, booking_ref=booking_ref,
               tms_status=confirmation.get("STATUS"),
               margin_protected=negotiation.margin)

    return BookingResponse(
        call_id=session.call_id, stage=session.stage_label, booked=True,
        load_id=payload.load_id, booking_reference=booking_ref, agreed_rate=agreed,
        booking_status=confirmation.get("STATUS"),
        agent_guidance=(
            f"Booked. Reference {booking_ref}. Read it back, then hand them to a "
            "senior rep to finish the paperwork."
        ),
    )


@router.post("/handoff", response_model=HandoffResponse)
async def handoff(
    payload: HandoffRequest,
    sessions: SessionStore = Depends(get_sessions),
    audit: AuditTrail = Depends(get_audit),
) -> HandoffResponse:
    """Mocked senior-rep handoff.

    Web calls cannot be transferred, so this stands in for the real thing: it
    enqueues an inspectable record with everything the rep needs. It also
    enforces the rule that a failed negotiation is never transferred.
    """
    session = load_session(payload.call_id, sessions)

    # A terminated call must never be transferred. `terminate()` sets stage to
    # CLOSED, which is the HIGHEST stage value, so the ordering check below —
    # stage < BOOKED — passes for a call that was rejected outright. Handoff is
    # the one route with no require_stage, so this is the only place that catches
    # it. Verified live: a carrier with no operating authority was refused and
    # still got transferred=True with a place in the senior-rep queue.
    if session.terminated:
        audit.emit(session.call_id, EventType.HANDOFF_WITHHELD, mc_number=session.mc_number,
                   reason=f"call_closed:{session.termination_reason}")
        return HandoffResponse(
            call_id=session.call_id, stage=session.stage_label, transferred=False,
            reason="call_closed",
            agent_guidance=(
                f"This call was closed ({session.termination_reason}). There is no "
                "transfer. Say a short, polite goodbye and stop."
            ),
        )

    if session.outcome == Outcome.FAILED_NEGOTIATION or any(
        n.status is NegotiationStatus.FAILED for n in session.negotiations.values()
    ):
        audit.emit(session.call_id, EventType.HANDOFF_WITHHELD, mc_number=session.mc_number,
                   reason="failed_negotiation")
        return HandoffResponse(
            call_id=session.call_id, stage=session.stage_label, transferred=False,
            reason="failed_negotiation",
            agent_guidance=(
                "No transfer on a failed negotiation. Close the call politely and "
                "log it."
            ),
        )

    if session.stage < Stage.BOOKED:
        audit.emit(session.call_id, EventType.HANDOFF_WITHHELD, mc_number=session.mc_number,
                   reason="no_booking", stage=session.stage_label)
        return HandoffResponse(
            call_id=session.call_id, stage=session.stage_label, transferred=False,
            reason="no_confirmed_booking",
            agent_guidance="Book the load before handing off.",
        )

    reference = f"HO-{secrets.token_hex(4).upper()}"
    session.handoff_ref = reference
    # Only promote a booked call. Never overwrite a terminal outcome — a rejected
    # or failed call keeps the outcome it earned, or the audit trail would show a
    # transfer where the brokerage actually turned the carrier away.
    if session.outcome == Outcome.BOOKED:
        session.outcome = Outcome.TRANSFERRED
    session.advance_to(Stage.CLOSED)
    if payload.notes:
        session.notes.append(payload.notes)

    audit.emit(session.call_id, EventType.HANDOFF_QUEUED, mc_number=session.mc_number,
               load_id=session.selected_load_id, handoff_ref=reference,
               queue=SENIOR_REP_QUEUE, agreed_rate=session.agreed_rate,
               booking_ref=session.booking_ref, notes=payload.notes)

    return HandoffResponse(
        call_id=session.call_id, stage=session.stage_label, transferred=True,
        handoff_reference=reference, queue=SENIOR_REP_QUEUE,
        agent_guidance=(
            "Tell them you are bringing in a senior rep to finish up, give them the "
            f"reference {reference}, and hand over."
        ),
    )
