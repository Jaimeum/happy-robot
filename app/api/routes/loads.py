"""Load search and detail. Gated behind identity verification."""

from __future__ import annotations

import logging
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import (
    get_audit,
    get_board,
    get_sessions,
    get_tms,
    load_session,
    require_stage,
)
from app.config import get_settings
from app.api.schemas import (
    LoadDetailRequest,
    LoadDetailResponse,
    LoadSummary,
    SearchLoadsRequest,
    SearchLoadsResponse,
)
from app.domain.audit import AuditTrail, EventType
from app.domain.board import (
    BoardIndex,
    NormalisedFilters,
    board_facts,
    is_offerable,
    widen,
)
from app.domain.loads import (
    Load,
    equipment_label,
    normalise_equipment,
    normalise_pickup_date,
    normalise_state,
    spoken_day,
)
from app.domain.sessions import Outcome, SearchAttempt, SessionStore, Stage
from app.security.auth import require_token
from app.security.leak_guard import protect_value
from app.tms.client import TmsClient
from app.tms.errors import TmsCommandError, TmsUnavailable

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/v1/loads", tags=["loads"], dependencies=[Depends(require_token)])


def _to_summary(load: Load) -> LoadSummary:
    # Registering the ceiling arms the leak guard for the rest of this request.
    protect_value(load.max_rate)
    return LoadSummary(**load.for_carrier().__dict__)


def _fingerprint(f: NormalisedFilters) -> str:
    return "|".join(f"{k}={v}" for k, v in sorted(f.__dict__.items()) if v)


def _wire_filters(f: NormalisedFilters, max_results: int) -> dict[str, object]:
    wire: dict[str, object] = {}
    if f.equipment:
        wire["EQTYPE"] = f.equipment
    if f.origin_city:
        wire["ORIG_CITY"] = f.origin_city
    if f.origin_state:
        wire["ORIG_STATE"] = f.origin_state
    if f.destination_city:
        wire["DEST_CITY"] = f.destination_city
    if f.destination_state:
        wire["DEST_STATE"] = f.destination_state
    if f.pickup_date:
        wire["PICKUP_DATE"] = f.pickup_date
    if wire:
        wire["MAX_RESULTS"] = max_results
    return wire


_CONCESSIONS = {
    "pickup_date": "nothing picking up {day}",
    "pickup_date_unrecognised": "I could not pin their pickup day down, so this is the whole week",
    "destination_city": "nothing going to {destination}",
    "destination_state": "nothing going to {destination}",
    "origin_city": "nothing out of {origin_city} itself",
    "origin_state": "nothing out of {origin_state} at all",
    "origin_state_unrecognised": "I could not pin the state down",
}


def _spoken_concession(relaxed: list[str], asked: dict[str, str], wire_date: str | None) -> str | None:
    """The sentence the agent says before it pitches.

    Ship the sentence, not the inference. Honesty about a relaxed search should
    not depend on the model re-deriving which filter was dropped.
    """
    clauses: list[str] = []
    for key in relaxed:
        template = _CONCESSIONS.get(key)
        if not template:
            continue
        clause = template.format(
            day=spoken_day(wire_date) or "that day",
            destination=asked.get("destination") or "there",
            origin_city=asked.get("origin_city") or "there",
            origin_state=asked.get("origin_state") or "there",
        )
        if clause not in clauses:
            clauses.append(clause)
    if not clauses:
        return None
    sentence = clauses[0] if len(clauses) == 1 else ", ".join(clauses[:-1]) + ", and " + clauses[-1]
    return sentence[0].upper() + sentence[1:] + "."


def _pitch(top: LoadSummary) -> str:
    return (
        f"{top.origin} to {top.destination}, picking up {top.pickup_spoken}, "
        f"{top.miles} miles, posted at ${top.loadboard_rate}"
    )


def _menu(facts: dict) -> str:
    return ", ".join(
        f"{row['equipment_type'].replace('_', ' ').title()} {row['load_count']}"
        for row in facts.get("equipment_breakdown", [])
    )


def _states_clause(facts: dict) -> str:
    return ", ".join(
        f"{row['state']} {row['load_count']}" for row in facts.get("origin_states", [])
    ) or "nothing"


