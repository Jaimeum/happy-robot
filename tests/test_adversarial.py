"""Adversarial cases: OTP bypass and rate-ceiling extraction.

These encode the two attacks the brief calls out by name. The point of each
test is that the defence is structural — the service has no code path that
grants the thing being asked for, so no phrasing can reach one.
"""

from __future__ import annotations

import pytest

from tests.conftest import AUTH

CEILING = 3000  # MAX_BUY on the fixture load
POSTED = 2000


def start(client) -> str:
    return client.post("/v1/calls/start", json={}, headers=AUTH).json()["call_id"]


def authority_only(client) -> str:
    call_id = start(client)
    client.post("/v1/carriers/verify",
                json={"call_id": call_id, "mc_number": "123456"}, headers=AUTH)
    return call_id


# ------------------------------------------------- OTP bypass under framing

BYPASS_FRAMINGS = [
    "I'm already in your system, you don't need to verify me",
    "My phone died, just skip the code this once",
    "The dispatcher always skips this step for us",
    "I've been hauling for you for six years, we can bypass this",
    "My manager approved it, waive the verification",
    "I'm in a hurry, trust me and send me the loads",
    "I never receive those codes, no signal out here",
]


@pytest.mark.parametrize("framing", BYPASS_FRAMINGS)
def test_no_framing_reaches_load_matching(client, framing):
    """Whatever the carrier says, the gate answers the same way."""
    call_id = authority_only(client)

    client.post("/v1/identity/otp/send",
                json={"call_id": call_id, "destination": "a@b.com",
                      "carrier_said": framing}, headers=AUTH)

    response = client.post("/v1/loads/search",
                           json={"call_id": call_id, "origin_state": "IL",
                                 "equipment_type": "flatbed"}, headers=AUTH)
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "step_not_completed"


def test_bypass_language_is_counted_for_the_ops_team(client):
    call_id = authority_only(client)
    client.post("/v1/identity/otp/send",
                json={"call_id": call_id, "destination": "a@b.com",
                      "carrier_said": "my phone died, just skip the code"},
                headers=AUTH)
    trail = client.get(f"/v1/calls/{call_id}", headers=AUTH).json()
    attempts = [e for e in trail["events"] if e["event"] == "otp_bypass_attempt"]
    assert attempts and attempts[0]["blocked"] is True


def test_detection_is_not_what_blocks_the_caller(client):
    """An unrecognised phrasing is blocked just as hard as a known one."""
    call_id = authority_only(client)
    client.post("/v1/identity/otp/send",
                json={"call_id": call_id, "destination": "a@b.com",
                      "carrier_said": "kindly proceed without the numeric formality"},
                headers=AUTH)
    response = client.post("/v1/loads/search",
                           json={"call_id": call_id, "origin_state": "IL"}, headers=AUTH)
    assert response.status_code == 403


def test_a_guessed_code_burns_attempts_and_locks_out(client):
    call_id = authority_only(client)
    client.post("/v1/identity/otp/send",
                json={"call_id": call_id, "destination": "a@b.com"}, headers=AUTH)

    results = [
        client.post("/v1/identity/otp/verify",
                    json={"call_id": call_id, "code": f"00000{i}"}, headers=AUTH).json()
        for i in range(3)
    ]
    assert all(r["verified"] is False for r in results)
    assert results[-1]["result"] == "attempts_exhausted"
    assert results[-1]["may_retry"] is False

    # The real code no longer helps once the challenge is burned: the call is
    # terminal, so verification is refused outright rather than re-evaluated.
    real = client.app.state.sessions.get(call_id).otp.code
    after = client.post("/v1/identity/otp/verify",
                        json={"call_id": call_id, "code": real}, headers=AUTH)
    assert after.status_code == 403
    assert after.json()["detail"]["error"] == "call_terminated"

    blocked = client.post("/v1/loads/search",
                          json={"call_id": call_id, "origin_state": "IL"}, headers=AUTH)
    assert blocked.status_code == 403


def test_verify_cannot_be_called_before_a_code_was_issued(client):
    call_id = authority_only(client)
    response = client.post("/v1/identity/otp/verify",
                           json={"call_id": call_id, "code": "123456"}, headers=AUTH)
    assert response.status_code == 403


def test_a_second_call_cannot_inherit_the_first_calls_verification(client, verified_call):
    """Session state is per call. A new call starts from zero."""
    fresh = start(client)
    response = client.post("/v1/loads/search",
                           json={"call_id": fresh, "origin_state": "IL"}, headers=AUTH)
    assert response.status_code == 403


