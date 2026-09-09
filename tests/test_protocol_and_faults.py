"""Codec correctness plus the four documented fault shapes.

The fault tests run against a real socket server that misbehaves on purpose, so
the adapter is exercised through the same code path production uses rather than
against a mock of itself.
"""

from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.tms import protocol
from app.tms.client import TmsClient
from app.tms.errors import TmsBookingUncertain, TmsCommandError, TmsUnavailable

RECORD = (
    "LOAD_ID:LD00720     |ORIG_CITY:Springfield                   |ORIG_STATE:IL|"
    "EQTYPE:FLATBED   |RATE:2534    |MILES:944   |STATUS:OPEN    "
)


# ----------------------------------------------------------------- codec

def test_encode_puts_cmd_and_auth_first():
    frame = protocol.encode_request("LOAD_QUERY", "tok", {"orig_state": "IL"})
    assert frame == b"CMD:LOAD_QUERY|AUTH:tok|ORIG_STATE:IL\r\n"


def test_encode_rejects_a_delimiter_in_a_value():
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_request("LOAD_QUERY", "tok", {"city": "Chi|cago"})


def test_encode_rejects_an_oversized_frame():
    with pytest.raises(protocol.ProtocolError):
        protocol.encode_request("LOAD_QUERY", "tok", {"notes": "x" * 5000})


def test_parse_strips_fixed_width_padding():
    record = protocol.parse_record(RECORD)
    assert record["LOAD_ID"] == "LD00720"
    assert record["ORIG_CITY"] == "Springfield"
    assert record["RATE"] == "2534"


def test_parse_error_line():
    code, message = protocol.parse_error("ERR|CODE:NOT_FOUND|MSG:Load not found")
    assert code == "NOT_FOUND"
    assert message == "Load not found"


# ------------------------------------------------------------ fault server

class FaultServer:
    """A TMS that behaves badly in exactly the four documented ways."""

    def __init__(self, behaviour: str, payloads: list[str] | None = None) -> None:
        self.behaviour = behaviour
        self.payloads = payloads or []
        self.requests: list[str] = []
        self._server = None
        self.port = 0

    async def __aenter__(self):
        self._server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader, writer):
        raw = await reader.readline()
        self.requests.append(raw.decode())
        attempt = len(self.requests)
        behaviour = self.behaviour

        if behaviour == "sequence":
            behaviour = self.payloads[min(attempt, len(self.payloads)) - 1]

        if behaviour == "timeout":
            await asyncio.sleep(30)
        elif behaviour == "partial":
            writer.write(f"{RECORD[:60]}".encode())
            await writer.drain()
        elif behaviour == "malformed":
            writer.write(b"LOAD_ID\x00broken|no-separator-here\r\nEND\r\n")
            await writer.drain()
        elif behaviour == "delayed_close":
            writer.write(f"{RECORD}\r\nEND\r\n".encode())
            await writer.drain()
            await asyncio.sleep(20)  # holds the socket open after a good response
        elif behaviour == "ok":
            writer.write(f"{RECORD}\r\nEND\r\n".encode())
            await writer.drain()
        elif behaviour == "already_booked":
            writer.write(b"ERR|CODE:ALREADY_BOOKED|MSG:Already booked by this token\r\n")
            await writer.drain()
        elif behaviour == "server_error":
            writer.write(b"ERR|CODE:SERVER_ERROR|MSG:transient\r\n")
            await writer.drain()
        writer.close()


def settings_for(port: int, retries: int = 3, timeout: float = 1.0) -> Settings:
    return Settings(
        tms_host="127.0.0.1", tms_port=port, tms_token="tok",
        tms_timeout_seconds=timeout, tms_max_retries=retries,
        api_auth_token="t",
    )


@pytest.mark.asyncio
async def test_clean_response_parses():
    async with FaultServer("ok") as server:
        client = TmsClient(settings_for(server.port))
        records = await client.query_loads({"ORIG_STATE": "IL"})
    assert records[0]["LOAD_ID"] == "LD00720"


@pytest.mark.asyncio
async def test_timeout_is_retried_then_reported_as_unavailable():
    async with FaultServer("timeout") as server:
        client = TmsClient(settings_for(server.port, retries=2, timeout=0.3))
        with pytest.raises(TmsUnavailable):
            await client.query_loads({"ORIG_STATE": "IL"})
    assert len(server.requests) == 2
    assert client.stats.timeouts == 2


@pytest.mark.asyncio
async def test_partial_response_is_not_mistaken_for_a_short_result():
    async with FaultServer("partial") as server:
        client = TmsClient(settings_for(server.port, retries=1))
        with pytest.raises(TmsUnavailable):
            await client.query_loads({"ORIG_STATE": "IL"})
    assert client.stats.partials == 1


@pytest.mark.asyncio
async def test_malformed_response_is_rejected():
    async with FaultServer("malformed") as server:
        client = TmsClient(settings_for(server.port, retries=1))
        with pytest.raises(TmsUnavailable):
            await client.query_loads({"ORIG_STATE": "IL"})


@pytest.mark.asyncio
async def test_delayed_termination_returns_as_soon_as_end_arrives():
    """A held-open socket must not stall the call waiting for close."""
    async with FaultServer("delayed_close") as server:
        client = TmsClient(settings_for(server.port, timeout=5.0))
        records = await asyncio.wait_for(
            client.query_loads({"ORIG_STATE": "IL"}), timeout=3.0
        )
    assert records[0]["LOAD_ID"] == "LD00720"


@pytest.mark.asyncio
async def test_a_transient_fault_recovers_on_retry():
    async with FaultServer("sequence", ["timeout", "ok"]) as server:
        client = TmsClient(settings_for(server.port, retries=3, timeout=0.4))
        records = await client.query_loads({"ORIG_STATE": "IL"})
    assert records[0]["LOAD_ID"] == "LD00720"
    assert client.stats.retries == 1


@pytest.mark.asyncio
async def test_business_errors_are_not_retried():
    async with FaultServer("already_booked") as server:
        client = TmsClient(settings_for(server.port, retries=3))
        with pytest.raises(TmsCommandError) as excinfo:
            await client.query_loads({"ORIG_STATE": "IL"})
    assert excinfo.value.code == "ALREADY_BOOKED"
    assert len(server.requests) == 1


@pytest.mark.asyncio
async def test_unknown_error_codes_are_treated_as_transient():
    async with FaultServer("server_error") as server:
        client = TmsClient(settings_for(server.port, retries=2))
        with pytest.raises(TmsUnavailable):
            await client.query_loads({"ORIG_STATE": "IL"})
    assert len(server.requests) == 2


@pytest.mark.asyncio
async def test_booking_is_never_blind_retried():
    """A timed-out booking may have landed. Retrying could double-book."""
    async with FaultServer("timeout") as server:
        client = TmsClient(settings_for(server.port, retries=3, timeout=0.3))
        with pytest.raises(TmsBookingUncertain):
            await client.book_load("LD00720", "123456", 2000)
    assert len(server.requests) == 1
