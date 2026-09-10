from __future__ import annotations

import datetime as _dt
import os

import pytest

# Forced, not defaulted: the compose env_file is present in the test container
# and would otherwise supply real (or empty) values.
os.environ["API_AUTH_TOKEN"] = "test-token"
os.environ["TMS_TOKEN"] = "test-tms-token"
os.environ["FMCSA_API_KEY"] = "test-fmcsa-key"
os.environ["ENV"] = "test"

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.domain.board import BoardIndex, BoardSnapshot  # noqa: E402
from app.integrations.fmcsa import CarrierAuthority  # noqa: E402
from app.main import create_app  # noqa: E402
from app.tms.client import TmsCallStats  # noqa: E402
from app.tms.errors import TmsCommandError, TmsUnavailable  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}

def _wire_dt(days_from_today: int, clock: str = "071700") -> str:
    """A wire YYYYMMDDHHMMSS relative to today.

    Fixture pickups are relative on purpose. A hardcoded future date is a time
    bomb: the offerable filter drops anything picking up before today, so a fixed
    date would silently start failing the suite once it passed.
    """
    return (_dt.date.today() + _dt.timedelta(days=days_from_today)).strftime("%Y%m%d") + clock


# A representative record in the exact shape and padding the live server emits.
LOAD_RECORD = {
    "LOAD_ID": "LD00720",
    "ORIG_CITY": "Springfield",
    "ORIG_STATE": "IL",
    "ORIG_ZIP": "62701",
    "DEST_CITY": "Worcester",
    "DEST_STATE": "MA",
    "DEST_ZIP": "01608",
    "PICKUP_DT": _wire_dt(6),
    "DELIVERY_DT": _wire_dt(8, "181700"),
    "EQTYPE": "FLATBED",
    "RATE": "2000",
    "WEIGHT": "38110",
    "COMMODITY": "Auto Parts",
    "PIECES": "28",
    "MILES": "944",
    "DIMS": "40ft x 8ft x 9ft",
    "NOTES": "",
    "STATUS": "OPEN",
    "MAX_BUY": "3000",
}


def _record(load_id, ocity, ostate, dcity, dstate, eqtype, rate, *,
            pickup=None, status="OPEN", miles="614", max_buy=None):
    """A board row in the shape the live server emits."""
    return {
        "LOAD_ID": load_id,
        "ORIG_CITY": ocity, "ORIG_STATE": ostate, "ORIG_ZIP": "00000",
        "DEST_CITY": dcity, "DEST_STATE": dstate, "DEST_ZIP": "00000",
        "PICKUP_DT": pickup or _wire_dt(6, "121000"), "DELIVERY_DT": _wire_dt(9, "181700"),
        "EQTYPE": eqtype, "RATE": str(rate), "WEIGHT": "38000",
        "COMMODITY": "General Freight", "PIECES": "12", "MILES": miles,
        "DIMS": "48ft x 8ft x 9ft", "NOTES": "", "STATUS": status,
        "MAX_BUY": str(max_buy) if max_buy else "",
    }


# The real California shape measured against the live board on 2026-09-10, plus
# national filler so board_facts has something honest to report. The point of the
# CA rows: exactly ONE dry van out of California (LD00760, and it is not in San
# Diego), and the one San Diego load is a step deck the van cannot take.
BOARD_RECORDS = [
    _record("LD00760", "San Jose", "CA", "Phoenix", "AZ", "DRY_VAN", 1579, miles="614"),
    _record("LD00784", "San Diego", "CA", "Madison", "WI", "STEP_DECK", 5144, miles="2050"),
    _record("LD00733", "San Jose", "CA", "Allentown", "PA", "REEFER", 6302, miles="2800"),
    _record("LD00752", "San Francisco", "CA", "New Orleans", "LA", "REEFER", 4284),
    # A dry van whose pickup was 03:03 this morning, and PENDING besides. It must
    # never be offered — on the live board this is the row that would have been
    # pitched first by an equipment-only search.
    _record("LD00719", "Anchorage", "AK", "Sarasota", "FL", "DRY_VAN", 10422,
            pickup=_wire_dt(0, "030300"), status="PENDING"),
    _record("LD00793", "Dallas", "TX", "New Orleans", "LA", "DRY_VAN", 1345),
    _record("LD00794", "Houston", "TX", "Memphis", "TN", "DRY_VAN", 1420),
    _record("LD00763", "Omaha", "NE", "Newark", "NJ", "DRY_VAN", 3504),
    _record("LD00779", "Syracuse", "NY", "Boise", "ID", "DRY_VAN", 6097),
    _record("LD00767", "Flint", "MI", "Jacksonville", "FL", "DRY_VAN", 1952),
    _record("LD00785", "Atlanta", "GA", "Colorado Springs", "CO", "DRY_VAN", 3605),
    _record("LD00738", "Minneapolis", "MN", "Grand Rapids", "MI", "DRY_VAN", 1093),
    _record("LD00731", "Salt Lake City", "UT", "Springfield", "IL", "DRY_VAN", 2535),
    _record("LD00730", "Virginia Beach", "VA", "Cheyenne", "WY", "DRY_VAN", 3366),
    _record("LD00757", "Chicago", "IL", "Long Beach", "CA", "FLATBED", 3222),
    _record("LD00750", "Columbia", "SC", "Cheyenne", "WY", "FLATBED", 2923),
    _record("LD00801", "Detroit", "MI", "Nashville", "TN", "POWER_ONLY", 904),
]

_WIRE_EQUIPMENT = {"DRY_VAN", "REEFER", "FLATBED", "STEP_DECK", "POWER_ONLY"}


