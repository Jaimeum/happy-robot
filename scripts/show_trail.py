"""Render a call's audit trail as a readable timeline.

Usage: cat trail.json | python3 scripts/show_trail.py
"""
import json
import sys

DETAIL = {
    "authority_checked": lambda e: f"{e.get('legal_name')} authorised={e.get('authorised')}",
    "otp_sent": lambda e: f"{e.get('channel')} -> {e.get('destination_masked')}",
    "otp_bypass_attempt": lambda e: f"BLOCKED markers={e.get('markers')}",
    "otp_verified": lambda e: f"attempts_used={e.get('attempts_used')}",
    "loads_searched": lambda e: f"{e.get('match_count')} match(es) {e.get('load_ids')}",
    "load_pitched": lambda e: f"{e.get('load_id')} posted={e.get('loadboard_rate')}",
    "negotiation_round": lambda e: (
        f"r{e.get('round')} carrier={e.get('carrier_offer')} "
        f"-> {e.get('decision')} counter={e.get('broker_counter')} "
        f"within_ceiling={e.get('within_ceiling')}"
    ),
    "ceiling_breach_attempt": lambda e: f"BLOCKED carrier asked {e.get('carrier_offer')}",
    "rate_agreed": lambda e: f"{e.get('agreed_rate')} in {e.get('rounds_used')} round(s)",
    "negotiation_failed": lambda e: f"{e.get('reason')}",
    "booking_confirmed": lambda e: f"ref={e.get('booking_ref')} rate={e.get('agreed_rate')}",
    "booking_rejected": lambda e: f"{e.get('code')} on {e.get('load_id')}",
    "booking_uncertain": lambda e: f"NEEDS MANUAL CHECK rate={e.get('agreed_rate')}",
    "handoff_withheld": lambda e: f"WITHHELD reason={e.get('reason')}",
    "handoff_queued": lambda e: f"ref={e.get('handoff_ref')} queue={e.get('queue')}",
    "call_closed": lambda e: f"outcome={e.get('outcome')}",
}

data = json.load(sys.stdin)
print(f"call_id : {data['call_id']}")
print(f"carrier : {data.get('carrier_name')} (MC {data.get('mc_number')})")
print(f"outcome : {data['outcome']}")
print(f"booking : {data.get('booking_reference')}   handoff: {data.get('handoff_reference')}")
print()
print("TIMELINE")
for event in data["events"]:
    render = DETAIL.get(event["event"])
    detail = render(event) if render else ""
    print(f"  {event['at'][11:19]}  {event['event']:<24} {detail}")
