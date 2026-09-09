"""Wire codec for the Legacy TMS line protocol.

Framing (confirmed against the live instance, 2026-09-08):
  request   CMD:<cmd>|AUTH:<token>|KEY:VALUE|...\r\n
  success   KEY:VALUE|...\r\n  (zero or more)  then  END\r\n
  error     ERR|CODE:<code>|MSG:<msg>\r\n

Records are parsed by key, never by offset. The manual suggests counting fixed
field widths from sample transcripts; the live server pads differently than those
samples (numerics are left-aligned space-padded, not zero-padded), so width-based
parsing would have silently produced wrong numbers.
"""

from __future__ import annotations

FRAME_TERMINATOR = "\r\n"
MAX_FRAME_BYTES = 4096
END_LINE = "END"
ERROR_PREFIX = "ERR"


class ProtocolError(ValueError):
    """The bytes on the wire do not conform to the framing rules."""


def encode_request(command: str, token: str, fields: dict[str, object] | None = None) -> bytes:
    """Build a single request frame. CMD first, AUTH second, then the rest."""
    parts = [f"CMD:{command}", f"AUTH:{token}"]
    for key, value in (fields or {}).items():
        if value is None:
            continue
        text = str(value)
        if "|" in text or "\r" in text or "\n" in text:
            raise ProtocolError(f"illegal delimiter in value for {key!r}")
        parts.append(f"{key.upper()}:{text}")

    frame = "|".join(parts) + FRAME_TERMINATOR
    encoded = frame.encode("ascii", errors="strict")
    if len(encoded) > MAX_FRAME_BYTES:
        raise ProtocolError(f"frame of {len(encoded)}B exceeds the {MAX_FRAME_BYTES}B limit")
    return encoded


MARKER_KEY = "_MARKER"


def parse_record(line: str) -> dict[str, str]:
    """Parse one `KEY:VALUE|KEY:VALUE` record line into a stripped dict.

    A leading bare token with no colon is a response marker, not a defect:
    DEBUG_ECHO answers `ECHO|AUTH:OK|...` and errors answer `ERR|CODE:...`.
    It is kept under `_MARKER`. A bare token anywhere else is a framing
    violation and is reported as one.
    """
    record: dict[str, str] = {}
    for index, segment in enumerate(line.split("|")):
        if not segment:
            continue
        key, separator, value = segment.partition(":")
        if not separator:
            if index == 0:
                record[MARKER_KEY] = key.strip().upper()
                continue
            raise ProtocolError(f"segment without a key/value separator: {segment!r}")
        record[key.strip().upper()] = value.strip()
    return record


def parse_error(line: str) -> tuple[str, str]:
    """Parse `ERR|CODE:x|MSG:y` into (code, message)."""
    fields = parse_record(line[len(ERROR_PREFIX):].lstrip("|"))
    return fields.get("CODE", "UNKNOWN"), fields.get("MSG", "")


def is_error_line(line: str) -> bool:
    return line.startswith(ERROR_PREFIX)


def split_frames(payload: str) -> list[str]:
    """Split a raw response buffer into non-empty logical lines."""
    return [line for line in payload.split(FRAME_TERMINATOR) if line]
