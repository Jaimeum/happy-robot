"""Formatting helpers for demo.sh. Reads a JSON response on stdin.

Usage: ... | python3 scripts/_fmt.py <view> [json-path]
"""
import json
import sys

view = sys.argv[1] if len(sys.argv) > 1 else "raw"
data = json.load(sys.stdin)

# FastAPI error envelope. Render it instead of dying on a missing success key —
# an error here is usually the service doing its job, not a failure of the demo.
if isinstance(data, dict) and set(data) == {"detail"}:
    detail = data["detail"]
    if isinstance(detail, dict):
        print(f"    ✗ {detail.get('error', 'error')}")
        for key in ("message", "reason", "code", "current_stage", "required_stage"):
            if detail.get(key):
                print(f"      {key}: {detail[key]}")
        if detail.get("agent_guidance"):
            print(f"      agent ▸ {detail['agent_guidance']}")
    else:
        print(f"    ✗ {detail}")
    sys.exit(3)


def get(path: str):
    node = data
    for part in path.split("."):
        if not part:
            continue
        node = node[int(part)] if part.isdigit() else node[part]
    return node


if view == "field":
    print(get(sys.argv[2]))

elif view == "loads":
    for load in data["loads"]:
        print(
            f"    {load['load_id']}  {load['origin']:<22} -> {load['destination']:<22} "
            f"{load['equipment_type']:<10} ${load['loadboard_rate']:>6}  {load['miles']}mi"
        )

elif view == "detail":
    load = data["load"]
    print(
        f"    {load['commodity_type']} · {load['weight']} lbs · "
        f"{load['num_of_pieces']} pieces · {load['dimensions']}"
    )
    print(f"    notes: {load['notes']}")

elif view == "negotiation":
    print(
        f"    -> {data['decision']}  counter={data['broker_counter']}  "
        f"agreed={data['agreed_rate']}  rounds_left={data['rounds_remaining']}  "
        f"may_transfer={data['may_transfer']}"
    )

elif view == "booking":
    print(
        f"    booked={data['booked']}  ref={data['booking_reference']}  "
        f"rate=${data['agreed_rate']}"
    )

elif view == "handoff":
    print(
        f"    transferred={data['transferred']}  ref={data['handoff_reference']}  "
        f"queue={data['queue']}  reason={data['reason']}"
    )

elif view == "otp":
    codes = [e["code"] for e in data["events"] if e["event"] == "otp_sent" and e.get("code")]
    print(codes[-1] if codes else "")

elif view == "dashboard":
    for key, value in data["northstar_kpis"].items():
        print(f"    {key:<32} {value}")
    print()
    for key, value in data["controls"].items():
        print(f"    {key:<32} {value}")
