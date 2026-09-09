"""Load search and detail. Gated behind identity verification."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.api.deps import get_audit, get_sessions, get_tms, load_session, require_stage
from app.api.schemas import (
    LoadDetailRequest,
    LoadDetailResponse,
    LoadSummary,
    SearchLoadsRequest,
    SearchLoadsResponse,
)
from app.domain.audit import AuditTrail, EventType
from app.domain.loads import Load, normalise_equipment
from app.domain.sessions import Outcome, SessionStore, Stage
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


@router.post("/search", response_model=SearchLoadsResponse)
async def search_loads(
    payload: SearchLoadsRequest,
    sessions: SessionStore = Depends(get_sessions),
    tms: TmsClient = Depends(get_tms),
    audit: AuditTrail = Depends(get_audit),
) -> SearchLoadsResponse:
    session = load_session(payload.call_id, sessions)
    require_stage(session, Stage.IDENTITY_VERIFIED, "Load matching")

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

    filters: dict[str, object] = {}
    if payload.origin_city:
        filters["ORIG_CITY"] = payload.origin_city
    if payload.origin_state:
        filters["ORIG_STATE"] = payload.origin_state.upper()
    if payload.destination_city:
        filters["DEST_CITY"] = payload.destination_city
    if payload.destination_state:
        filters["DEST_STATE"] = payload.destination_state.upper()
    if equipment:
        filters["EQTYPE"] = equipment
    if payload.pickup_date:
        # The wire takes PICKUP_DATE as a filter even though it returns PICKUP_DT.
        filters["PICKUP_DATE"] = payload.pickup_date.replace("-", "")

    if not filters:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "no_filters",
                "message": "At least one search filter is required.",
                "agent_guidance": "Ask where they are loading out of and what they pull.",
            },
        )

    filters["MAX_RESULTS"] = payload.limit

    try:
        records = await tms.query_loads(filters)
    except TmsCommandError as exc:
        audit.emit(session.call_id, EventType.LOADS_SEARCHED, mc_number=session.mc_number,
                   filters=filters, error=exc.code)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "error": "search_rejected", "code": exc.code, "message": exc.message,
                "agent_guidance": "Ask them to restate the lane and equipment, then try again.",
            },
        ) from exc
    except TmsUnavailable as exc:
        # Graceful degradation: never let a socket fault sound like a dead line.
        logger.warning("tms.search_degraded call_id=%s error=%s", session.call_id, exc)
        audit.emit(session.call_id, EventType.TMS_DEGRADED, mc_number=session.mc_number,
                   operation="load_search", error=str(exc))
        return SearchLoadsResponse(
            call_id=session.call_id, stage=session.stage_label, match_count=0,
            loads=[], degraded=True,
            agent_guidance=(
                "The load board is not responding right now. Tell the carrier the "
                "board is briefly down, offer to take their lane and call them back, "
                "and stay on the line rather than ending abruptly."
            ),
        )

    loads = [Load.from_record(record) for record in records]
    summaries = [_to_summary(load) for load in loads]

    audit.emit(session.call_id, EventType.LOADS_SEARCHED, mc_number=session.mc_number,
               filters={k: v for k, v in filters.items() if k != "MAX_RESULTS"},
               match_count=len(summaries),
               load_ids=[s.load_id for s in summaries])

    if not summaries:
        session.outcome = Outcome.NO_LOADS
        return SearchLoadsResponse(
            call_id=session.call_id, stage=session.stage_label, match_count=0, loads=[],
            agent_guidance=(
                "Nothing open on that lane right now. Ask if they are flexible on "
                "destination or pickup day, or offer to note the lane and call them "
                "when something posts."
            ),
        )

    top = summaries[0]
    return SearchLoadsResponse(
        call_id=session.call_id, stage=session.stage_label,
        match_count=len(summaries), loads=summaries,
        agent_guidance=(
            f"{len(summaries)} match(es). Lead with the best one: {top.origin} to "
            f"{top.destination}, {top.equipment_type}, picking up {top.pickup_spoken}, "
            f"{top.miles} miles, posted at ${top.loadboard_rate}. Pitch it and ask if "
            "they want it."
        ),
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
