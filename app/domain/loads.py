"""Load representations.

Two shapes, deliberately separate types rather than one type with a flag:

  Load          internal. Carries max_rate. Never serialised to a caller.
  CarrierLoad   what the voice agent may read aloud. Has no max_rate field at
                all, so there is no code path — and no future edit — that can
                accidentally include it.

The Legacy TMS returns MAX_BUY only on tokens flagged for it. Absence means
"no ceiling on record", which is NOT the same as a ceiling of zero: a missing
ceiling must block booking rather than reject every rate.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

EQUIPMENT_TYPES = ("DRY_VAN", "REEFER", "FLATBED", "STEP_DECK", "POWER_ONLY")

_EQUIPMENT_LABELS = {
    "DRY_VAN": "Dry Van",
    "REEFER": "Reefer",
    "FLATBED": "Flatbed",
    "STEP_DECK": "Step Deck",
    "POWER_ONLY": "Power Only",
}

_EQUIPMENT_ALIASES = {
    "DRY VAN": "DRY_VAN", "DRYVAN": "DRY_VAN", "VAN": "DRY_VAN", "V": "DRY_VAN",
    "REEFER": "REEFER", "REFRIGERATED": "REEFER", "R": "REEFER", "TEMP CONTROLLED": "REEFER",
    "FLATBED": "FLATBED", "FLAT BED": "FLATBED", "FLAT": "FLATBED", "F": "FLATBED",
    "STEP DECK": "STEP_DECK", "STEPDECK": "STEP_DECK", "SD": "STEP_DECK", "DROP DECK": "STEP_DECK",
    "POWER ONLY": "POWER_ONLY", "POWERONLY": "POWER_ONLY", "PO": "POWER_ONLY",
}


def normalise_equipment(value: str | None) -> str | None:
    """Map whatever a carrier says on a call onto a wire-legal EQTYPE.

    The TMS rejects an unknown EQTYPE outright, so a carrier saying "flat bed"
    would otherwise end the search with an error instead of results.
    """
    if not value:
        return None
    key = value.strip().upper().replace("-", " ")
    if key in EQUIPMENT_TYPES:
        return key
    collapsed = key.replace(" ", "_")
    if collapsed in EQUIPMENT_TYPES:
        return collapsed
    return _EQUIPMENT_ALIASES.get(key)


def _to_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S")
    except ValueError:
        return None


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _spoken_datetime(value: datetime | None) -> str | None:
    """A phrasing a voice agent can say without reading digits aloud."""
    if not value:
        return None
    return value.strftime("%A %B %-d at %-I:%M %p")


@dataclass(frozen=True)
class Load:
    load_id: str
    origin_city: str
    origin_state: str
    origin_zip: str
    destination_city: str
    destination_state: str
    destination_zip: str
    pickup_datetime: datetime | None
    delivery_datetime: datetime | None
    equipment_type: str
    loadboard_rate: int
    status: str
    miles: int | None = None
    weight: int | None = None
    commodity_type: str | None = None
    num_of_pieces: int | None = None
    dimensions: str | None = None
    notes: str | None = None
    # Never serialise. Enforced structurally by CarrierLoad and at runtime by
    # the leak guard middleware.
    max_rate: int | None = None

    @classmethod
    def from_record(cls, record: dict[str, str]) -> "Load":
        return cls(
            load_id=record.get("LOAD_ID", "").strip(),
            origin_city=record.get("ORIG_CITY", "").strip(),
            origin_state=record.get("ORIG_STATE", "").strip(),
            origin_zip=record.get("ORIG_ZIP", "").strip(),
            destination_city=record.get("DEST_CITY", "").strip(),
            destination_state=record.get("DEST_STATE", "").strip(),
            destination_zip=record.get("DEST_ZIP", "").strip(),
            pickup_datetime=_to_datetime(record.get("PICKUP_DT")),
            delivery_datetime=_to_datetime(record.get("DELIVERY_DT")),
            equipment_type=record.get("EQTYPE", "").strip(),
            loadboard_rate=_to_int(record.get("RATE")) or 0,
            status=record.get("STATUS", "").strip(),
            miles=_to_int(record.get("MILES")),
            weight=_to_int(record.get("WEIGHT")),
            commodity_type=(record.get("COMMODITY") or "").strip() or None,
            num_of_pieces=_to_int(record.get("PIECES")),
            dimensions=(record.get("DIMS") or "").strip() or None,
            # A genuinely blank NOTES is space-padded on the wire and collapses
            # to None here. That is intended, not lossy.
            notes=(record.get("NOTES") or "").strip() or None,
            max_rate=_to_int(record.get("MAX_BUY")),
        )

    @property
    def origin(self) -> str:
        return f"{self.origin_city}, {self.origin_state}"

    @property
    def destination(self) -> str:
        return f"{self.destination_city}, {self.destination_state}"

    @property
    def rate_per_mile(self) -> float | None:
        if not self.miles:
            return None
        return round(self.loadboard_rate / self.miles, 2)

    def for_carrier(self) -> "CarrierLoad":
        return CarrierLoad(
            load_id=self.load_id,
            origin=self.origin,
            destination=self.destination,
            origin_state=self.origin_state,
            destination_state=self.destination_state,
            pickup_datetime=_iso(self.pickup_datetime),
            delivery_datetime=_iso(self.delivery_datetime),
            pickup_spoken=_spoken_datetime(self.pickup_datetime),
            delivery_spoken=_spoken_datetime(self.delivery_datetime),
            equipment_type=_EQUIPMENT_LABELS.get(self.equipment_type, self.equipment_type),
            loadboard_rate=self.loadboard_rate,
            miles=self.miles,
            rate_per_mile=self.rate_per_mile,
            weight=self.weight,
            commodity_type=self.commodity_type,
            num_of_pieces=self.num_of_pieces,
            dimensions=self.dimensions,
            notes=self.notes,
        )


@dataclass(frozen=True)
class CarrierLoad:
    """Carrier-facing projection. There is intentionally no max_rate field."""

    load_id: str
    origin: str
    destination: str
    origin_state: str
    destination_state: str
    pickup_datetime: str | None
    delivery_datetime: str | None
    pickup_spoken: str | None
    delivery_spoken: str | None
    equipment_type: str
    loadboard_rate: int
    miles: int | None
    rate_per_mile: float | None
    weight: int | None
    commodity_type: str | None
    num_of_pieces: int | None
    dimensions: str | None
    notes: str | None
