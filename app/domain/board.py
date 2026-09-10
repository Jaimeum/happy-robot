"""A read-through snapshot of the TMS load board, and the relaxation ladder over it.

WHY THIS EXISTS, AND WHY IT IS NOT A DATABASE
---------------------------------------------
The brief requires that all call activity be captured with HappyRobot-native
tooling (Twin), and that an external database be justified in writing. This is
not that, and the distinction is load-bearing:

  * It holds ~50 rows of freight geography — origin, destination, equipment,
    pickup, posted rate — for ninety seconds. Nothing else.
  * It holds no call activity, no carrier PII, no booking, no negotiation and no
    rate ceiling. Twin remains the data layer for all of that, unchanged.
  * It has no write path. Nothing reads it as a system of record; the TMS is
    re-consulted before a load's details are quoted and before it is booked.
  * It adds no infrastructure. No Redis, no Postgres, no volume, no container.
    It dies with the process, which is what the rule exists to prevent.

It exists because the TMS answers only the exact question asked — strict AND on
exact-match ORIG_STATE/EQTYPE and an exact calendar day, with no radius, no range
and no OR — so the bridge could never tell an agent "here is what the board
actually holds" without a burst of extra socket calls on a live call. Over a
snapshot the whole ladder is microseconds of filtering.

CEILING SAFETY
--------------
This index is built strictly from LOAD_QUERY records, which carry no MAX_BUY;
the ceiling appears only on LOAD_GET. Every Load in here therefore has
max_rate=None. Never enrich a row here via get_load — that would drag rate
ceilings into a long-lived process-wide structure, which is exactly the thing
the carrier-facing type split exists to prevent.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, time as time_of_day, timedelta

from app.config import Settings
from app.domain.loads import EQUIPMENT_TYPES, Load
from app.tms.client import TmsClient

logger = logging.getLogger(__name__)

# How far a pickup day is widened before the day is dropped altogether.
_PICKUP_WINDOW_DAYS = 3
# origin_states is a spoken menu, not a report. Past a dozen the agent should be
# asking a question, not reading a list.
_MAX_STATE_ROWS = 12
_MAX_CITIES_PER_STATE = 3


@dataclass(frozen=True)
class NormalisedFilters:
    """What the carrier asked for, already mapped onto wire-legal values."""

    equipment: str | None = None
    origin_city: str | None = None
    origin_state: str | None = None
    destination_city: str | None = None
    destination_state: str | None = None
    pickup_date: str | None = None  # YYYYMMDD


def is_offerable(load: Load, settings: Settings) -> bool:
    if load.status.upper() not in settings.offerable_statuses:
        return False
    if load.pickup_datetime is None:
        return True
    # A pickup earlier today has already gone. The live board carries a PENDING
    # dry van whose pickup was 03:03 this morning; pitching that is worse than
    # saying there is nothing.
    return load.pickup_datetime >= datetime.combine(date.today(), time_of_day.min)


@dataclass(frozen=True)
class BoardSnapshot:
    loads: tuple[Load, ...]
    offerable: tuple[Load, ...]
    fetched_at: float
    shards_ok: int
    shards_total: int
    failed_equipment: tuple[str, ...] = ()

    @classmethod
    def from_records(
        cls, records: list[dict[str, str]], settings: Settings, *, shards_ok: int | None = None
    ) -> "BoardSnapshot":
        """Build a snapshot from raw wire records, without a sweep.

        Used by a warm start and by the test suite, so tests exercise the same
        offerable filter and the same ladder the live path uses.
        """
        loads = tuple(Load.from_record(r) for r in records if r.get("LOAD_ID"))
        offerable = tuple(l for l in loads if is_offerable(l, settings))
        return cls(
            loads=loads,
            offerable=offerable,
            fetched_at=time.monotonic(),
            shards_ok=shards_ok if shards_ok is not None else len(EQUIPMENT_TYPES),
            shards_total=len(EQUIPMENT_TYPES),
        )

    @property
    def age_seconds(self) -> int:
        return int(time.monotonic() - self.fetched_at)

    def state(self, settings: Settings) -> str:
        if self.shards_ok < self.shards_total:
            return "partial"
        if self.age_seconds > settings.board_stale_after_seconds:
            return "stale"
        return "fresh"


@dataclass(frozen=True)
class LadderResult:
    rung: str
    loads: list[Load]
    dropped: list[str] = field(default_factory=list)
    exhausted: bool = False


class BoardIndex:
    """Keeps one snapshot warm. Never makes a caller wait for a sweep."""

    def __init__(self, tms: TmsClient, settings: Settings) -> None:
        self._tms = tms
        self._settings = settings
        self._snapshot: BoardSnapshot | None = None
        self._lock = asyncio.Lock()

    def get(self) -> BoardSnapshot | None:
        """The current snapshot, or None before the first successful sweep.

        A stale snapshot is returned as-is and a refresh is scheduled behind it.
        No carrier ever waits on a sweep.
        """
        snapshot = self._snapshot
        if snapshot and snapshot.age_seconds > self._settings.board_snapshot_ttl_seconds:
            try:
                asyncio.get_running_loop().create_task(self._refresh_quietly())
            except RuntimeError:  # no loop (sync test context) — nothing to schedule
                pass
        return snapshot

    async def _refresh_quietly(self) -> None:
        try:
            await self.refresh()
        except Exception:  # a background sweep must never surface anywhere
            logger.warning("board.refresh_failed", exc_info=True)

    async def refresh(self) -> BoardSnapshot | None:
        """Sweep the board, one shard per equipment type, concurrently.

        A shard that fails is recorded rather than hidden: an equipment type whose
        shard failed must never be part of a "there is nothing anywhere" claim.
        A sweep where every shard fails leaves the previous snapshot in place.
        """
        async with self._lock:
            shards = await asyncio.gather(
                *(
                    self._tms.query_loads(
                        {"EQTYPE": eq, "MAX_RESULTS": self._settings.board_shard_max_results}
                    )
                    for eq in EQUIPMENT_TYPES
                ),
                return_exceptions=True,
            )

            by_id: dict[str, Load] = {}
            ok = 0
            failed: list[str] = []
            for equipment, result in zip(EQUIPMENT_TYPES, shards):
                if isinstance(result, BaseException):
                    failed.append(equipment)
                    logger.warning("board.shard_failed equipment=%s error=%s", equipment, result)
                    continue
                ok += 1
                for record in result:
                    load = Load.from_record(record)
                    if load.load_id:
                        by_id[load.load_id] = load

            if ok == 0:
                logger.warning("board.refresh_all_shards_failed keeping_previous=%s",
                               self._snapshot is not None)
                return self._snapshot

            loads = tuple(by_id.values())
            snapshot = BoardSnapshot(
                loads=loads,
                offerable=tuple(l for l in loads if self._is_offerable(l)),
                fetched_at=time.monotonic(),
                shards_ok=ok,
                shards_total=len(EQUIPMENT_TYPES),
                failed_equipment=tuple(failed),
            )
            self._snapshot = snapshot
            logger.info(
                "board.refreshed indexed=%d offerable=%d shards=%d/%d",
                len(snapshot.loads), len(snapshot.offerable), ok, len(EQUIPMENT_TYPES),
            )
            return snapshot

    def _is_offerable(self, load: Load) -> bool:
        return is_offerable(load, self._settings)

    def prime(self, snapshot: BoardSnapshot) -> None:
        """Install a snapshot without sweeping. Used by tests and a warm start."""
        self._snapshot = snapshot

    async def run_forever(self) -> None:
        while True:
            await self._refresh_quietly()
            await asyncio.sleep(self._settings.board_refresh_interval_seconds)


# ─────────────────────────────────────────────────────────────────
# Matching — mirrors the wire semantics measured on 2026-09-10:
# exact match on state and equipment, case-insensitive substring on city,
# exact calendar-day match on pickup.
# ─────────────────────────────────────────────────────────────────

def _matches(load: Load, f: NormalisedFilters, *, pickup_window: int = 0) -> bool:
    if f.equipment and load.equipment_type != f.equipment:
        return False
    if f.origin_state and load.origin_state.upper() != f.origin_state:
        return False
    if f.destination_state and load.destination_state.upper() != f.destination_state:
        return False
    if f.origin_city and f.origin_city.lower() not in load.origin_city.lower():
        return False
    if f.destination_city and f.destination_city.lower() not in load.destination_city.lower():
        return False
    if f.pickup_date:
        if load.pickup_datetime is None:
            return False
        wanted = datetime.strptime(f.pickup_date, "%Y%m%d").date()
        actual = load.pickup_datetime.date()
        if abs((actual - wanted).days) > pickup_window:
            return False
    return True


def _rank(loads: list[Load], f: NormalisedFilters) -> list[Load]:
    return sorted(
        loads,
        key=lambda l: (
            l.origin_state.upper() != (f.origin_state or ""),
            l.pickup_datetime or datetime.max,
            -(l.rate_per_mile or 0),
        ),
    )


def widen(snapshot: BoardSnapshot, filters: NormalisedFilters) -> LadderResult:
    """Walk down the ladder until something real comes back.

    Order is deliberate. The pickup day goes first because it is almost always the
    binding constraint — roughly one dry van a day across the board, and six of the
    ten searches in the six-minute call pinned a date. Destination goes next
    because flexibility on destination is what a broker actually sells. The origin
    city goes last, because origin is the one thing a carrier cannot change — and
    dropping it is precisely what surfaces the one California dry van.

    EQTYPE is never relaxed at any rung: a carrier with a van is never offered a
    step deck. This function only ever removes or widens a filter, never adds one.
    """
    rows = list(snapshot.offerable)

    rungs: list[tuple[str, NormalisedFilters, list[str], int]] = [
        ("exact", filters, [], 0),
    ]
    if filters.pickup_date:
        rungs.append(("pickup_window", filters, [], _PICKUP_WINDOW_DAYS))
        rungs.append((
            "any_date",
            NormalisedFilters(**{**filters.__dict__, "pickup_date": None}),
            ["pickup_date"], 0,
        ))
    base = NormalisedFilters(**{**filters.__dict__, "pickup_date": None})
    dropped_so_far = ["pickup_date"] if filters.pickup_date else []

    if filters.destination_city:
        base = NormalisedFilters(**{**base.__dict__, "destination_city": None})
        dropped_so_far = dropped_so_far + ["destination_city"]
        rungs.append(("dest_state_only", base, list(dropped_so_far), 0))
    if filters.destination_state:
        base = NormalisedFilters(**{**base.__dict__, "destination_state": None})
        dropped_so_far = dropped_so_far + ["destination_state"]
        rungs.append(("any_destination", base, list(dropped_so_far), 0))
    if filters.origin_city:
        base = NormalisedFilters(**{**base.__dict__, "origin_city": None})
        dropped_so_far = dropped_so_far + ["origin_city"]
        rungs.append(("origin_state", base, list(dropped_so_far), 0))
    if filters.origin_state:
        base = NormalisedFilters(**{**base.__dict__, "origin_state": None})
        dropped_so_far = dropped_so_far + ["origin_state"]
        rungs.append(("equipment_only", base, list(dropped_so_far), 0))

    for rung, rung_filters, dropped, window in rungs:
        hits = [l for l in rows if _matches(l, rung_filters, pickup_window=window)]
        if hits:
            return LadderResult(rung=rung, loads=_rank(hits, filters), dropped=dropped)

    # Nothing at any rung. That is only "exhausted" if we can see the whole board
    # for that equipment — a failed shard is ignorance, not absence.
    equipment_rows = [
        l for l in rows if not filters.equipment or l.equipment_type == filters.equipment
    ]
    exhausted = not equipment_rows and (
        filters.equipment is None or filters.equipment not in snapshot.failed_equipment
    )
    return LadderResult(rung="none", loads=[], dropped=dropped_so_far, exhausted=exhausted)


def board_facts(
    snapshot: BoardSnapshot,
    settings: Settings,
    equipment: str | None,
    origin_state: str | None,
) -> dict:
    """The real option set: only what exists, never a zero.

    Every row carries load_count >= 1 and zero-count rows are omitted, so there is
    nothing in this payload the agent can offer that is not actually on the board.
    Carries no rates of any kind.
    """
    offerable = snapshot.offerable
    for_equipment = [
        l for l in offerable if not equipment or l.equipment_type == equipment
    ]

    by_state: dict[str, list[Load]] = {}
    for load in for_equipment:
        by_state.setdefault(load.origin_state.upper(), []).append(load)

    state_rows = sorted(
        (
            {
                "state": state,
                "load_count": len(rows),
                "cities": sorted({r.origin_city for r in rows})[:_MAX_CITIES_PER_STATE],
            }
            for state, rows in by_state.items()
            if rows
        ),
        key=lambda row: (-row["load_count"], row["state"]),
    )

    breakdown: dict[str, int] = {}
    for load in offerable:
        breakdown[load.equipment_type] = breakdown.get(load.equipment_type, 0) + 1

    return {
        "state": snapshot.state(settings),
        "snapshot_age_seconds": snapshot.age_seconds,
        "loads_indexed": len(snapshot.loads),
        "loads_offerable": len(offerable),
        "shards_ok": snapshot.shards_ok,
        "equipment_type": equipment,
        "open_for_equipment": len(for_equipment),
        "open_in_requested_state": len(by_state.get(origin_state or "", [])),
        "origin_states": state_rows[:_MAX_STATE_ROWS],
        "origin_states_truncated": len(state_rows) > _MAX_STATE_ROWS,
        "equipment_breakdown": sorted(
            (
                {"equipment_type": eq, "load_count": n}
                for eq, n in breakdown.items()
                if n
            ),
            key=lambda row: -row["load_count"],
        ),
    }
