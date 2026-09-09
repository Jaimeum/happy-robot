"""Rate negotiation policy.

Two hard rules from the brief live here, and both are enforced in code rather
than in a prompt, because a prompt can be talked out of them:

  1. max_rate is never disclosed, directly or indirectly. Our counters are
     always held strictly below the ceiling, and agreement always lands on a
     number the carrier themselves named. The ceiling is therefore never
     spoken, even by a carrier who spends all three rounds probing for it.
  2. At most three counter rounds. Round four does not exist: the engine
     refuses it, marks the negotiation failed, and withholds transfer.

Discovery finding that motivates rule 1 living here: the Legacy TMS accepted a
LOAD_BOOK at three times the load's own MAX_BUY. The system of record does not
defend the ceiling, so this layer is the only thing that does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum

# How much of the remaining gap we concede on each successive round. Front-loaded
# so a reasonable carrier closes fast, then tightening as room runs out.
CONCESSION_SCHEDULE = (0.50, 0.65, 0.80)


class Decision(StrEnum):
    ACCEPTED = "accepted"
    COUNTERED = "countered"
    REJECTED = "rejected"


class NegotiationStatus(StrEnum):
    OPEN = "open"
    AGREED = "agreed"
    FAILED = "failed"


@dataclass
class Round:
    number: int
    carrier_offer: int
    decision: Decision
    broker_counter: int | None
    at: datetime

    def as_audit(self) -> dict[str, object]:
        return {
            "round": self.number,
            "carrier_offer": self.carrier_offer,
            "decision": str(self.decision),
            "broker_counter": self.broker_counter,
            "at": self.at.isoformat(),
        }


@dataclass
class NegotiationOutcome:
    decision: Decision
    round_number: int
    rounds_remaining: int
    status: NegotiationStatus
    broker_counter: int | None = None
    agreed_rate: int | None = None
    may_book: bool = False
    may_transfer: bool = False
    reason: str = ""
    # Internal-only. Recorded in the audit trail, never returned to a caller.
    within_ceiling: bool = False


@dataclass
class Negotiation:
    load_id: str
    loadboard_rate: int
    max_rate: int | None
    max_rounds: int
    ceiling_buffer_pct: float
    auto_accept_pct: float
    current_offer: int = 0
    status: NegotiationStatus = NegotiationStatus.OPEN
    agreed_rate: int | None = None
    rounds: list[Round] = field(default_factory=list)
    ceiling_breach_attempts: int = 0

    def __post_init__(self) -> None:
        if not self.current_offer:
            self.current_offer = self.loadboard_rate

    @property
    def rounds_used(self) -> int:
        return len(self.rounds)

    @property
    def rounds_remaining(self) -> int:
        return max(0, self.max_rounds - self.rounds_used)

    @property
    def counter_cap(self) -> int | None:
        """Highest number we will ever say out loud. Strictly below the ceiling."""
        if self.max_rate is None:
            return None
        return int(math.floor(self.max_rate * (1 - self.ceiling_buffer_pct)))

    @property
    def margin(self) -> int | None:
        """Ceiling minus what we actually agreed — the value this call protected."""
        if self.max_rate is None or self.agreed_rate is None:
            return None
        return self.max_rate - self.agreed_rate

    def evaluate(self, carrier_offer: int) -> NegotiationOutcome:
        if self.status is not NegotiationStatus.OPEN:
            return NegotiationOutcome(
                decision=Decision.REJECTED if self.status is NegotiationStatus.FAILED else Decision.ACCEPTED,
                round_number=self.rounds_used,
                rounds_remaining=0,
                status=self.status,
                agreed_rate=self.agreed_rate,
                may_book=self.status is NegotiationStatus.AGREED,
                may_transfer=self.status is NegotiationStatus.AGREED,
                reason="negotiation is already closed",
            )

        if self.rounds_used >= self.max_rounds:
            return self._fail("round limit already reached")

        if self.max_rate is None:
            # No ceiling on record for this load. Refuse to improvise one.
            return self._fail("no rate ceiling on record for this load")

        round_number = self.rounds_used + 1
        is_final_round = round_number >= self.max_rounds
        within_ceiling = carrier_offer <= self.max_rate
        if not within_ceiling:
            self.ceiling_breach_attempts += 1

        # The carrier asked for no more than we are already offering. Take it.
        if carrier_offer <= self.current_offer:
            return self._accept(round_number, carrier_offer, within_ceiling,
                                "carrier accepted at or below our standing offer")

        if within_ceiling:
            close_enough = carrier_offer <= self.current_offer * (1 + self.auto_accept_pct)
            if is_final_round or close_enough:
                reason = ("final round, carrier ask is within ceiling"
                          if is_final_round else "carrier ask is within reach of our offer")
                return self._accept(round_number, carrier_offer, within_ceiling, reason)
            return self._counter(round_number, carrier_offer, within_ceiling,
                                 "countering to protect margin")

        if is_final_round:
            return self._fail("no agreement within three rounds", carrier_offer=carrier_offer,
                              within_ceiling=False)
        return self._counter(round_number, carrier_offer, within_ceiling,
                             "carrier ask exceeds what this lane supports")

    def _accept(self, round_number: int, rate: int, within_ceiling: bool,
                reason: str) -> NegotiationOutcome:
        self.status = NegotiationStatus.AGREED
        self.agreed_rate = rate
        self.current_offer = rate
        self.rounds.append(
            Round(round_number, rate, Decision.ACCEPTED, None, _now())
        )
        return NegotiationOutcome(
            decision=Decision.ACCEPTED,
            round_number=round_number,
            rounds_remaining=self.rounds_remaining,
            status=self.status,
            agreed_rate=rate,
            may_book=True,
            may_transfer=True,
            reason=reason,
            within_ceiling=within_ceiling,
        )

    def _counter(self, round_number: int, carrier_offer: int, within_ceiling: bool,
                 reason: str) -> NegotiationOutcome:
        counter = self._next_counter(round_number, carrier_offer)
        self.current_offer = counter
        self.rounds.append(
            Round(round_number, carrier_offer, Decision.COUNTERED, counter, _now())
        )
        return NegotiationOutcome(
            decision=Decision.COUNTERED,
            round_number=round_number,
            rounds_remaining=self.rounds_remaining,
            status=self.status,
            broker_counter=counter,
            may_book=False,
            may_transfer=False,
            reason=reason,
            within_ceiling=within_ceiling,
        )

    def _fail(self, reason: str, carrier_offer: int | None = None,
              within_ceiling: bool = False) -> NegotiationOutcome:
        self.status = NegotiationStatus.FAILED
        if carrier_offer is not None:
            self.rounds.append(
                Round(self.rounds_used + 1, carrier_offer, Decision.REJECTED, None, _now())
            )
        return NegotiationOutcome(
            decision=Decision.REJECTED,
            round_number=self.rounds_used,
            rounds_remaining=0,
            status=self.status,
            may_book=False,
            # The brief is explicit: a failed negotiation is not transferred.
            may_transfer=False,
            reason=reason,
            within_ceiling=within_ceiling,
        )

    def _next_counter(self, round_number: int, carrier_offer: int) -> int:
        cap = self.counter_cap
        assert cap is not None  # guarded by the max_rate check in evaluate()

        concession = CONCESSION_SCHEDULE[min(round_number, len(CONCESSION_SCHEDULE)) - 1]
        target = self.current_offer + concession * (carrier_offer - self.current_offer)
        counter = int(math.floor(min(target, cap)))
        # Never move backwards, and never move above the cap even if the posted
        # rate already sits above it.
        return max(min(counter, cap), min(self.current_offer, cap))

    def as_audit(self) -> dict[str, object]:
        return {
            "load_id": self.load_id,
            "loadboard_rate": self.loadboard_rate,
            "max_rate": self.max_rate,
            "status": str(self.status),
            "agreed_rate": self.agreed_rate,
            "margin_protected": self.margin,
            "rounds_used": self.rounds_used,
            "ceiling_breach_attempts": self.ceiling_breach_attempts,
            "rounds": [r.as_audit() for r in self.rounds],
        }


def _now() -> datetime:
    return datetime.now(timezone.utc)
