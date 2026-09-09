"""One-time-passcode issue and verification.

Properties that matter for the "resist social engineering" requirement:

  * The code is generated here with `secrets`, never supplied by the caller.
  * `verify` is the only function that can mark a challenge verified, and it
    only does so on a constant-time match of a live, unexpired code.
  * Attempts are capped; a burned challenge cannot be revived, only reissued,
    and reissuing rolls a fresh code.
  * No function in this module takes a "skip", "force", "override" or
    "already_known" argument. There is nothing to talk the service into.
"""

from __future__ import annotations

import hmac
import logging
import re
import secrets
import time
from dataclasses import dataclass

from app.domain.sessions import OtpChallenge

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
PHONE_PATTERN = re.compile(r"^\+?[0-9][0-9\-\s().]{6,19}$")

# Phrasings observed in the wild that try to talk past the step. Matching one is
# not what blocks the caller — the stage machine already does that — but it lets
# us count and surface the attempt for the ops team.
BYPASS_MARKERS = (
    "already in your system", "already verified", "skip the code", "skip verification",
    "phone died", "phone is dead", "no signal", "not receiving", "didn't get the code",
    "dispatcher always", "we always skip", "do this every time", "regular carrier",
    "in a hurry", "just this once", "bypass", "override", "manager approved",
    "waive", "trust me", "known carrier", "been hauling for you",
)


class OtpError(Exception):
    """Delivery target was unusable."""


@dataclass(frozen=True)
class OtpIssue:
    channel: str
    destination_masked: str
    expires_in_seconds: int


def detect_bypass_language(text: str | None) -> list[str]:
    if not text:
        return []
    lowered = text.lower()
    return [marker for marker in BYPASS_MARKERS if marker in lowered]


def mask_destination(channel: str, destination: str) -> str:
    if channel == "email":
        local, _, domain = destination.partition("@")
        head = local[:2] if len(local) > 2 else local[:1]
        return f"{head}{'*' * max(3, len(local) - len(head))}@{domain}"
    digits = re.sub(r"\D", "", destination)
    return f"***-***-{digits[-4:]}" if len(digits) >= 4 else "***"


def normalise_channel(channel: str | None, destination: str) -> str:
    if channel:
        lowered = channel.strip().lower()
        if lowered in {"email", "mail", "e-mail"}:
            return "email"
        if lowered in {"sms", "text", "phone", "mobile"}:
            return "sms"
    return "email" if "@" in destination else "sms"


def validate_destination(channel: str, destination: str) -> None:
    target = destination.strip()
    if channel == "email":
        if not EMAIL_PATTERN.match(target):
            raise OtpError("that email address does not look valid")
    elif not PHONE_PATTERN.match(target):
        raise OtpError("that phone number does not look valid")


def issue(
    *,
    channel: str,
    destination: str,
    ttl_seconds: int,
    length: int = 6,
) -> tuple[OtpChallenge, OtpIssue]:
    validate_destination(channel, destination)
    code = "".join(secrets.choice("0123456789") for _ in range(length))
    now = time.time()
    challenge = OtpChallenge(
        code=code,
        channel=channel,
        destination_masked=mask_destination(channel, destination),
        issued_at=now,
        expires_at=now + ttl_seconds,
    )
    return challenge, OtpIssue(
        channel=channel,
        destination_masked=challenge.destination_masked,
        expires_in_seconds=ttl_seconds,
    )


class VerifyResult:
    OK = "verified"
    WRONG = "incorrect_code"
    EXPIRED = "expired"
    EXHAUSTED = "attempts_exhausted"
    NOT_ISSUED = "no_challenge_issued"


def verify(challenge: OtpChallenge | None, submitted: str, max_attempts: int) -> str:
    if challenge is None:
        return VerifyResult.NOT_ISSUED
    if challenge.verified:
        return VerifyResult.OK
    if challenge.attempts_used >= max_attempts:
        return VerifyResult.EXHAUSTED
    if challenge.is_expired():
        return VerifyResult.EXPIRED

    challenge.attempts_used += 1
    candidate = re.sub(r"\D", "", submitted or "")
    if candidate and hmac.compare_digest(candidate, challenge.code):
        challenge.verified = True
        return VerifyResult.OK
    if challenge.attempts_used >= max_attempts:
        return VerifyResult.EXHAUSTED
    return VerifyResult.WRONG
