from __future__ import annotations


class TmsError(Exception):
    """Base class for every failure surfaced by the Legacy TMS adapter."""

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class TmsUnavailable(TmsError):
    """The adapter could not obtain a well-formed response within its retry budget.

    Raised for the injected-fault family (timeout, partial response, malformed
    response) and for transport-level failures. Callers should degrade
    gracefully rather than surface this to a carrier on a live call.
    """


class TmsProtocolViolation(TmsUnavailable):
    """A response arrived but broke the framing rules."""


class TmsCommandError(TmsError):
    """The server rejected the request with a well-formed ERR line.

    This is a deterministic business answer (unknown load, already booked,
    invalid rate) and must never be retried.
    """


class TmsBookingUncertain(TmsError):
    """A booking attempt failed in a way that leaves its outcome unknown.

    LOAD_BOOK is not idempotent — a timeout means the write may or may not have
    landed server-side. Never blind-retry; escalate to a human instead.
    """