class FakeTms:
    """Stands in for the socket adapter so API tests stay deterministic."""

    def __init__(self) -> None:
        self.stats = TmsCallStats()
        self.records = [dict(LOAD_RECORD)]
        self.query_error: Exception | None = None
        self.get_error: Exception | None = None
        self.book_error: Exception | None = None
        self.booked: list[tuple[str, str, int]] = []
        self.queries: list[dict] = []

    async def query_loads(self, filters):
        """Mirror the wire's filter semantics, measured live on 2026-09-10.

        The old fake ignored `filters` and returned everything, which would let
        every rung of the relaxation ladder return identical rows — the whole
        feature would test green while being broken.
        """
        self.queries.append(dict(filters))
        if self.query_error:
            raise self.query_error

        keys = {k for k in filters if k != "MAX_RESULTS"}
        if not keys:
            raise TmsCommandError("At least one filter required", code="MISSING_FIELD")

        for field in ("ORIG_STATE", "DEST_STATE"):
            value = filters.get(field)
            if value is not None and not (
                isinstance(value, str) and len(value) == 2 and value.isupper()
            ):
                raise TmsCommandError(f"Invalid {field}", code="MALFORMED")
        equipment = filters.get("EQTYPE")
        if equipment is not None and equipment not in _WIRE_EQUIPMENT:
            raise TmsCommandError("Invalid EQTYPE", code="MALFORMED")

        exact = {"ORIG_STATE": "ORIG_STATE", "DEST_STATE": "DEST_STATE",
                 "EQTYPE": "EQTYPE", "STATUS": "STATUS"}
        substring = {"ORIG_CITY": "ORIG_CITY", "DEST_CITY": "DEST_CITY"}

        hits = []
        for record in self.records:
            if any(
                record.get(col, "").strip() != filters[key]
                for key, col in exact.items() if key in filters
            ):
                continue
            if any(
                str(filters[key]).lower() not in record.get(col, "").lower()
                for key, col in substring.items() if key in filters
            ):
                continue
            if "PICKUP_DATE" in filters:
                if record.get("PICKUP_DT", "")[:8] != filters["PICKUP_DATE"]:
                    continue
            # MAX_BUY is never on a LOAD_QUERY record — only LOAD_GET carries it.
            hits.append({k: v for k, v in record.items() if k != "MAX_BUY"})

        limit = filters.get("MAX_RESULTS")
        return hits[: int(limit)] if limit else hits

    async def get_load(self, load_id):
        if self.get_error:
            raise self.get_error
        for record in self.records:
            if record["LOAD_ID"] == load_id:
                return dict(record)
        return None

    async def book_load(self, load_id, mc_number, agreed_rate):
        if self.book_error:
            raise self.book_error
        self.booked.append((load_id, mc_number, agreed_rate))
        return {
            "LOAD_ID": load_id,
            "BOOKING_REF": "69HXG4EOKE1R91QC",
            "STATUS": "PENDING",
            "TIMESTAMP": "20260908054859",
        }

    async def echo(self, message="PING"):
        return {"ECHO": "", "AUTH": "OK", "FIELDS_PARSED": "3", "MSG": message}


class FakeFmcsa:
    def __init__(self) -> None:
        self.authorised = True
        self.error: Exception | None = None

    async def verify(self, mc_number):
        if self.error:
            raise self.error
        if self.authorised:
            return CarrierAuthority(
                mc_number="123456", found=True, authorised=True,
                legal_name="RELIABLE FREIGHT LLC", dot_number="987654",
                city="Joliet", state="IL", status_code="A",
                allowed_to_operate="Y", common_authority="A", contract_authority="A",
                power_units=12, drivers=14,
            )
        return CarrierAuthority(
            mc_number="999999", found=True, authorised=False,
            legal_name="LAPSED HAULERS INC", dot_number="111111",
            status_code="I", allowed_to_operate="N", common_authority="N",
            contract_authority="I",
            reasons=["FMCSA does not list this carrier as allowed to operate",
                     "No active common or contract for-hire authority on file"],
        )


@pytest.fixture
def tms() -> FakeTms:
    return FakeTms()


@pytest.fixture
def fmcsa() -> FakeFmcsa:
    return FakeFmcsa()


@pytest.fixture
def client(tms, fmcsa):
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as test_client:
        app.state.tms = tms
        app.state.fmcsa = fmcsa
        # The lifespan already built a board bound to the real TmsClient and
        # started its sweep. Rebind it to the fake and stop that task, or the
        # suite would reach for the network.
        app.state.board_task.cancel()
        app.state.board = BoardIndex(tms, get_settings())
        yield test_client


@pytest.fixture
def verified_call(client):
    """A call that has passed authority and OTP, ready for load matching."""
    call_id = client.post("/v1/calls/start", json={}, headers=AUTH).json()["call_id"]
    client.post("/v1/carriers/verify",
                json={"call_id": call_id, "mc_number": "MC-123456"}, headers=AUTH)
    client.post("/v1/identity/otp/send",
                json={"call_id": call_id, "destination": "dispatch@reliable.com"},
                headers=AUTH)
    code = _current_code(client, call_id)
    client.post("/v1/identity/otp/verify",
                json={"call_id": call_id, "code": code}, headers=AUTH)
    return call_id


@pytest.fixture
def board_call(client, tms, verified_call):
    """A verified call against the realistic board, with a warm snapshot.

    Opt in to this when the test is about finding loads. The default single-record
    fake stays in place for everything else.
    """
    tms.records = [dict(r) for r in BOARD_RECORDS]
    client.app.state.board.prime(
        BoardSnapshot.from_records([dict(r) for r in tms.records], get_settings())
    )
    return verified_call


def _current_code(client, call_id: str) -> str:
    session = client.app.state.sessions.get(call_id)
    return session.otp.code


@pytest.fixture
def current_code():
    return _current_code
