"""Validation failures must still tell the agent what to do.

The webhook nodes on the platform send every tool parameter as a JSON string,
so a detail the agent did not capture arrives as "" rather than absent. That is
a validation error, and FastAPI's default 422 is a raw Pydantic dump. An agent
handed that blob has no instruction and nothing to say to the carrier.
"""

from __future__ import annotations

from tests.conftest import AUTH


def start(client) -> str:
    return client.post("/v1/calls/start", json={}, headers=AUTH).json()["call_id"]


def test_blank_required_field_returns_guidance_not_a_pydantic_dump(client):
    call_id = start(client)

    response = client.post("/v1/carriers/verify",
                           json={"call_id": call_id, "mc_number": ""}, headers=AUTH)

    assert response.status_code == 422
    body = response.json()
    assert body["error"] == "invalid_parameters"
    assert body["fields"] == ["mc_number"]
    assert "MC number" in body["agent_guidance"]
    # The default FastAPI shape must be gone, or the agent gets the blob again.
    assert "detail" not in body


def test_guidance_names_every_missing_field(client):
    call_id = start(client)

    response = client.post("/v1/negotiate",
                           json={"call_id": call_id, "load_id": "",
                                 "carrier_offer": ""}, headers=AUTH)

    assert response.status_code == 422
    body = response.json()
    assert set(body["fields"]) == {"load_id", "carrier_offer"}
    guidance = body["agent_guidance"]
    assert "which load" in guidance
    assert "plain number" in guidance
    assert "one question at a time" in guidance


def test_missing_call_id_is_an_internal_recovery_not_a_carrier_question(client):
    # The carrier cannot answer this one — the agent lost the id from start_call.
    response = client.post("/v1/loads/detail",
                           json={"call_id": "", "load_id": "LD00730"}, headers=AUTH)

    assert response.status_code == 422
    guidance = response.json()["agent_guidance"]
    assert "start_call" in guidance
    assert "Do not mention this to the carrier" in guidance


def test_unmapped_field_still_gets_a_usable_instruction(client):
    call_id = start(client)

    # carrier_said has no entry in the repair table; the fallback must still be
    # an instruction rather than a validation dump.
    response = client.post("/v1/negotiate",
                           json={"call_id": call_id, "load_id": "LD00730",
                                 "carrier_offer": 2500, "carrier_said": "x" * 2001},
                           headers=AUTH)

    assert response.status_code == 422
    body = response.json()
    assert body["fields"] == ["carrier_said"]
    assert "repeat the last detail" in body["agent_guidance"]


def test_validation_errors_never_carry_the_rate_ceiling(client):
    # The leak guard inspects outgoing JSON; the handler must not smuggle a
    # numeric echo of the input past it in an error payload.
    call_id = start(client)

    response = client.post("/v1/negotiate",
                           json={"call_id": call_id, "load_id": "", "carrier_offer": ""},
                           headers=AUTH)

    assert response.status_code == 422
    assert "max_rate" not in response.text