@router.post("/search", response_model=SearchLoadsResponse)
async def search_loads(
    payload: SearchLoadsRequest,
    sessions: SessionStore = Depends(get_sessions),
    tms: TmsClient = Depends(get_tms),
    board: BoardIndex = Depends(get_board),
    audit: AuditTrail = Depends(get_audit),
) -> SearchLoadsResponse:
    """Answer the question the agent actually has, not just the one it asked.

    The exact filters are tried on the wire first. If they find nothing, the
    ladder relaxes them over the board snapshot and the response says what it
    relaxed, so the agent can be honest about it out loud. A search can no longer
    come back as a bare zero with an invitation to guess again — that invitation
    is what turned one call into ten dead searches over six minutes.
    """
    session = load_session(payload.call_id, sessions)
    require_stage(session, Stage.IDENTITY_VERIFIED, "Load matching")
    settings = get_settings()
    today = date.today()

    equipment = normalise_equipment(payload.equipment_type)
    if payload.equipment_type and not equipment:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "unknown_equipment_type",
                "message": f"{payload.equipment_type!r} is not equipment we book.",
                "agent_guidance": (
                    "We book dry van, reefer, flatbed, step deck and power only. "
                    "Ask which of those they are running."
                ),
            },
        )

    # A state or day we cannot resolve is dropped from the filters and reported,
    # never 422'd back at the agent: the carrier already answered the question,
    # and the ladder relaxes both anyway.
    unresolved: list[str] = []
    origin_state = normalise_state(payload.origin_state)
    if payload.origin_state and not origin_state:
        unresolved.append("origin_state_unrecognised")
    destination_state = normalise_state(payload.destination_state)
    if payload.destination_state and not destination_state:
        unresolved.append("destination_state_unrecognised")
    pickup_date = normalise_pickup_date(payload.pickup_date, today=today)
    if payload.pickup_date and not pickup_date:
        unresolved.append("pickup_date_unrecognised")

    filters = NormalisedFilters(
        equipment=equipment,
        origin_city=payload.origin_city or None,
        origin_state=origin_state,
        destination_city=payload.destination_city or None,
        destination_state=destination_state,
        pickup_date=pickup_date,
    )
    asked = {
        "origin_city": payload.origin_city or "",
        "origin_state": payload.origin_state or "",
        "destination": payload.destination_state or payload.destination_city or "",
    }
    snapshot = board.get()
    facts = board_facts(snapshot, settings, equipment, origin_state) if snapshot else {}
    budget = settings.max_searches_per_call
    used = len(session.searches)

    def respond(
        *,
        loads: list[Load],
        match_count: int,
        basis: str,
        rung: str,
        guidance: str,
        relaxed: list[str] | None = None,
        concession: str | None = None,
        equipment_note: str | None = None,
        exhausted: bool = False,
        degraded: bool = False,
        repeat_of: int | None = None,
        record: bool = True,
    ) -> SearchLoadsResponse:
        summaries = [_to_summary(load) for load in loads][: payload.limit]
        # The ledger of what this call was actually handed. Anything not in here
        # cannot later be detailed, priced or booked.
        session.offered_load_ids.update(s.load_id for s in summaries)
        if record:
            session.searches.append(
                SearchAttempt(
                    number=used + 1,
                    fingerprint=_fingerprint(filters),
                    rung=rung,
                    match_count=match_count,
                    guidance=guidance,
                    loads=summaries,
                )
            )
        spent = len(session.searches)
        return SearchLoadsResponse(
            call_id=session.call_id,
            stage=session.stage_label,
            match_count=match_count,
            loads=summaries,
            degraded=degraded,
            agent_guidance=guidance,
            search_basis=basis,
            rung=rung,
            returned_count=len(summaries),
            relaxed=relaxed or [],
            spoken_concession=concession,
            equipment_note=equipment_note,
            exhausted=exhausted,
            board_facts=facts,
            searches_used=spent,
            searches_remaining=max(0, budget - spent),
            may_search_again=spent < budget,
            repeat_of_search=repeat_of,
        )

    # ── 1. An identical search on this call is answered from the call record ──
    fingerprint = _fingerprint(filters)
    for previous in session.searches:
        if previous.fingerprint == fingerprint:
            audit.emit(session.call_id, EventType.SEARCH_REPEATED,
                       mc_number=session.mc_number, filters={"fingerprint": fingerprint},
                       match_count=previous.match_count)
            return SearchLoadsResponse(
                call_id=session.call_id, stage=session.stage_label,
                match_count=previous.match_count, loads=previous.loads,
                agent_guidance=(
                    "You already ran this exact search on this call and this was the "
                    "answer. Do not run it again. " + previous.guidance
                ),
                search_basis="repeat", rung=previous.rung,
                returned_count=len(previous.loads), board_facts=facts,
                searches_used=len(session.searches),
                searches_remaining=max(0, budget - len(session.searches)),
                may_search_again=len(session.searches) < budget,
                repeat_of_search=previous.number,
            )

    # ── 2. The per-call cap: hand over everything rather than refuse ──
    if used >= budget and snapshot:
        everything = widen(snapshot, NormalisedFilters(equipment=equipment))
        audit.emit(session.call_id, EventType.SEARCH_BUDGET_EXHAUSTED,
                   mc_number=session.mc_number, match_count=len(everything.loads))
        return respond(
            loads=everything.loads, match_count=len(everything.loads),
            basis="budget_exhausted", rung=everything.rung, record=False,
            guidance=(
                f"You have searched {used} times on this call. Stop searching. "
                "Everything on the board for their equipment is in `loads` and "
                f"`board_facts.origin_states`: {_states_clause(facts)}. Offer one of "
                "those loads, or take the lane they want and close with end_call_log."
            ),
        )

    # ── 3. Nothing to search on. With a snapshot, hand over the live menu; ──
    #      without one, refuse here rather than send the wire an empty query.
    if not _wire_filters(filters, payload.limit):
        if snapshot:
            return respond(
                loads=[], match_count=0, basis="none", rung="none", record=False,
                guidance=(
                    "Ask what they pull before you search again — dry van, reefer, "
                    f"flatbed, step deck or power only. Live counts on the board: "
                    f"{_menu(facts)}. Every type with a count has freight, so just ask; "
                    "do not guess for them."
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "no_filters",
                "message": "At least one search filter is required.",
                "agent_guidance": "Ask where they are loading out of and what they pull.",
            },
        )

    # ── 4. Exact filters, on the wire ──
    degraded_to_snapshot = False
    try:
        records = await tms.query_loads(_wire_filters(filters, settings.board_shard_max_results))
        exact = [l for l in (Load.from_record(r) for r in records) if is_offerable(l, settings)]
    except TmsCommandError as exc:
        audit.emit(session.call_id, EventType.LOADS_SEARCHED, mc_number=session.mc_number,
                   filters=_wire_filters(filters, payload.limit), error=exc.code)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "search_rejected", "code": exc.code, "message": exc.message,
                "agent_guidance": "Ask them to restate the lane and equipment, then try again.",
            },
        ) from exc
    except TmsUnavailable as exc:
        logger.warning("tms.search_degraded call_id=%s error=%s", session.call_id, exc)
        audit.emit(session.call_id, EventType.TMS_DEGRADED, mc_number=session.mc_number,
                   operation="load_search", error=str(exc))
        if not snapshot or snapshot.age_seconds > settings.board_stale_after_seconds:
            return respond(
                loads=[], match_count=0, basis="degraded", rung="none",
                degraded=True, record=False,
                guidance=(
                    "The load board is not answering right now. Tell the carrier the "
                    "board is briefly down, ask them to hold for a moment, then run "
                    "search_loads once more. If it fails again, tell them to call back "
                    "in a few minutes and close warmly. Do not promise a callback and "
                    "do not tell them there is a system problem."
                ),
            )
        exact = []
        degraded_to_snapshot = True

    if exact:
        ranked = sorted(exact, key=lambda l: (l.pickup_datetime or datetime.max,
                                              -(l.rate_per_mile or 0)))
        audit.emit(session.call_id, EventType.LOADS_SEARCHED, mc_number=session.mc_number,
                   filters=_wire_filters(filters, payload.limit),
                   match_count=len(ranked), load_ids=[l.load_id for l in ranked])
        top = _to_summary(ranked[0])
        return respond(
            loads=ranked, match_count=len(ranked), basis="exact", rung="exact",
            guidance=(
                f"{len(ranked)} match(es). Lead with {_pitch(top)}. Pitch it and ask if "
                "they want it. These are the only loads that exist for them right now — "
                "do not mention any other lane, city or pickup day."
            ),
        )

    # ── 5. Nothing exact. Relax over the snapshot rather than send them guessing ──
    if not snapshot:
        # Cold board: behave exactly as before rather than claim anything.
        session.outcome = Outcome.NO_LOADS
        audit.emit(session.call_id, EventType.LOADS_SEARCHED, mc_number=session.mc_number,
                   filters=_wire_filters(filters, payload.limit), match_count=0, load_ids=[])
        return respond(
            loads=[], match_count=0, basis="none", rung="exact", record=False,
            guidance=(
                "Nothing open on that lane right now. Ask what else they can run or "
                "where else they can get to, then search once more."
            ),
        )

    ladder = widen(snapshot, filters)
    relaxed = unresolved + ladder.dropped
    concession = _spoken_concession(relaxed, asked, pickup_date)

    # Their state has freight, just not for their trailer. State it as a fact
    # rather than relaxing equipment — a van must never be offered a step deck.
    equipment_note = None
    if origin_state and facts.get("open_in_requested_state", 0) == 0:
        in_state_any = [l for l in snapshot.offerable if l.origin_state.upper() == origin_state]
        if in_state_any and equipment:
            label = equipment_label(equipment)
            equipment_note = (
                f"{origin_state} has {len(in_state_any)} load(s) on the board, but none "
                f"of them are for a {label}. Do not offer them a load for equipment "
                "they do not have."
            )

    if not ladder.loads:
        session.outcome = Outcome.NO_LOADS
        audit.emit(session.call_id, EventType.LOADS_SEARCHED, mc_number=session.mc_number,
                   filters=_wire_filters(filters, payload.limit), match_count=0, load_ids=[])
        label = equipment_label(equipment) if equipment else "that equipment"
        if ladder.exhausted:
            guidance = (
                f"There is nothing at all on the board for a {label} right now — zero "
                "nationwide, not just on their lane. Say that plainly. Do not ask them "
                "to be flexible on date or destination; it will not change the answer. "
                "Tell them to check back tomorrow, then close with end_call_log. Do not "
                "promise a callback and do not search again on this call."
            )
        else:
            guidance = (
                f"Nothing for a {label} on that lane. What the board does have for them "
                f"is in `board_facts.origin_states`: {_states_clause(facts)}. Name two or "
                "three of those states and ask which they can get to. Do not name a city, "
                "state or day that is not in board_facts."
            )
        return respond(
            loads=[], match_count=0, basis="none", rung=ladder.rung,
            relaxed=relaxed, concession=concession, equipment_note=equipment_note,
            exhausted=ladder.exhausted, guidance=guidance, record=False,
        )

    # Dropping the origin STATE means we have left the carrier's region entirely.
    # A load is still returned so the agent has it if they ask, but it must not be
    # pitched: leading a carrier in California with Charleston to Savannah, 83
    # miles at $210 — first only because it picks up soonest — is honest and
    # useless, and it is what makes the agent sound like it found nothing.
    # Out of region, the useful answer is the menu of states that actually have
    # their equipment, so they can say which one they can get to.
    left_their_region = "origin_state" in ladder.dropped
    basis = "cached" if degraded_to_snapshot else "widened"
    audit.emit(session.call_id, EventType.SEARCH_WIDENED, mc_number=session.mc_number,
               filters={"rung": ladder.rung, "dropped": ",".join(ladder.dropped)},
               match_count=len(ladder.loads),
               load_ids=[l.load_id for l in ladder.loads])
    top = _to_summary(ladder.loads[0])
    label = equipment_label(equipment) if equipment else "that equipment"
    prefix = (
        "Working from the board copy taken a moment ago. Quote it normally — the load "
        "is re-checked when you pull the details. " if basis == "cached" else ""
    )
    if left_their_region:
        guidance = (
            f"{prefix}{concession or ''} Do NOT pitch a load — everything left is out "
            f"of their region and offering one would sound like you are not listening. "
            f"Say the concession, then tell them where you actually have a {label} and "
            f"ask which they can get to: {_states_clause(facts)}. Those states and "
            "counts are real; name two or three. If they can reach one, search that "
            "state. If they cannot, take the lane they want and close warmly."
        ).strip()
    else:
        # Lead with the load, not the absence. This guidance used to open with
        # the concession — "Nothing going to Nevada. What I do have is…" — and a
        # live call proved why that is wrong: the agent relayed the first clause,
        # the carrier heard "nothing", and switched equipment believing there was
        # no van freight at all. There was: this very response carried it.
        qualifier = f" Then, if it matters to them, add what is not there: {concession}" if concession else ""
        guidance = (
            f"{prefix}Lead with this load: {_pitch(top)}. Pitch it and ask if they "
            f"want it.{qualifier} Do NOT open with what you do not have — a carrier "
            "who hears \"nothing\" first assumes there is nothing at all and stops "
            "listening. Do not offer any city, state or pickup day that is not in "
            "`loads` or `board_facts.origin_states`."
        ).strip()
    if equipment_note:
        guidance = f"{guidance} {equipment_note}"
    return respond(
        loads=ladder.loads, match_count=len(ladder.loads), basis=basis, rung=ladder.rung,
        relaxed=relaxed, concession=concession, equipment_note=equipment_note,
        guidance=guidance,
    )


