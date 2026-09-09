"""The audit trail the brokerage does not have today.

Every call emits an ordered event stream: who called, whether authority passed,
whether identity was proven, what was pitched, every offer and counter with a
timestamp, what was agreed, and how the call ended.

Two sinks, on purpose:

  log sink      structured JSON on stdout, so the record survives the process
                and is picked up by whatever the deployment ships logs to.
  memory sink   a bounded ring buffer that backs the ops dashboard and the
                per-call trail endpoint.

Neither is the system of record. Twin is, per the brief, and `TwinSink` is the
one class that has to be added to land these same events there — the emit call
sites do not change.
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

audit_logger = logging.getLogger("audit")


class EventType:
    CALL_STARTED = "call_started"
    AUTHORITY_CHECKED = "authority_checked"
    OTP_SENT = "otp_sent"
    OTP_VERIFIED = "otp_verified"
    OTP_FAILED = "otp_failed"
    OTP_BYPASS_ATTEMPT = "otp_bypass_attempt"
    LOADS_SEARCHED = "loads_searched"
    LOAD_PITCHED = "load_pitched"
    NEGOTIATION_ROUND = "negotiation_round"
    CEILING_BREACH_ATTEMPT = "ceiling_breach_attempt"
    RATE_AGREED = "rate_agreed"
    NEGOTIATION_FAILED = "negotiation_failed"
    BOOKING_CONFIRMED = "booking_confirmed"
    # The TMS gave a definite no (load gone, rate refused). Normal business outcome.
    BOOKING_REJECTED = "booking_rejected"
    # The write may or may not have landed. Needs a human — never a blind retry.
    BOOKING_UNCERTAIN = "booking_uncertain"
    HANDOFF_QUEUED = "handoff_queued"
    HANDOFF_WITHHELD = "handoff_withheld"
    TMS_DEGRADED = "tms_degraded"
    CALL_CLOSED = "call_closed"


@dataclass(frozen=True)
class AuditEvent:
    call_id: str
    event: str
    at: datetime
    mc_number: str | None = None
    load_id: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "call_id": self.call_id,
            "event": self.event,
            "at": self.at.isoformat(),
            "mc_number": self.mc_number,
            "load_id": self.load_id,
            **self.detail,
        }


class AuditSink(Protocol):
    def write(self, event: AuditEvent) -> None: ...


class LogSink:
    def write(self, event: AuditEvent) -> None:
        audit_logger.info(json.dumps(event.as_dict(), default=str))


class MemorySink:
    def __init__(self, capacity: int = 5000) -> None:
        self._events: deque[AuditEvent] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def write(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def for_call(self, call_id: str) -> list[AuditEvent]:
        with self._lock:
            return [e for e in self._events if e.call_id == call_id]

    def all(self) -> list[AuditEvent]:
        with self._lock:
            return list(self._events)

    def count(self, event_type: str) -> int:
        with self._lock:
            return sum(1 for e in self._events if e.event == event_type)


class AuditTrail:
    def __init__(self, sinks: list[AuditSink]) -> None:
        self._sinks = sinks

    def emit(
        self,
        call_id: str,
        event: str,
        *,
        mc_number: str | None = None,
        load_id: str | None = None,
        **detail: Any,
    ) -> AuditEvent:
        record = AuditEvent(
            call_id=call_id,
            event=event,
            at=datetime.now(timezone.utc),
            mc_number=mc_number,
            load_id=load_id,
            detail=detail,
        )
        for sink in self._sinks:
            try:
                sink.write(record)
            except Exception:  # a broken sink must never end a live call
                audit_logger.exception("audit sink failed for %s", event)
        return record
