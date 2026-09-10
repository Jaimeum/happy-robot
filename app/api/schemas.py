"""Request and response contracts.

Response models are explicit rather than free-form dicts because they are the
last line between internal state and something a voice agent will read out
loud. If a field is not declared here, it cannot be spoken.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- requests

class StartCallRequest(Strict):
    call_id: str | None = Field(default=None, max_length=128)
    channel: str | None = Field(default="web_call", max_length=32)


class VerifyCarrierRequest(Strict):
    call_id: str = Field(min_length=1, max_length=128)
    mc_number: str = Field(min_length=1, max_length=32)


class SendOtpRequest(Strict):
    call_id: str = Field(min_length=1, max_length=128)
    destination: str = Field(min_length=3, max_length=128)
    channel: str | None = Field(default=None, max_length=16)
    carrier_said: str | None = Field(default=None, max_length=2000)


class VerifyOtpRequest(Strict):
    call_id: str = Field(min_length=1, max_length=128)
    code: str = Field(min_length=1, max_length=16)
    carrier_said: str | None = Field(default=None, max_length=2000)


class SearchLoadsRequest(Strict):
    call_id: str = Field(min_length=1, max_length=128)
    origin_city: str | None = Field(default=None, max_length=64)
    # Voice gives you "California", not "CA". The route normalises; a value that
    # will not resolve is dropped from the filters rather than 422'd back at the
    # agent, because the carrier already answered the question.
    origin_state: str | None = Field(default=None, max_length=32)
    destination_city: str | None = Field(default=None, max_length=64)
    destination_state: str | None = Field(default=None, max_length=32)
    equipment_type: str | None = Field(default=None, max_length=32)
    pickup_date: str | None = Field(default=None, max_length=16)
    # The platform tool sends no limit, so a default of 3 meant every live search
    # asked the wire for three of ten open dry vans and reported three as the whole
    # answer. The agent should see what is actually there.
    limit: int = Field(default=10, ge=1, le=10)


class LoadDetailRequest(Strict):
    call_id: str = Field(min_length=1, max_length=128)
    load_id: str = Field(min_length=1, max_length=32)


class NegotiateRequest(Strict):
    call_id: str = Field(min_length=1, max_length=128)
    load_id: str = Field(min_length=1, max_length=32)
    carrier_offer: int = Field(gt=0, le=1_000_000)
    carrier_said: str | None = Field(default=None, max_length=2000)


class BookRequest(Strict):
    call_id: str = Field(min_length=1, max_length=128)
    load_id: str = Field(min_length=1, max_length=32)


class HandoffRequest(Strict):
    call_id: str = Field(min_length=1, max_length=128)
    notes: str | None = Field(default=None, max_length=2000)


class CloseCallRequest(Strict):
    call_id: str = Field(min_length=1, max_length=128)
    reason: str | None = Field(default=None, max_length=256)


# --------------------------------------------------------------- responses

class StartCallResponse(BaseModel):
    call_id: str
    stage: str
    agent_guidance: str


class CarrierVerificationResponse(BaseModel):
    call_id: str
    stage: str
    mc_number: str
    verified: bool
    carrier_name: str | None = None
    dot_number: str | None = None
    domicile: str | None = None
    power_units: int | None = None
    failure_reasons: list[str] = Field(default_factory=list)
    next_step: str
    agent_guidance: str


class OtpSentResponse(BaseModel):
    call_id: str
    stage: str
    sent: bool
    channel: str
    destination_masked: str
    expires_in_seconds: int
    attempts_allowed: int
    agent_guidance: str


class OtpVerifyResponse(BaseModel):
    call_id: str
    stage: str
    verified: bool
    result: str
    attempts_remaining: int
    may_retry: bool
    agent_guidance: str


class LoadSummary(BaseModel):
    load_id: str
    origin: str
    destination: str
    pickup_datetime: str | None = None
    delivery_datetime: str | None = None
    pickup_spoken: str | None = None
    delivery_spoken: str | None = None
    equipment_type: str
    loadboard_rate: int
    miles: int | None = None
    rate_per_mile: float | None = None
    weight: int | None = None
    commodity_type: str | None = None
    num_of_pieces: int | None = None
    dimensions: str | None = None
    notes: str | None = None


class SearchLoadsResponse(BaseModel):
    call_id: str
    stage: str
    # Total matched at the winning rung, not a page size. returned_count is how
    # many are in `loads`.
    match_count: int
    loads: list[LoadSummary]
    degraded: bool = False
    agent_guidance: str

    # How the answer was reached. The agent has to be able to say the truth about
    # a relaxed search out loud, so the reasoning ships in the payload rather
    # than being left for the model to re-derive.
    search_basis: str = "exact"
    rung: str = "exact"
    returned_count: int = 0
    relaxed: list[str] = Field(default_factory=list)
    # The sentence to say before pitching, pre-built. Honesty should not depend
    # on the model reconstructing which filter was dropped.
    spoken_concession: str | None = None
    equipment_note: str | None = None
    exhausted: bool = False
    # The real option set: every row has load_count >= 1, so there is nothing in
    # here the agent can offer that is not actually on the board.
    board_facts: dict = Field(default_factory=dict)
    searches_used: int = 0
    searches_remaining: int = 0
    may_search_again: bool = True
    repeat_of_search: int | None = None


class LoadDetailResponse(BaseModel):
    call_id: str
    stage: str
    load: LoadSummary
    agent_guidance: str


class NegotiateResponse(BaseModel):
    call_id: str
    load_id: str
    stage: str
    decision: str
    round: int
    rounds_remaining: int
    broker_counter: int | None = None
    agreed_rate: int | None = None
    may_book: bool
    may_transfer: bool
    agent_guidance: str


class BookingResponse(BaseModel):
    call_id: str
    stage: str
    booked: bool
    load_id: str
    booking_reference: str | None = None
    agreed_rate: int | None = None
    booking_status: str | None = None
    requires_manual_check: bool = False
    agent_guidance: str


class HandoffResponse(BaseModel):
    call_id: str
    stage: str
    transferred: bool
    handoff_reference: str | None = None
    queue: str | None = None
    reason: str | None = None
    agent_guidance: str


class CallTrailResponse(BaseModel):
    call_id: str
    stage: str
    outcome: str
    mc_number: str | None = None
    carrier_name: str | None = None
    agreed_rate: int | None = None
    booking_reference: str | None = None
    handoff_reference: str | None = None
    events: list[dict]


class HealthResponse(BaseModel):
    status: str
    environment: str
    dependencies: dict[str, str]
    tms: dict[str, object]
