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
from datetime import date, datetime, timedelta

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


_STATE_CODES = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
    "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)

_STATE_NAMES = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR",
    "CALIFORNIA": "CA", "CALI": "CA", "COLORADO": "CO", "CONNECTICUT": "CT",
    "DELAWARE": "DE", "DISTRICT OF COLUMBIA": "DC", "WASHINGTON DC": "DC", "D C": "DC",
    "FLORIDA": "FL", "GEORGIA": "GA", "HAWAII": "HI", "IDAHO": "ID",
    "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD",
    "MASSACHUSETTS": "MA", "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS",
    "MISSOURI": "MO", "MONTANA": "MT", "NEBRASKA": "NE", "NEVADA": "NV",
    "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM", "NEW YORK": "NY",
    "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI",
    "SOUTH CAROLINA": "SC", "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX",
    "UTAH": "UT", "VERMONT": "VT", "VIRGINIA": "VA", "WASHINGTON": "WA",
    "WASHINGTON STATE": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
}

_WEEKDAYS = {
    "MONDAY": 0, "TUESDAY": 1, "WEDNESDAY": 2, "THURSDAY": 3,
    "FRIDAY": 4, "SATURDAY": 5, "SUNDAY": 6,
}


def equipment_label(value: str | None) -> str:
    """DRY_VAN -> "Dry Van", for a sentence read aloud."""
    if not value:
        return "that equipment"
    return _EQUIPMENT_LABELS.get(value, value.replace("_", " ").title())


def normalise_state(value: str | None) -> str | None:
    """Map a spoken state onto a wire-legal two-letter code.

    Same reason as normalise_equipment: the TMS rejects anything that is not two
    uppercase letters, so a carrier who says "California" would otherwise end the
    search with a wire error. Returns None rather than a nearest match — guessing
    a state would put a carrier on the wrong side of the country.
    """
    if not value:
        return None
    key = " ".join(value.strip().upper().replace(".", "").split())
    if key in _STATE_CODES:
        return key
    return _STATE_NAMES.get(key)


def normalise_pickup_date(value: str | None, *, today: date | None = None) -> str | None:
    """Map a spoken pickup day onto the wire's YYYYMMDD.

    The wire takes an exact calendar day and nothing else, so "Thursday" used to be
    dash-stripped and sent verbatim, which the TMS answered as malformed. `today` is
    resolved once per request by the caller so a long call cannot drift mid-request.
    """
    if not value:
        return None
    today = today or date.today()
    key = " ".join(value.strip().upper().split())

    relative = {"TODAY": 0, "TONIGHT": 0, "TOMORROW": 1, "DAY AFTER TOMORROW": 2}
    if key in relative:
        return (today + timedelta(days=relative[key])).strftime("%Y%m%d")

    forced_next = key.startswith("NEXT ")
    weekday_key = key[5:].strip() if forced_next else key
    if weekday_key in _WEEKDAYS:
        ahead = (_WEEKDAYS[weekday_key] - today.weekday()) % 7
        if forced_next:
            ahead = ahead + 7 if ahead == 0 else ahead
        return (today + timedelta(days=ahead)).strftime("%Y%m%d")

    # Only dashes are stripped here. Stripping slashes too would turn
    # "09/16/2026" into the 8 digits "09162026" and fail it as YYYYMMDD before
    # the MM/DD/YYYY branch below ever ran.
    digits = key.replace("-", "")
    if len(digits) == 8 and digits.isdigit():
        try:
            return datetime.strptime(digits, "%Y%m%d").strftime("%Y%m%d")
        except ValueError:
            return None

    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(key, fmt).strftime("%Y%m%d")
        except ValueError:
            pass

    # "9/16" — the next occurrence, so a carrier naming a day in December in
    # January does not get sent eleven months back.
    parts = key.split("/")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        month, day = int(parts[0]), int(parts[1])
        for year in (today.year, today.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                return None
            if candidate >= today:
                return candidate.strftime("%Y%m%d")
    return None


def spoken_day(wire_date: str | None) -> str | None:
    """YYYYMMDD -> "Friday September 11", for a concession the agent reads aloud."""
    if not wire_date:
        return None
    try:
        return datetime.strptime(wire_date, "%Y%m%d").strftime("%A %B %-d")
    except ValueError:
        return None


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
