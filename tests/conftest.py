from __future__ import annotations

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
from app.integrations.fmcsa import CarrierAuthority  # noqa: E402
from app.main import create_app  # noqa: E402
from app.tms.client import TmsCallStats  # noqa: E402
from app.tms.errors import TmsCommandError, TmsUnavailable  # noqa: E402

AUTH = {"Authorization": "Bearer test-token"}

# A representative record in the exact shape and padding the live server emits.
LOAD_RECORD = {
    "LOAD_ID": "LD00720",
    "ORIG_CITY": "Springfield",
    "ORIG_STATE": "IL",
    "ORIG_ZIP": "62701",
    "DEST_CITY": "Worcester",
    "DEST_STATE": "MA",
    "DEST_ZIP": "01608",
    "PICKUP_DT": "20260909071700",
    "DELIVERY_DT": "20260910181700",
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


class FakeTms:
    """Stands in for the socket adapter so API tests stay deterministic."""

    def __init__(self) -> None:
        self.stats = TmsCallStats()
        self.records = [dict(LOAD_RECORD)]
        self.query_error: Exception | None = None
        self.get_error: Exception | None = None
        self.book_error: Exception | None = None
        self.booked: list[tuple[str, str, int]] = []

    async def query_loads(self, filters):
        if self.query_error:
            raise self.query_error
        return list(self.records)

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


def _current_code(client, call_id: str) -> str:
    session = client.app.state.sessions.get(call_id)
    return session.otp.code


@pytest.fixture
def current_code():
    return _current_code
