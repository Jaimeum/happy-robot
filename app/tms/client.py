"""Async adapter for the Legacy TMS TCP interface.

The non-production instance injects faults on every operational command
(LOAD_QUERY / LOAD_GET / LOAD_BOOK) without signalling them: measured at roughly
5% of calls during protocol discovery. Four shapes were documented and are all
handled here:

  timeout              connection accepted, no response ever written
  partial response     a prefix of a valid response, closed without END
  malformed response   framing violations (stray delimiters, bad segments)
  delayed termination  a complete response, then the socket is held open

Delayed termination is neutralised by returning the moment END (or an ERR line)
is seen rather than waiting for the peer to close.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

from app.config import Settings
from app.tms import protocol
from app.tms.errors import (
    TmsBookingUncertain,
    TmsCommandError,
    TmsProtocolViolation,
    TmsUnavailable,
)

logger = logging.getLogger(__name__)

READ_ONLY_COMMANDS = frozenset({"LOAD_QUERY", "LOAD_GET", "DEBUG_ECHO"})

# Business rejections. Retrying these just burns call time — the answer is stable.
DETERMINISTIC_CODES = frozenset(
    {
        "AUTH_FAILED",
        "UNKNOWN_CMD",
        "MISSING_FIELD",
        "MALFORMED",
        "UNKNOWN_LOAD",
        "NOT_FOUND",
        "ALREADY_BOOKED",
        "INVALID_RATE",
    }
)


@dataclass
class TmsCallStats:
    """Per-process counters, surfaced on the ops dashboard as a reliability signal."""

    attempts: int = 0
    successes: int = 0
    timeouts: int = 0
    partials: int = 0
    malformed: int = 0
    transport_errors: int = 0
    command_errors: int = 0
    retries: int = 0
    latencies_ms: list[float] = field(default_factory=list)

    def observe_latency(self, value: float) -> None:
        self.latencies_ms.append(value)
        if len(self.latencies_ms) > 500:
            del self.latencies_ms[:-500]

    def snapshot(self) -> dict[str, object]:
        latencies = sorted(self.latencies_ms)
        p95 = latencies[int(len(latencies) * 0.95)] if latencies else 0.0
        faults = self.timeouts + self.partials + self.malformed + self.transport_errors
        return {
            "attempts": self.attempts,
            "successes": self.successes,
            "command_errors": self.command_errors,
            "retries": self.retries,
            "faults_absorbed": faults,
            "fault_rate_pct": round(100 * faults / self.attempts, 2) if self.attempts else 0.0,
            "timeouts": self.timeouts,
            "partial_responses": self.partials,
            "malformed_responses": self.malformed,
            "transport_errors": self.transport_errors,
            "latency_p95_ms": round(p95, 1),
        }


class TmsClient:
    """One TCP connection per request, as the protocol requires."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.stats = TmsCallStats()

    async def query_loads(self, filters: dict[str, object]) -> list[dict[str, str]]:
        return await self._execute("LOAD_QUERY", filters)

    async def get_load(self, load_id: str) -> dict[str, str] | None:
        try:
            records = await self._execute("LOAD_GET", {"LOAD_ID": load_id})
        except TmsCommandError as exc:
            if exc.code in {"NOT_FOUND", "UNKNOWN_LOAD"}:
                return None
            raise
        return records[0] if records else None

    async def book_load(self, load_id: str, mc_number: str, agreed_rate: int) -> dict[str, str]:
        """Commit a booking. Never retried — see TmsBookingUncertain."""
        records = await self._execute(
            "LOAD_BOOK",
            {"LOAD_ID": load_id, "MC_NUM": mc_number, "AGREED_RATE": agreed_rate},
        )
        if not records:
            raise TmsBookingUncertain(
                "booking returned no confirmation record; outcome unknown"
            )
        return records[0]

    async def echo(self, message: str = "PING") -> dict[str, str]:
        records = await self._execute("DEBUG_ECHO", {"MSG": message})
        return records[0] if records else {}

    async def _execute(self, command: str, fields: dict[str, object]) -> list[dict[str, str]]:
        frame = protocol.encode_request(command, self._settings.tms_token, fields)
        retryable = command in READ_ONLY_COMMANDS
        budget = self._settings.tms_max_retries if retryable else 1
        last_error: Exception | None = None

        for attempt in range(1, budget + 1):
            self.stats.attempts += 1
            started = time.perf_counter()
            try:
                payload = await self._roundtrip(frame, timeout=self._timeout_for(command))
                records = self._decode(payload)
            except TmsCommandError as exc:
                self.stats.command_errors += 1
                raise
            except TmsUnavailable as exc:
                last_error = exc
                self._count_fault(exc)
                if not retryable:
                    # A booking that failed mid-flight may still have landed.
                    if command == "LOAD_BOOK":
                        raise TmsBookingUncertain(
                            f"booking outcome unknown after transport fault: {exc.message}"
                        ) from exc
                    raise
                if attempt < budget:
                    self.stats.retries += 1
                    delay = min(0.25 * 2 ** (attempt - 1), 2.0) + random.uniform(0, 0.1)
                    logger.warning(
                        "tms.retry command=%s attempt=%d/%d reason=%s backoff=%.2fs",
                        command, attempt, budget, exc.__class__.__name__, delay,
                    )
                    await asyncio.sleep(delay)
                continue
            else:
                self.stats.successes += 1
                self.stats.observe_latency((time.perf_counter() - started) * 1000)
                return records

        raise TmsUnavailable(
            f"{command} did not return a usable response after {budget} attempt(s)"
        ) from last_error

    def _timeout_for(self, command: str) -> float:
        """Reads get a short budget, writes keep the long one.

        A read that inherits the booking timeout holds a live voice call silent for
        ten seconds on one unlucky socket — forty times the measured p95. A booking
        keeps the long budget, because a slow commit beats an uncertain one.
        """
        if command in READ_ONLY_COMMANDS:
            return self._settings.tms_query_timeout_seconds
        return self._settings.tms_timeout_seconds

    async def _roundtrip(self, frame: bytes, *, timeout: float | None = None) -> str:
        timeout = timeout if timeout is not None else self._settings.tms_timeout_seconds
        writer = None
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(self._settings.tms_host, self._settings.tms_port),
                timeout=timeout,
            )
            writer.write(frame)
            await asyncio.wait_for(writer.drain(), timeout=timeout)

            buffer = bytearray()
            deadline = asyncio.get_running_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    raise TmsUnavailable("read deadline exceeded")
                chunk = await asyncio.wait_for(reader.read(4096), timeout=remaining)
                if not chunk:
                    # Peer closed. Complete only if properly terminated.
                    break
                buffer.extend(chunk)
                text = buffer.decode("ascii", errors="replace")
                # Return as soon as the frame is complete; do not wait for close.
                # This is what makes "delayed termination" a non-event.
                if text.startswith(protocol.ERROR_PREFIX) and protocol.FRAME_TERMINATOR in text:
                    return text
                if text.endswith(f"{protocol.END_LINE}{protocol.FRAME_TERMINATOR}"):
                    return text
                if len(buffer) > protocol.MAX_FRAME_BYTES * 16:
                    raise TmsProtocolViolation("response exceeded the sane size ceiling")

            text = buffer.decode("ascii", errors="replace")
            if not text:
                raise TmsUnavailable("connection closed before any response was written")
            raise TmsProtocolViolation("connection closed before the END terminator")

        except asyncio.TimeoutError as exc:
            raise TmsUnavailable("timed out waiting for the TMS") from exc
        except (OSError, ConnectionError) as exc:
            raise TmsUnavailable(f"transport failure: {exc}") from exc
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=1.0)
                except (asyncio.TimeoutError, OSError):
                    pass

    def _decode(self, payload: str) -> list[dict[str, str]]:
        lines = protocol.split_frames(payload)
        if not lines:
            raise TmsProtocolViolation("empty response")

        if protocol.is_error_line(lines[0]):
            code, message = protocol.parse_error(lines[0])
            if code in DETERMINISTIC_CODES:
                raise TmsCommandError(message or code, code=code)
            # Unrecognised codes (incl. SERVER_ERROR) are treated as transient.
            raise TmsUnavailable(f"TMS reported {code}: {message}", code=code)

        if lines[-1] != protocol.END_LINE:
            raise TmsProtocolViolation("response is missing its END terminator")

        records: list[dict[str, str]] = []
        for line in lines[:-1]:
            try:
                records.append(protocol.parse_record(line))
            except protocol.ProtocolError as exc:
                raise TmsProtocolViolation(str(exc)) from exc
        return records

    def _count_fault(self, exc: TmsUnavailable) -> None:
        text = exc.message.lower()
        if isinstance(exc, TmsProtocolViolation):
            if "end terminator" in text:
                self.stats.partials += 1
            else:
                self.stats.malformed += 1
        elif "timed out" in text or "deadline" in text:
            self.stats.timeouts += 1
        else:
            self.stats.transport_errors += 1
