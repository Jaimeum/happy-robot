"""Call session state and the gate that sequences it.

The brief requires the OTP step to survive social engineering "under any
framing". Prompt instructions cannot promise that — a caller only has to find
the phrasing the model wasn't told about. So the guarantee is structural:

  * Progress through the call is a one-way stage machine held server-side.
  * Every downstream endpoint declares the stage it requires.
  * There is no argument, header, or flag anywhere in this service that moves a
    session forward other than genuinely passing the step.

A caller can say anything they like. The load-search endpoint still returns 403
until an OTP that this service generated has been echoed back to it.
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import IntEnum

from app.domain.negotiation import Negotiation


class Stage(IntEnum):
    """Ordered. A session's stage only ever increases."""

    STARTED = 0
    AUTHORITY_VERIFIED = 10
    OTP_SENT = 20
    IDENTITY_VERIFIED = 30
    LOAD_SELECTED = 40
    RATE_AGREED = 50
    BOOKED = 60
    CLOSED = 70


STAGE_LABELS = {
    Stage.STARTED: "started",
    Stage.AUTHORITY_VERIFIED: "authority_verified",
    Stage.OTP_SENT: "otp_sent",
    Stage.IDENTITY_VERIFIED: "identity_verified",
    Stage.LOAD_SELECTED: "load_selected",
    Stage.RATE_AGREED: "rate_agreed",
    Stage.BOOKED: "booked",
    Stage.CLOSED: "closed",
}


class Outcome:
    IN_PROGRESS = "in_progress"
    REJECTED_AUTHORITY = "rejected_authority"
    FAILED_IDENTITY = "failed_identity"
    NO_LOADS = "no_loads_matched"
    FAILED_NEGOTIATION = "failed_negotiation"
    BOOKED = "booked"
    TRANSFERRED = "transferred_to_senior_rep"
    ABANDONED = "abandoned"


@dataclass
class OtpChallenge:
    code: str
    channel: str
    destination_masked: str
    issued_at: float
    expires_at: float
    attempts_used: int = 0
    verified: bool = False

    def is_expired(self, now: float | None = None) -> bool:
        return (now or time.time()) > self.expires_at


@dataclass
class CallSession:
    call_id: str
    created_at: datetime
    stage: Stage = Stage.STARTED
    outcome: str = Outcome.IN_PROGRESS

    mc_number: str | None = None
    carrier_name: str | None = None
    dot_number: str | None = None

    otp: OtpChallenge | None = None
    otp_bypass_attempts: int = 0

    selected_load_id: str | None = None
    negotiations: dict[str, Negotiation] = field(default_factory=dict)

    booking_ref: str | None = None
    agreed_rate: int | None = None
    handoff_ref: str | None = None
    notes: list[str] = field(default_factory=list)

    # A terminal call is refused by every gate regardless of how far it got.
    # Tracked separately from `stage` because CLOSED is the highest stage value,
    # so a rejected call marked CLOSED would otherwise satisfy every ordering
    # check rather than failing them all.
    terminated: bool = False
    termination_reason: str | None = None

    last_touched: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_touched = time.time()

    def advance_to(self, stage: Stage) -> None:
        if stage > self.stage:
            self.stage = stage
        self.touch()

    def terminate(self, reason: str) -> None:
        self.terminated = True
        self.termination_reason = reason
        self.stage = Stage.CLOSED
        self.touch()

    @property
    def identity_verified(self) -> bool:
        return self.stage >= Stage.IDENTITY_VERIFIED

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS[self.stage]

    def active_negotiation(self) -> Negotiation | None:
        if not self.selected_load_id:
            return None
        return self.negotiations.get(self.selected_load_id)


class SessionStore:
    """In-process, TTL-bounded session store.

    Deliberately not a database. These are ephemeral per-call working values
    (a live OTP, a round counter) with a lifetime measured in minutes, not the
    call record itself — the durable audit trail is a separate concern and is
    what belongs in Twin. A single-process store also means an OTP cannot be
    replayed against a second instance.
    """

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._ttl = ttl_seconds
        self._sessions: dict[str, CallSession] = {}
        self._lock = threading.RLock()

    def create(self, call_id: str | None = None) -> CallSession:
        with self._lock:
            self._evict_expired()
            resolved = call_id or f"call_{secrets.token_urlsafe(12)}"
            session = CallSession(
                call_id=resolved, created_at=datetime.now(timezone.utc)
            )
            self._sessions[resolved] = session
            return session

    def get(self, call_id: str) -> CallSession | None:
        with self._lock:
            self._evict_expired()
            session = self._sessions.get(call_id)
            if session:
                session.touch()
            return session

    def get_or_create(self, call_id: str | None) -> CallSession:
        if call_id:
            existing = self.get(call_id)
            if existing:
                return existing
        return self.create(call_id)

    def all(self) -> list[CallSession]:
        with self._lock:
            self._evict_expired()
            return list(self._sessions.values())

    def _evict_expired(self) -> None:
        cutoff = time.time() - self._ttl
        for key in [k for k, v in self._sessions.items() if v.last_touched < cutoff]:
            del self._sessions[key]
