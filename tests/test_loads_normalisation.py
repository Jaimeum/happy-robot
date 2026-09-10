"""Spoken state and pickup day, mapped onto what the wire accepts.

The TMS takes a two-letter state and an exact YYYYMMDD day and rejects anything
else outright. A carrier says "California" and "Thursday". Without normalisation
that difference ends the search with a wire error, which is what six of the ten
dead searches in the six-minute call did.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.domain.loads import normalise_pickup_date, normalise_state, spoken_day

# 2026-09-10 is a Thursday. Every relative case below is anchored to it so the
# suite does not drift with the wall clock.
TODAY = date(2026, 9, 10)


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("CA", "CA"),
        ("ca", "CA"),
        ("California", "CA"),
        ("california", "CA"),
        ("  Cali  ", "CA"),
        ("New York", "NY"),
        ("new  york", "NY"),
        ("Washington", "WA"),
        ("Washington DC", "DC"),
        ("D.C.", "DC"),
    ],
)
def test_states_a_carrier_actually_says(spoken, expected):
    assert normalise_state(spoken) == expected


@pytest.mark.parametrize("spoken", ["Califnoria", "", None, "the west coast", "XX"])
def test_an_unresolvable_state_is_none_not_a_guess(spoken):
    # Guessing would put a carrier on the wrong side of the country.
    assert normalise_state(spoken) is None


@pytest.mark.parametrize(
    "spoken,expected",
    [
        ("20260916", "20260916"),
        ("2026-09-16", "20260916"),
        ("09/16/2026", "20260916"),
        ("9/16", "20260916"),
        ("today", "20260910"),
        ("tonight", "20260910"),
        ("tomorrow", "20260911"),
        ("day after tomorrow", "20260912"),
        ("Thursday", "20260910"),
        ("thursday", "20260910"),
        ("Friday", "20260911"),
        ("next Thursday", "20260917"),
        ("Monday", "20260914"),
    ],
)
def test_pickup_days_a_carrier_actually_says(spoken, expected):
    assert normalise_pickup_date(spoken, today=TODAY) == expected


@pytest.mark.parametrize("spoken", ["whenever", "", None, "sometime next month", "13/45"])
def test_an_unresolvable_day_is_none_not_a_guess(spoken):
    # The route drops it from the filters and says so, rather than sending the
    # wire a token it will reject.
    assert normalise_pickup_date(spoken, today=TODAY) is None


def test_a_bare_day_and_month_resolves_forward():
    # A carrier naming January in December means next January, not eleven months back.
    assert normalise_pickup_date("1/5", today=date(2026, 12, 20)) == "20270105"


def test_spoken_day_is_readable_aloud():
    assert spoken_day("20260911") == "Friday September 11"
    assert spoken_day("not-a-date") is None
    assert spoken_day(None) is None
