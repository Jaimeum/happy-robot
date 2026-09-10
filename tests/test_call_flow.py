"""End-to-end call scenarios through the HTTP surface."""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import AUTH
from app.tms.errors import TmsBookingUncertain, TmsCommandError, TmsUnavailable


def start(client) -> str:
    return client.post("/v1/calls/start", json={}, headers=AUTH).json()["call_id"]


def code_for(client, call_id) -> str:
    return client.app.state.sessions.get(call_id).otp.code


# ------------------------------------------------------------- happy path

def test_full_call_books_and_hands_off(client, tms):
    call_id = start(client)

    verify = client.post("/v1/carriers/verify",
                         json={"call_id": call_id, "mc_number": "MC-123456"},
                         headers=AUTH).json()
    assert verify["verified"] is True
    assert verify["carrier_name"] == "RELIABLE FREIGHT LLC"

    client.post("/v1/identity/otp/send",
                json={"call_id": call_id, "destination": "+1 815 555 0142",
                      "channel": "sms"}, headers=AUTH)
    otp = client.post("/v1/identity/otp/verify",
                      json={"call_id": call_id, "code": code_for(client, call_id)},
                      headers=AUTH).json()
    assert otp["verified"] is True

    search = client.post("/v1/loads/search",
                         json={"call_id": call_id, "origin_state": "IL",
                               "equipment_type": "flat bed"}, headers=AUTH).json()
    assert search["match_count"] == 1
    load_id = search["loads"][0]["load_id"]

    detail = client.get(f"/v1/loads/{load_id}?call_id={call_id}", headers=AUTH).json()
    assert detail["load"]["loadboard_rate"] == 2000

    deal = client.post("/v1/negotiate",
                       json={"call_id": call_id, "load_id": load_id,
                             "carrier_offer": 2400}, headers=AUTH).json()
    if deal["decision"] == "countered":
        deal = client.post("/v1/negotiate",
                           json={"call_id": call_id, "load_id": load_id,
                                 "carrier_offer": deal["broker_counter"]},
                           headers=AUTH).json()
    assert deal["decision"] == "accepted"

    booking = client.post("/v1/bookings",
                          json={"call_id": call_id, "load_id": load_id},
                          headers=AUTH).json()
    assert booking["booked"] is True
    assert booking["booking_reference"] == "69HXG4EOKE1R91QC"

    handoff = client.post("/v1/handoff", json={"call_id": call_id}, headers=AUTH).json()
    assert handoff["transferred"] is True
    assert handoff["handoff_reference"].startswith("HO-")

    trail = client.get(f"/v1/calls/{call_id}", headers=AUTH).json()
    events = [e["event"] for e in trail["events"]]
    assert "authority_checked" in events
    assert "otp_verified" in events
    assert "rate_agreed" in events
    assert "booking_confirmed" in events
    assert "handoff_queued" in events


# ------------------------------------------------------------------ auth

def test_every_route_requires_a_token(client):
    for method, path, body in [
        ("post", "/v1/calls/start", {}),
        ("get", "/health", None),
        ("post", "/v1/loads/search", {"call_id": "x"}),
        ("get", "/v1/ops/dashboard", None),
    ]:
        response = getattr(client, method)(path, json=body) if body is not None \
            else getattr(client, method)(path)
        assert response.status_code == 401, path


def test_liveness_is_the_only_open_route(client):
    assert client.get("/live").status_code == 204


def test_api_docs_are_not_served_in_production():
    """Swagger publishes the whole attack surface, so it is dev-only."""
    import os

    from app.config import get_settings
    from app.main import create_app

    previous = os.environ["ENV"]
    os.environ["ENV"] = "production"
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as prod:
            for path in ("/docs", "/openapi.json", "/redoc"):
                assert prod.get(path).status_code == 404, path
    finally:
        os.environ["ENV"] = previous
        get_settings.cache_clear()


def test_a_wrong_token_is_refused(client):
    response = client.get("/health", headers={"Authorization": "Bearer nope"})
    assert response.status_code == 401


# ----------------------------------------------------------------- gating

def test_load_search_is_blocked_before_authority(client):
    call_id = start(client)
    response = client.post("/v1/loads/search",
                           json={"call_id": call_id, "origin_state": "IL"}, headers=AUTH)
    assert response.status_code == 403
    assert response.json()["detail"]["error"] == "step_not_completed"


def test_load_search_is_blocked_between_authority_and_otp(client):
    call_id = start(client)
    client.post("/v1/carriers/verify",
                json={"call_id": call_id, "mc_number": "123456"}, headers=AUTH)
    response = client.post("/v1/loads/search",
                           json={"call_id": call_id, "origin_state": "IL"}, headers=AUTH)
    assert response.status_code == 403
    assert response.json()["detail"]["current_stage"] == "authority_verified"


def test_carrier_without_authority_is_turned_away(client, fmcsa):
    fmcsa.authorised = False
    call_id = start(client)
    result = client.post("/v1/carriers/verify",
                         json={"call_id": call_id, "mc_number": "999999"},
                         headers=AUTH).json()
    assert result["verified"] is False
    assert result["next_step"] == "end_call"

    blocked = client.post("/v1/identity/otp/send",
                          json={"call_id": call_id, "destination": "a@b.com"},
                          headers=AUTH)
    assert blocked.status_code == 403


