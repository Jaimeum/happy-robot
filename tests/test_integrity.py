"""Four holes a widened search makes reachable, two of them hard-rule breaches.

Every one of these was reproduced against the running service before being
fixed. They are grouped here because they are compliance, not polish: the brief
says a failed call is never transferred and that the OTP gate must not be
bypassable under any framing, and two of these broke exactly that.
"""

from __future__ import annotations

from tests.conftest import AUTH


def _start(client) -> str:
    return client.post("/v1/calls/start", json={}, headers=AUTH).json()["call_id"]


# ------------------------------------- a failed call is never transferred

def test_handoff_is_refused_after_an_authority_rejection(client, fmcsa):
    # terminate() sets stage to CLOSED, which is the HIGHEST stage value, so
    # handoff's "stage < BOOKED" ordering check passed for a rejected call.
    # Live, this returned transferred=True with a place in the senior-rep queue.
    fmcsa.authorised = False
    call_id = _start(client)
    client.post("/v1/carriers/verify",
                json={"call_id": call_id, "mc_number": "MC-999999"}, headers=AUTH)

    body = client.post("/v1/handoff", json={"call_id": call_id}, headers=AUTH).json()

    assert body["transferred"] is False
    assert body["reason"] == "call_closed"
    assert "no transfer" in body["agent_guidance"].lower()


def test_a_rejected_call_keeps_the_outcome_it_earned(client, fmcsa):
    # The audit trail must not show a transfer where the brokerage actually
    # turned the carrier away.
    fmcsa.authorised = False
    call_id = _start(client)
    client.post("/v1/carriers/verify",
                json={"call_id": call_id, "mc_number": "MC-999999"}, headers=AUTH)
    client.post("/v1/handoff", json={"call_id": call_id}, headers=AUTH)

    assert client.app.state.sessions.get(call_id).outcome == "rejected_authority"


# ------------------------------------- the identity gate cannot be re-pointed

def test_a_different_mc_after_the_code_is_refused(client, verified_call):
    # The stage machine only moves forward, so swapping mc_number after OTP left
    # the call identity_verified for a carrier who never passed a code. Live,
    # this booked as MC 777777 with no bypass phrasing at all.
    session = client.app.state.sessions.get(verified_call)
    original = session.mc_number

    response = client.post("/v1/carriers/verify",
                           json={"call_id": verified_call, "mc_number": "MC-777777"},
                           headers=AUTH)

    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "carrier_already_identified"
    assert session.mc_number == original, "the verified identity must not move"


def test_correcting_a_mis_heard_mc_before_the_code_is_allowed(client):
    # A mis-heard MC before the code is exactly what should be correctable.
    call_id = _start(client)
    client.post("/v1/carriers/verify",
                json={"call_id": call_id, "mc_number": "MC-123456"}, headers=AUTH)

    response = client.post("/v1/carriers/verify",
                           json={"call_id": call_id, "mc_number": "MC-654321"},
                           headers=AUTH)

    assert response.status_code == 200
    assert response.json()["verified"] is True


# ------------------------------------- margin does not leak to a repeated ask

def test_the_same_offer_twice_consumes_no_round_and_concedes_nothing(client, verified_call):
    # Live, three identical asks of 2600 returned 2300, then 2495, then accepted
    # 2600: the bridge negotiated against itself and burned the whole cap on a
    # carrier who said one number once.
    first = client.post("/v1/negotiate",
                        json={"call_id": verified_call, "load_id": "LD00720",
                              "carrier_offer": 2600}, headers=AUTH).json()
    second = client.post("/v1/negotiate",
                         json={"call_id": verified_call, "load_id": "LD00720",
                               "carrier_offer": 2600}, headers=AUTH).json()

    assert first["decision"] == "countered"
    assert second["replayed"] is True
    assert second["broker_counter"] == first["broker_counter"], "we must not improve our own offer"
    assert second["round"] == first["round"], "a repeat is not a new round"
    assert "do not improve it" in second["agent_guidance"]
    negotiation = client.app.state.sessions.get(verified_call).negotiations["LD00720"]
    assert negotiation.rounds_used == 1


def test_distinct_offers_still_terminate_at_three_rounds(client, verified_call):
    # Dedupe is exact-equality only, so walking the number up in small steps
    # must still hit the cap rather than looping forever.
    decisions = []
    for offer in (2600, 2601, 2602, 2603):
        decisions.append(
            client.post("/v1/negotiate",
                        json={"call_id": verified_call, "load_id": "LD00720",
                              "carrier_offer": offer}, headers=AUTH).json()["decision"]
        )

    negotiation = client.app.state.sessions.get(verified_call).negotiations["LD00720"]
    assert negotiation.rounds_used <= 3
    assert decisions[-1] in {"accepted", "rejected"}


# ------------------------------------- a booking is committed once

def test_booking_the_same_load_twice_returns_the_same_reference(client, verified_call, tms):
    client.post("/v1/negotiate",
                json={"call_id": verified_call, "load_id": "LD00720",
                      "carrier_offer": 1900}, headers=AUTH)
    first = client.post("/v1/bookings",
                        json={"call_id": verified_call, "load_id": "LD00720"},
                        headers=AUTH).json()

    second = client.post("/v1/bookings",
                         json={"call_id": verified_call, "load_id": "LD00720"},
                         headers=AUTH).json()

    assert first["booked"] is True
    assert second["booked"] is True
    assert second["booking_reference"] == first["booking_reference"]
    assert "do not book it a second time" in second["agent_guidance"]
    # The carrier who just heard their reference must never be told someone else
    # took the load, and the TMS must not be asked twice.
    assert len(tms.booked) == 1
