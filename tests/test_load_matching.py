"""Load matching that cannot dead-end.

The call these tests exist for: a carrier with a dry van in San Diego. The agent
made ten searches over six minutes, every one returning zero, because the bridge
answered only the exact question asked and then told it to "ask if they are
flexible on destination or pickup day" — an instruction to guess again.

On the real board there is exactly one dry van out of California, and it is in
San Jose. It sat one dropped filter away the whole time.
"""

from __future__ import annotations

from tests.conftest import AUTH


def search(client, call_id, **kwargs):
    return client.post(
        "/v1/loads/search", json={"call_id": call_id, **kwargs}, headers=AUTH
    ).json()


# ------------------------------------------------------- the six-minute call

def test_the_san_diego_dry_van_finds_the_san_jose_load(client, board_call):
    body = search(client, board_call, origin_city="San Diego", origin_state="CA",
                  destination_state="UT", equipment_type="dry van",
                  pickup_date="tomorrow")

    assert body["match_count"] >= 1
    assert [l["load_id"] for l in body["loads"]][0] == "LD00760"
    assert body["search_basis"] == "widened"
    # The filters that had to go, in the order they went.
    assert body["relaxed"] == ["pickup_date", "destination_state", "origin_city"]
    assert body["rung"] == "origin_state"


def test_the_concession_is_a_sentence_the_agent_can_read_out(client, board_call):
    body = search(client, board_call, origin_city="San Diego", origin_state="CA",
                  destination_state="UT", equipment_type="dry van",
                  pickup_date="tomorrow")

    concession = body["spoken_concession"]
    # Ship the sentence, not the inference: honesty must not depend on the model
    # re-deriving which filter was dropped.
    assert concession.startswith("Nothing picking up ")
    assert "nothing going to UT" in concession
    assert "nothing out of San Diego itself" in concession
    assert concession.endswith(".")
    assert "San Jose" in body["agent_guidance"]


def test_equipment_is_never_relaxed(client, board_call):
    # There IS a San Diego load — LD00784 — but it is a step deck. A carrier with
    # a van must never be offered it, however far the ladder relaxes.
    body = search(client, board_call, origin_city="San Diego", origin_state="CA",
                  equipment_type="dry van")

    assert "LD00784" not in [l["load_id"] for l in body["loads"]]
    assert "origin_city" in body["relaxed"]


def test_a_state_with_freight_but_not_their_trailer_is_stated_as_a_fact(client, board_call):
    body = search(client, board_call, origin_state="CA", equipment_type="power only")

    # California has loads, none of them power only. Say so rather than relaxing
    # equipment or claiming the state is empty.
    assert body["equipment_note"] is not None
    assert "CA has" in body["equipment_note"]
    assert "power only" in body["equipment_note"].lower()


# ------------------------------------------------------- only what exists

def test_a_load_whose_pickup_already_passed_is_never_offered(client, board_call):
    # LD00719 is a dry van, and on an equipment-only search it sorts first by
    # rate. Its pickup was 03:03 this morning and it is PENDING besides.
    body = search(client, board_call, equipment_type="dry van")

    assert "LD00719" not in [l["load_id"] for l in body["loads"]]


def test_board_facts_never_contains_a_zero(client, board_call):
    facts = search(client, board_call, origin_state="CA",
                   equipment_type="dry van")["board_facts"]

    assert facts["origin_states"], "the menu must not be empty when freight exists"
    for row in facts["origin_states"]:
        assert row["load_count"] >= 1
        assert row["cities"]
    for row in facts["equipment_breakdown"]:
        assert row["load_count"] >= 1
    # Nothing in the payload the agent could offer that is not really there.
    assert facts["loads_offerable"] <= facts["loads_indexed"]


def test_nothing_to_search_on_returns_the_live_menu_instead_of_an_error(client, board_call):
    body = search(client, board_call)  # no equipment, no lane

    guidance = body["agent_guidance"]
    assert "Ask what they pull" in guidance
    # Real counts off the board, not an invented list of options.
    assert "Dry Van" in guidance
    assert body["may_search_again"] is True


def test_match_count_is_a_count_not_a_page_size(client, board_call):
    body = search(client, board_call, equipment_type="dry van", limit=2)

    assert body["returned_count"] == 2
    assert body["match_count"] > 2, "match_count must report what matched, not what fitted"


# ------------------------------------------------------- the loop breaker

def test_an_identical_search_is_answered_without_touching_the_tms(client, board_call, tms):
    first = search(client, board_call, origin_state="TX", equipment_type="dry van")
    before = len(tms.queries)

    second = search(client, board_call, origin_state="TX", equipment_type="dry van")

    assert len(tms.queries) == before, "a repeat must not reach the wire"
    assert second["search_basis"] == "repeat"
    assert second["repeat_of_search"] == 1
    assert second["agent_guidance"].startswith("You already ran this exact search")
    assert [l["load_id"] for l in second["loads"]] == [l["load_id"] for l in first["loads"]]


def test_a_state_spoken_in_full_is_understood(client, board_call):
    body = search(client, board_call, origin_state="California", equipment_type="dry van")

    # max_length was 2, so "California" used to be a 422 telling the agent to
    # re-ask a question the carrier had already answered.
    assert body["match_count"] >= 1
    assert [l["load_id"] for l in body["loads"]] == ["LD00760"]


def test_the_search_budget_hands_over_the_whole_option_set(client, board_call):
    # Four distinct searches, then the cap.
    for state in ("TX", "NY", "MI", "GA"):
        search(client, board_call, origin_state=state, equipment_type="dry van")

    body = search(client, board_call, origin_state="UT", equipment_type="dry van")

    assert body["search_basis"] == "budget_exhausted"
    assert body["may_search_again"] is False
    assert body["searches_remaining"] == 0
    # A hard cap is only safe because it hands over everything rather than refusing.
    assert body["loads"], "the cap must return the whole option set, not an empty list"
    assert "Stop searching" in body["agent_guidance"]


def test_every_search_reports_its_budget(client, board_call):
    body = search(client, board_call, origin_state="TX", equipment_type="dry van")

    assert body["searches_used"] == 1
    assert body["searches_remaining"] == 3
    assert body["may_search_again"] is True


# ------------------------------------------------------- nothing anywhere

def test_nothing_for_that_equipment_anywhere_says_so_plainly(client, board_call, tms):
    from app.config import get_settings
    from app.domain.board import BoardSnapshot

    # A board with no power-only freight at all.
    records = [r for r in tms.records if r["EQTYPE"] != "POWER_ONLY"]
    tms.records = records
    client.app.state.board.prime(BoardSnapshot.from_records(records, get_settings()))

    body = search(client, board_call, origin_state="TX", equipment_type="power only")

    assert body["exhausted"] is True
    guidance = body["agent_guidance"].lower()
    assert "zero nationwide" in guidance
    # Assert the instruction, not the absence of a word: the guidance names
    # flexibility precisely in order to forbid asking for it, because asking
    # cannot change the answer when the board holds none of that equipment.
    assert "do not ask them to be flexible" in guidance
    assert "do not promise a callback" in guidance
    assert "do not search again" in guidance