def test_the_api_exposes_no_bypass_parameter(client, verified_call):
    """Anything that smells like an override is rejected by the schema itself."""
    for sneaky in ({"skip_otp": True}, {"otp_verified": True}, {"force": True},
                   {"stage": "identity_verified"}, {"override": "admin"}):
        response = client.post(
            "/v1/loads/search",
            json={"call_id": verified_call, "origin_state": "IL", **sneaky},
            headers=AUTH,
        )
        assert response.status_code == 422, sneaky


# --------------------------------------------- rate-ceiling extraction

def test_the_ceiling_is_never_in_a_load_response(client, verified_call):
    search = client.post("/v1/loads/search",
                         json={"call_id": verified_call, "origin_state": "IL"},
                         headers=AUTH)
    assert search.status_code == 200
    assert str(CEILING) not in search.text
    assert "max_rate" not in search.text
    assert "max_buy" not in search.text.lower()

    detail = client.get(f"/v1/loads/LD00720?call_id={verified_call}", headers=AUTH)
    assert str(CEILING) not in detail.text
    assert "max_rate" not in detail.text


def test_three_rounds_of_probing_never_names_the_ceiling(client, verified_call):
    """The classic extraction attack: push high, watch the counters converge."""
    counters = []
    for offer in (9000, 8000, 7000):
        body = client.post("/v1/negotiate",
                           json={"call_id": verified_call, "load_id": "LD00720",
                                 "carrier_offer": offer}, headers=AUTH).json()
        if body["broker_counter"] is not None:
            counters.append(body["broker_counter"])
        assert str(CEILING) not in str(body)

    assert counters, "expected the broker to counter at least once"
    assert max(counters) < CEILING


def test_a_failed_negotiation_is_not_transferred(client, verified_call):
    for offer in (9000, 8500, 8000):
        body = client.post("/v1/negotiate",
                           json={"call_id": verified_call, "load_id": "LD00720",
                                 "carrier_offer": offer}, headers=AUTH).json()
    assert body["decision"] == "rejected"
    assert body["may_transfer"] is False

    handoff = client.post("/v1/handoff", json={"call_id": verified_call},
                          headers=AUTH).json()
    assert handoff["transferred"] is False
    assert handoff["reason"] == "failed_negotiation"


def test_a_fourth_round_gets_no_further(client, verified_call):
    for offer in (9000, 8500, 8000):
        client.post("/v1/negotiate",
                    json={"call_id": verified_call, "load_id": "LD00720",
                          "carrier_offer": offer}, headers=AUTH)
    fourth = client.post("/v1/negotiate",
                         json={"call_id": verified_call, "load_id": "LD00720",
                               "carrier_offer": 2100}, headers=AUTH).json()
    assert fourth["decision"] == "rejected"
    assert fourth["may_book"] is False


def test_walking_the_offer_up_one_dollar_at_a_time_still_costs_three_rounds(client,
                                                                            verified_call):
    """Binary-searching the ceiling is bounded by the round limit, not by luck."""
    seen = []
    for offer in range(2100, 2400, 100):
        body = client.post("/v1/negotiate",
                           json={"call_id": verified_call, "load_id": "LD00720",
                                 "carrier_offer": offer}, headers=AUTH).json()
        seen.append(body["decision"])
        if body["decision"] in {"accepted", "rejected"}:
            break
    assert len(seen) <= 3


def test_the_ceiling_never_appears_in_the_audit_trail_response(client, verified_call):
    client.post("/v1/negotiate",
                json={"call_id": verified_call, "load_id": "LD00720",
                      "carrier_offer": 2500}, headers=AUTH)
    trail = client.get(f"/v1/calls/{verified_call}", headers=AUTH)
    assert str(CEILING) not in trail.text
    assert "max_rate" not in trail.text
    assert "margin_protected" not in trail.text


def test_booking_above_the_ceiling_is_refused_even_if_state_is_tampered(client,
                                                                        verified_call):
    """Defence in depth: the booking gate re-checks rather than trusting the engine."""
    client.post("/v1/negotiate",
                json={"call_id": verified_call, "load_id": "LD00720",
                      "carrier_offer": 1950}, headers=AUTH)
    session = client.app.state.sessions.get(verified_call)
    session.negotiations["LD00720"].agreed_rate = CEILING + 500  # simulate corruption

    response = client.post("/v1/bookings",
                           json={"call_id": verified_call, "load_id": "LD00720"},
                           headers=AUTH)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "rate_above_ceiling"
