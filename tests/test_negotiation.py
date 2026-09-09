"""The negotiation policy, exercised directly.

These are the rules the brokerage is paying for, so they are tested at the unit
level rather than only through the API.
"""

from __future__ import annotations

import pytest

from app.domain.negotiation import Decision, Negotiation, NegotiationStatus


def make(loadboard=2000, ceiling=3000, rounds=3):
    return Negotiation(
        load_id="LD00720", loadboard_rate=loadboard, max_rate=ceiling,
        max_rounds=rounds, ceiling_buffer_pct=0.03, auto_accept_pct=0.02,
    )


def test_accepts_when_carrier_asks_at_or_below_our_offer():
    n = make()
    outcome = n.evaluate(1900)
    assert outcome.decision is Decision.ACCEPTED
    assert outcome.agreed_rate == 1900
    assert outcome.may_book and outcome.may_transfer


def test_accepts_a_small_step_up_without_haggling():
    n = make()
    outcome = n.evaluate(2030)  # inside the 2% auto-accept band
    assert outcome.decision is Decision.ACCEPTED
    assert outcome.agreed_rate == 2030


def test_counters_rather_than_conceding_a_large_ask():
    n = make()
    outcome = n.evaluate(2900)
    assert outcome.decision is Decision.COUNTERED
    assert 2000 < outcome.broker_counter < 2900


def test_counter_never_reaches_the_ceiling():
    """The ceiling must not be discoverable by pushing hard on every round."""
    n = make(loadboard=2000, ceiling=3000)
    cap = n.counter_cap
    assert cap == 2910  # floor(3000 * 0.97)
    for offer in (5000, 5000, 5000):
        outcome = n.evaluate(offer)
        if outcome.broker_counter is not None:
            assert outcome.broker_counter <= cap
            assert outcome.broker_counter < n.max_rate


def test_three_rounds_then_no_deal_and_no_transfer():
    n = make()
    first = n.evaluate(4000)
    second = n.evaluate(3800)
    third = n.evaluate(3600)

    assert first.decision is Decision.COUNTERED
    assert second.decision is Decision.COUNTERED
    assert third.decision is Decision.REJECTED
    assert third.may_transfer is False
    assert third.may_book is False
    assert n.status is NegotiationStatus.FAILED
    assert n.rounds_used == 3


def test_fourth_round_is_refused_outright():
    n = make()
    for offer in (4000, 3800, 3600):
        n.evaluate(offer)
    fourth = n.evaluate(2100)
    assert fourth.decision is Decision.REJECTED
    assert fourth.may_book is False


def test_final_round_accepts_an_ask_inside_the_ceiling():
    n = make()
    n.evaluate(4000)
    n.evaluate(3500)
    final = n.evaluate(2800)
    assert final.decision is Decision.ACCEPTED
    assert final.agreed_rate == 2800
    assert final.may_transfer is True


def test_agreement_never_exceeds_the_ceiling():
    """Property check across the plausible offer space."""
    for ceiling in (1500, 3000, 7500):
        for offer in range(100, ceiling * 2, 137):
            n = make(loadboard=int(ceiling * 0.7), ceiling=ceiling)
            for _ in range(3):
                outcome = n.evaluate(offer)
                if outcome.broker_counter is not None:
                    assert outcome.broker_counter < ceiling
                if n.status is not NegotiationStatus.OPEN:
                    break
            if n.agreed_rate is not None:
                assert n.agreed_rate <= ceiling


def test_missing_ceiling_blocks_the_deal_rather_than_assuming_zero():
    """MAX_BUY is absent on unflagged tokens. Absent is not the same as zero."""
    n = Negotiation(load_id="LD1", loadboard_rate=2000, max_rate=None,
                    max_rounds=3, ceiling_buffer_pct=0.03, auto_accept_pct=0.02)
    outcome = n.evaluate(2100)
    assert outcome.decision is Decision.REJECTED
    assert "no rate ceiling" in outcome.reason


def test_margin_is_recorded_for_the_dashboard():
    n = make(loadboard=2000, ceiling=3000)
    countered = n.evaluate(2500)
    assert countered.decision is Decision.COUNTERED
    n.evaluate(countered.broker_counter)  # carrier takes our counter
    assert n.agreed_rate == countered.broker_counter
    assert n.margin == 3000 - countered.broker_counter


@pytest.mark.parametrize("ceiling", [1000, 2500, 9999])
def test_counters_move_forward_monotonically(ceiling):
    n = make(loadboard=int(ceiling * 0.5), ceiling=ceiling)
    previous = n.current_offer
    for offer in (ceiling * 3, ceiling * 3, ceiling * 3):
        outcome = n.evaluate(offer)
        if outcome.broker_counter is None:
            break
        assert outcome.broker_counter >= previous
        previous = outcome.broker_counter
