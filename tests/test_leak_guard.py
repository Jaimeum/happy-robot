"""The runtime leak guard, tested in isolation and through a live route."""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.security.leak_guard import (
    MaxRateLeakGuard,
    allow_value,
    find_leaks,
    protect_value,
    reset_protected_values,
)


def test_finds_a_ceiling_hidden_in_a_nested_field():
    leaks = find_leaks({"load": {"detail": {"cap": 3157}}}, frozenset({3157}))
    assert leaks == [("$.load.detail.cap", 3157)]


def test_finds_a_ceiling_embedded_in_free_text():
    leaks = find_leaks({"note": "I can go up to 3157 on this one"}, frozenset({3157}))
    assert leaks and leaks[0][1] == 3157


def test_finds_a_ceiling_written_with_a_thousands_separator():
    leaks = find_leaks({"note": "up to 3,157 dollars"}, frozenset({3157}))
    assert leaks and leaks[0][1] == 3157


def test_small_numbers_are_not_treated_as_secrets():
    reset_protected_values()
    protect_value(3)
    assert find_leaks({"rounds_remaining": 3}, frozenset()) == []


def test_booleans_are_not_mistaken_for_numbers():
    assert find_leaks({"ok": True}, frozenset({1})) == []


def test_a_carrier_named_number_is_exempt():
    reset_protected_values()
    protect_value(3000)
    allow_value(3000)
    from app.security.leak_guard import protected_values
    assert 3000 not in protected_values()


def _guarded_app(payload, secret=3157, disclosed=None):
    app = FastAPI()
    app.add_middleware(MaxRateLeakGuard)

    @app.get("/probe")
    async def probe():
        protect_value(secret)
        if disclosed is not None:
            allow_value(disclosed)
        return payload

    return TestClient(app)


def test_a_leaking_response_fails_closed():
    client = _guarded_app({"counter": 3157})
    response = client.get("/probe")
    assert response.status_code == 500
    assert response.json()["error"] == "response_blocked"


def test_a_clean_response_passes_through_untouched():
    client = _guarded_app({"counter": 2900, "load_id": "LD00720"})
    response = client.get("/probe")
    assert response.status_code == 200
    assert response.json() == {"counter": 2900, "load_id": "LD00720"}


def test_an_exempted_value_passes():
    client = _guarded_app({"agreed_rate": 3157}, disclosed=3157)
    assert client.get("/probe").status_code == 200