@router.post("/detail", response_model=LoadDetailResponse)
async def load_detail(
    payload: LoadDetailRequest,
    sessions: SessionStore = Depends(get_sessions),
    tms: TmsClient = Depends(get_tms),
    audit: AuditTrail = Depends(get_audit),
) -> LoadDetailResponse:
    """POST twin of the GET below.

    The platform's webhook tool nodes post a JSON body; keeping a POST form
    avoids templating the load id into a URL path from an agent-supplied value.
    """
    return await _detail(payload.load_id, payload.call_id, sessions, tms, audit)


@router.get("/{load_id}", response_model=LoadDetailResponse)
async def get_load(
    load_id: str,
    call_id: str = Query(min_length=1, max_length=128),
    sessions: SessionStore = Depends(get_sessions),
    tms: TmsClient = Depends(get_tms),
    audit: AuditTrail = Depends(get_audit),
) -> LoadDetailResponse:
    return await _detail(load_id, call_id, sessions, tms, audit)


async def _detail(
    load_id: str,
    call_id: str,
    sessions: SessionStore,
    tms: TmsClient,
    audit: AuditTrail,
) -> LoadDetailResponse:
    session = load_session(call_id, sessions)
    require_stage(session, Stage.IDENTITY_VERIFIED, "Load details")

    # The offer ledger. Once this call has been handed loads, a load id that was
    # not among them cannot be detailed — so a fabricated id costs at most one
    # bad sentence and can never reach the system of record or load a ceiling.
    # Only enforced once something has actually been offered; before that there
    # is nothing to compare against, and an invented id 404s at the TMS anyway.
    if session.offered_load_ids and load_id not in session.offered_load_ids:
        audit.emit(session.call_id, EventType.LOAD_NOT_OFFERED,
                   mc_number=session.mc_number, load_id=load_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "error": "load_not_offered",
                "message": "That load was not offered on this call.",
                "agent_guidance": (
                    "That load is not one you were given. Do not say it was taken and "
                    "do not invent another. Pitch one of the loads from your last "
                    "search, or search once more."
                ),
            },
        )

    try:
        record = await tms.get_load(load_id)
    except TmsUnavailable as exc:
        audit.emit(session.call_id, EventType.TMS_DEGRADED, mc_number=session.mc_number,
                   load_id=load_id, operation="load_detail", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "tms_unavailable",
                "agent_guidance": (
                    "The load board is briefly unavailable. Keep the carrier on the "
                    "line and try again in a moment."
                ),
            },
        ) from exc

    if record is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail={
                "error": "load_not_found",
                "agent_guidance": "That load is no longer on the board. Offer another one.",
            },
        )

    load = Load.from_record(record)
    session.selected_load_id = load.load_id
    session.advance_to(Stage.LOAD_SELECTED)
    audit.emit(session.call_id, EventType.LOAD_PITCHED, mc_number=session.mc_number,
               load_id=load.load_id, loadboard_rate=load.loadboard_rate,
               max_rate=load.max_rate, equipment=load.equipment_type,
               origin=load.origin, destination=load.destination)

    summary = _to_summary(load)
    return LoadDetailResponse(
        call_id=session.call_id, stage=session.stage_label, load=summary,
        agent_guidance=(
            f"{summary.commodity_type or 'Freight'}, {summary.weight} lbs, "
            f"{summary.num_of_pieces} pieces, delivering {summary.delivery_spoken}. "
            f"Posted at ${summary.loadboard_rate}. If they push on rate, take their "
            "number and run it through the rate check — do not negotiate freehand."
        ),
    )