def test_booking_requires_an_agreed_rate(client, verified_call):
    response = client.post("/v1/bookings",
                           json={"call_id": verified_call, "load_id": "LD00720"},
                           headers=AUTH)
    assert response.status_code == 403


# ------------------------------------------------------------ degradation

def test_search_degrades_gracefully_when_the_tms_is_down(client, verified_call, tms):
    tms.query_error = TmsUnavailable("timed out waiting for the TMS")
    result = client.post("/v1/loads/search",
                         json={"call_id": verified_call, "origin_state": "IL"},
                         headers=AUTH)
    assert result.status_code == 200
    body = result.json()
    assert body["degraded"] is True
    assert body["match_count"] == 0
    guidance = body["agent_guidance"].lower()
    # Keep the carrier on the line, retry once, and commit to nothing. The old
    # string promised "a rep will call back", which no endpoint can deliver.
    assert "briefly down" in guidance
    assert "once more" in guidance
    assert "do not promise a callback" in guidance


def test_uncertain_booking_is_escalated_not_retried(client, verified_call, tms):
    load_id = "LD00720"
    client.post("/v1/negotiate",
                json={"call_id": verified_call, "load_id": load_id,
                      "carrier_offer": 1950}, headers=AUTH)
    tms.book_error = TmsBookingUncertain("outcome unknown")
    body = client.post("/v1/bookings",
                       json={"call_id": verified_call, "load_id": load_id},
                       headers=AUTH).json()
    assert body["booked"] is False
    assert body["requires_manual_check"] is True
    assert "Do not retry" in body["agent_guidance"]


def test_load_taken_by_someone_else_is_handled(client, verified_call, tms):
    load_id = "LD00720"
    client.post("/v1/negotiate",
                json={"call_id": verified_call, "load_id": load_id,
                      "carrier_offer": 1950}, headers=AUTH)
    tms.book_error = TmsCommandError("Already booked", code="ALREADY_BOOKED")
    response = client.post("/v1/bookings",
                           json={"call_id": verified_call, "load_id": load_id},
                           headers=AUTH)
    assert response.status_code == 409
    assert response.json()["detail"]["error"] == "already_booked"

    # A definite rejection must not land on the manual-check queue, or that
    # counter stops being a signal the ops manager can act on.
    dashboard = client.get("/v1/ops/dashboard", headers=AUTH).json()
    assert dashboard["controls"]["bookings_needing_manual_check"] == 0
    assert dashboard["controls"]["loads_lost_to_another_carrier"] == 1


def test_an_uncertain_booking_does_land_on_the_manual_check_queue(client, verified_call, tms):
    load_id = "LD00720"
    client.post("/v1/negotiate",
                json={"call_id": verified_call, "load_id": load_id,
                      "carrier_offer": 1950}, headers=AUTH)
    tms.book_error = TmsBookingUncertain("outcome unknown")
    client.post("/v1/bookings",
                json={"call_id": verified_call, "load_id": load_id}, headers=AUTH)

    dashboard = client.get("/v1/ops/dashboard", headers=AUTH).json()
    assert dashboard["controls"]["bookings_needing_manual_check"] == 1
    assert dashboard["controls"]["loads_lost_to_another_carrier"] == 0


def test_no_matching_loads_never_invites_the_agent_to_guess(client, verified_call, tms):
    # The old guidance said "ask if they are flexible on destination or pickup
    # day", and an agent followed it into ten dead searches over six minutes.
    # With no board snapshot the honest move is to ask one open question, not to
    # send the agent cycling through destinations and dates.
    tms.records = []
    body = client.post("/v1/loads/search",
                       json={"call_id": verified_call, "origin_state": "WY"},
                       headers=AUTH).json()
    assert body["match_count"] == 0
    guidance = body["agent_guidance"].lower()
    assert "flexible" not in guidance
    assert "pickup day" not in guidance
    assert body["may_search_again"] is True


def test_unknown_equipment_is_rejected_with_a_usable_prompt(client, verified_call):
    response = client.post("/v1/loads/search",
                           json={"call_id": verified_call, "origin_state": "IL",
                                 "equipment_type": "spaceship"}, headers=AUTH)
    assert response.status_code == 400
    assert "dry van" in response.json()["detail"]["agent_guidance"]


def test_search_requires_at_least_one_filter(client, verified_call):
    response = client.post("/v1/loads/search", json={"call_id": verified_call},
                           headers=AUTH)
    assert response.status_code == 400
    assert response.json()["detail"]["error"] == "no_filters"


# ------------------------------------------------------------------- ops

def test_dashboard_reports_the_northstar_kpis(client, verified_call, tms):
    load_id = "LD00720"
    countered = client.post("/v1/negotiate",
                            json={"call_id": verified_call, "load_id": load_id,
                                  "carrier_offer": 2500}, headers=AUTH).json()
    agreed = client.post("/v1/negotiate",
                         json={"call_id": verified_call, "load_id": load_id,
                               "carrier_offer": countered["broker_counter"]},
                         headers=AUTH).json()
    client.post("/v1/bookings",
                json={"call_id": verified_call, "load_id": load_id}, headers=AUTH)

    body = client.get("/v1/ops/dashboard", headers=AUTH).json()
    kpis = body["northstar_kpis"]
    assert kpis["rate_ceiling_breaches"] == 0
    assert kpis["otp_bypasses"] == 0
    assert kpis["margin_protected_total"] == 3000 - agreed["agreed_rate"]
    assert body["funnel"]["bookings_confirmed"] == 1
