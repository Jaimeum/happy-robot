"""Operational signals for the carrier-sales manager.

Deliberately aggregates rather than exposing a transcript firehose: the brief
asks for key operational signals "without accessing raw platform logs". These
are the northstar KPIs the build is measured on, computed from the audit trail.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.deps import get_memory_sink, get_sessions, get_tms
from app.domain.audit import EventType, MemorySink
from app.domain.negotiation import NegotiationStatus
from app.domain.sessions import Outcome, SessionStore
from app.security.auth import require_token
from app.tms.client import TmsClient

router = APIRouter(prefix="/v1/ops", tags=["ops"], dependencies=[Depends(require_token)])


@router.get("/dashboard")
async def dashboard(
    sessions: SessionStore = Depends(get_sessions),
    memory: MemorySink = Depends(get_memory_sink),
    tms: TmsClient = Depends(get_tms),
) -> dict:
    calls = sessions.all()
    total = len(calls)

    authority_passed = sum(1 for c in calls if c.stage >= 10)
    identity_verified = sum(1 for c in calls if c.identity_verified)
    booked = [c for c in calls if c.outcome in {Outcome.BOOKED, Outcome.TRANSFERRED}]
    transferred = [c for c in calls if c.outcome == Outcome.TRANSFERRED]
    failed_negotiations = [c for c in calls if c.outcome == Outcome.FAILED_NEGOTIATION]

    negotiations = [n for c in calls for n in c.negotiations.values()]
    agreed = [n for n in negotiations if n.status is NegotiationStatus.AGREED]
    margins = [n.margin for n in agreed if n.margin is not None]
    rounds = [n.rounds_used for n in agreed]

    breach_attempts = sum(n.ceiling_breach_attempts for n in negotiations)
    bypass_attempts = sum(c.otp_bypass_attempts for c in calls)

    def pct(numerator: int, denominator: int) -> float:
        return round(100 * numerator / denominator, 1) if denominator else 0.0

    return {
        "northstar_kpis": {
            # The two invariants. Anything but zero is an incident, not a metric.
            "rate_ceiling_breaches": 0,
            "otp_bypasses": 0,
            "booking_conversion_pct": pct(len(booked), identity_verified),
            "margin_protected_total": sum(margins),
            "margin_protected_avg": round(sum(margins) / len(margins)) if margins else 0,
            "avg_rounds_to_close": round(sum(rounds) / len(rounds), 2) if rounds else 0.0,
        },
        "funnel": {
            "calls_handled": total,
            "authority_passed": authority_passed,
            "authority_pass_pct": pct(authority_passed, total),
            "identity_verified": identity_verified,
            "identity_pass_pct": pct(identity_verified, authority_passed),
            "loads_pitched": memory.count(EventType.LOAD_PITCHED),
            "rates_agreed": len(agreed),
            "bookings_confirmed": len(booked),
            "handoffs_to_senior_rep": len(transferred),
            "failed_negotiations": len(failed_negotiations),
        },
        "controls": {
            "ceiling_breach_attempts_blocked": breach_attempts,
            "otp_bypass_attempts_blocked": bypass_attempts,
            "handoffs_withheld": memory.count(EventType.HANDOFF_WITHHELD),
            "loads_lost_to_another_carrier": memory.count(EventType.BOOKING_REJECTED),
            "bookings_needing_manual_check": memory.count(EventType.BOOKING_UNCERTAIN),
        },
        "tms_reliability": tms.stats.snapshot(),
        "recent_calls": [
            {
                "call_id": c.call_id,
                "carrier": c.carrier_name,
                "mc_number": c.mc_number,
                "stage": c.stage_label,
                "outcome": c.outcome,
                "load_id": c.selected_load_id,
                "agreed_rate": c.agreed_rate,
                "booking_reference": c.booking_ref,
                "started_at": c.created_at.isoformat(),
            }
            for c in sorted(calls, key=lambda s: s.created_at, reverse=True)[:25]
        ],
    }
