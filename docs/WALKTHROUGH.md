# Walkthrough — how to run it, what each piece does, and where to change things

Read this with a terminal open. Everything below is runnable right now.

---

## Part 1 — The two halves

```
   ┌─────────────────────────────────────┐        ┌──────────────────────────────┐
   │  HappyRobot platform (SaaS)         │        │  This repo (Docker)          │
   │                                     │        │                              │
   │  Web call trigger                   │        │  Carrier Sales Bridge        │
   │       ↓                             │        │       ↓                      │
   │  Voice agent  ──── 10 tools ────────┼───────▶│  10 HTTP endpoints           │
   │                                     │  HTTPS │       ↓                      │
   │  Decides WHAT TO SAY                │        │  Decides WHAT IS ALLOWED     │
   └─────────────────────────────────────┘        └──────┬───────────────────────┘
                                                          │
                                              ┌───────────┴───────────┐
                                              ▼                       ▼
                                        Legacy TMS              FMCSA REST
                                        (raw TCP)               (authority)
```

The line between them is the whole design. The agent is good at conversation and bad
at holding a line under pressure. The bridge is the opposite. So the agent phrases
things and the bridge decides them.

---

## Part 2 — Running the service

The container is probably already up. Check:

```bash
cd /workspaces/hr/happy-robot
docker compose ps
```

You want `Up ... (healthy)`. If not:

```bash
docker compose up -d --build app     # build + start
docker compose logs -f app           # follow logs (Ctrl-C to stop following)
docker compose down                  # stop everything
```

Three ways to exercise it, in increasing depth.

### a) The narrated demo — start here

```bash
./scripts/demo.sh            # carrier haggles once, then books
./scripts/demo.sh hostile    # carrier tries to skip the code and extract the ceiling
```

This walks a whole call, printing what the carrier says, what the agent is told to say,
and what the service decided. It hits the **real** TMS and the **real** FMCSA. The
booking in happy mode is a real write.

The most instructive line in the output is in step 6:

```
posted $2534 · true ceiling $3157 (from the internal log — the carrier never sees this)
```

That ceiling is read out of the internal log, not the API response, precisely to show
it never appears in anything the carrier could hear.

### b) The test suite

```bash
docker compose run --rm tests                                    # all 71
docker compose run --rm tests python -m pytest tests/test_adversarial.py -v
```

Tests run against the working tree via a bind mount, so you never accidentally test a
stale image. `tests/test_adversarial.py` is the interesting one — it is the OTP-bypass
and ceiling-extraction attacks written as executable assertions.

### c) Poking it by hand

```bash
set -a && . ./.env && set +a
AUTH="Authorization: Bearer $API_AUTH_TOKEN"

# every route needs the token
curl -s -H "$AUTH" localhost:8000/health | python3 -m json.tool

# start a call, keep the id
CID=$(curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  localhost:8000/v1/calls/start -d '{}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["call_id"])')

# try to jump straight to loads — this is the gate
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  localhost:8000/v1/loads/search -d "{\"call_id\":\"$CID\",\"origin_state\":\"IL\"}" | python3 -m json.tool
```

Interactive API docs, with every schema: **http://localhost:8000/docs**

Two helper scripts you will reuse:
- `scripts/show_trail.py` — pipe a call-trail JSON into it for a readable timeline
- `scripts/smoke.sh` — terser than demo.sh, good for a quick "is it alive"

---

## Part 3 — What actually happens on a call

Ten endpoints, in the order a call uses them. Each returns an `agent_guidance` string —
plain English telling the agent what to do next, including what *not* to say.

| # | Endpoint | What it does | Blocked unless |
|---|---|---|---|
| 1 | `POST /v1/calls/start` | Opens a call record, returns `call_id` | — |
| 2 | `POST /v1/carriers/verify` | FMCSA authority by MC number | call exists |
| 3 | `POST /v1/identity/otp/send` | Issues a 6-digit code | authority passed |
| 4 | `POST /v1/identity/otp/verify` | Checks the code | code sent |
| 5 | `POST /v1/loads/search` | Searches the live TMS | **identity verified** |
| 6 | `POST /v1/loads/detail` | Full record for one load | identity verified |
| 7 | `POST /v1/negotiate` | The rate desk — accept / counter / reject | identity verified |
| 8 | `POST /v1/bookings` | Real write to the TMS | rate agreed |
| 9 | `POST /v1/handoff` | Mocked senior-rep transfer | booked |
| 10 | `POST /v1/calls/close` | Writes the outcome, returns the trail | — |

Plus `GET /v1/calls/{id}` (audit trail) and `GET /v1/ops/dashboard` (the KPIs).

### The stage machine

A call moves through ordered stages and **only forwards**:

```
STARTED → AUTHORITY_VERIFIED → OTP_SENT → IDENTITY_VERIFIED
        → LOAD_SELECTED → RATE_AGREED → BOOKED → CLOSED
```

Each endpoint declares its minimum stage. That is the entire OTP defence: there is no
argument, header, or request field anywhere in the API that advances a session. Only
actually passing the step does.

Try it — every one of these is a 422 rejected by the schema before any handler runs:

```bash
curl -s -X POST -H "$AUTH" -H 'Content-Type: application/json' \
  localhost:8000/v1/loads/search \
  -d "{\"call_id\":\"$CID\",\"origin_state\":\"IL\",\"skip_otp\":true}"
```

Terminal state is tracked separately from stage, which matters: a rejected call is
marked `CLOSED`, the *highest* stage value, so if terminality were just "stage ==
CLOSED" a rejected carrier would satisfy every ordering check instead of failing them
all. That was a real bug the test suite caught.

### The negotiation engine

Live example from the demo, ceiling $3157, posted $2534:

| Round | Carrier asks | We do | Why |
|---|---|---|---|
| 1 | $3,294 | counter $2,914 | Above ceiling. Concede 50% of the gap, capped at ceiling − 3% = $3,062 |
| 2 | $2,914 | **accept** | They took our own counter |

Result: booked at $2,914, **$243 of margin protected**, ceiling never spoken.

In hostile mode with a $676 load (ceiling $759):

| Round | Carrier asks | We do |
|---|---|---|
| 1 | $1,500 | counter $736 |
| 2 | $1,100 | counter $736 (already at cap) |
| 3 | $900 | **rejected** — three rounds used |

Then `/v1/handoff` returns `transferred=false, reason=failed_negotiation`.

Knobs, all in `.env`:

```bash
MAX_NEGOTIATION_ROUNDS=3              # the hard cap
NEGOTIATION_CEILING_BUFFER_PCT=0.03   # how far below the ceiling counters stop
NEGOTIATION_AUTO_ACCEPT_PCT=0.02      # ask within this of our offer → just take it
```

The concession curve (50%, 65%, 80% of the remaining gap per round) is in
`app/domain/negotiation.py:CONCESSION_SCHEDULE`.

### Three layers keeping the ceiling secret

1. **Type separation** — `CarrierLoad` has no `max_rate` field at all, so no code path
   can serialise one (`app/domain/loads.py`).
2. **Capped counters** — we never say a number above ceiling × 0.97. Agreement always
   lands on a figure the *carrier* named.
3. **Response guard** — every outgoing JSON body is scanned for the ceiling, including
   inside free text and with thousands separators. A hit fails the request closed with
   a 500 and a CRITICAL log (`app/security/leak_guard.py`).

The audit-trail endpoint also strips `max_rate` and `margin_protected` at any depth, so
even the operator view can't become the leak.

---

## Part 4 — The HappyRobot platform side

**Editor:** https://platform.happyrobot.ai/fdefernandouriamedina/workflows/8l6gr42sfl9j/editor/eim2p49l43jg

Org `fdefernandouriamedina` · workflow `8l6gr42sfl9j` · version 1 · **draft, not
published** · 23 nodes.

### What you will see when you open it

```
Carrier Web Call                    ← trigger, no phone number provisioned
└── Carrier Sales Agent             ← inbound voice agent
    └── (root prompt)               ← the agent's instructions
        ├── start_call              → POST /v1/calls/start
        ├── verify_carrier_authority → POST /v1/carriers/verify
        ├── send_verification_code   → POST /v1/identity/otp/send
        ├── check_verification_code  → POST /v1/identity/otp/verify
        ├── search_loads             → POST /v1/loads/search
        ├── get_load_details         → POST /v1/loads/detail
        ├── check_rate               → POST /v1/negotiate
        ├── book_load                → POST /v1/bookings
        ├── transfer_to_senior_rep   → POST /v1/handoff
        └── end_call_log             → POST /v1/calls/close
```

Each tool node is **just a schema** — the name, description and parameters the agent
sees. The actual HTTP call is the child webhook node underneath it. Click a tool to
edit what the agent understands; click its child to edit where it posts.

### Why it does not work yet

The webhook nodes point at a workflow variable `BRIDGE_BASE_URL`, currently
`https://bridge.example.com` — a placeholder. **The platform cannot reach a service on
your laptop's localhost.** Two options:

**Option A — a tunnel, for a demo.** Publishes the local container to a temporary
public URL, still behind bearer auth.

**Option B — deploy it.** The image has no dependencies beyond the two upstreams, so
Cloud Run / Fly / Railway / ECS all work unchanged.

Either way you then set two workflow variables and publish. Full commands, the
one-replica constraint, and the post-deploy checks are in **`docs/DEPLOY.md`**.

### Editing the workflow

- **The agent's instructions** live in the root prompt node. That is where tone,
  sequence and the "rules you do not break" text sits. Changing it changes how the
  agent *talks*, never what it is *allowed* to do — that is the bridge's job.
- **Adding a tool**: add a tool node under the prompt for the schema, then a webhook
  node under that tool for the call. Copy an existing pair.
- **Two gotchas** that cost me time and will cost you the same:
  - `update_workflow_nodes` replaces `configuration` **wholesale**. Fetch the current
    config first or you silently wipe sibling fields.
  - Webhook v2 bodies go in `body.raw` as a JSON string with
    `{{$var:<tool_node_id>.<param>}}` tokens — the **tool** node's id, not the webhook
    node's.

---

## Part 5 — What is done vs. what is not

**Working, end to end, against real systems:**
- FMCSA authority verification
- OTP issue and verify, ungateable
- Load search and detail against the live Legacy TMS
- Negotiation with the ceiling enforced and the 3-round cap
- Real bookings written to the TMS
- Mocked handoff, correctly withheld on failed negotiations
- Full audit trail per call
- Ops dashboard with the northstar KPIs
- 71 tests, containerised, one command to run

**Not done:**

| Gap | Why it matters | Effort |
|---|---|---|
| Bridge not deployed publicly | The workflow can't call it, so no live voice demo yet | Small — deploy + 2 variables |
| Twin not provisioned (404 on the org) | The brief mandates it as the data layer; audit currently goes to logs + memory | Small once enabled — one `TwinSink` class, call sites unchanged |
| Apps dashboard not built | The brief mandates Apps as the ops UI | Medium — data is already served by `/v1/ops/dashboard` |
| Northstars not created | API returned 404 linking agent↔prompt | Small — 6 criteria written out in `QA_AND_KPIS.md`, paste into Evaluate tab |
| OTP delivery mocked | Codes go to the audit stream, not SMS/email | Small — swap the delivery call site |
| No voice testing yet | Everything is verified at the API level, not by talking to it | Blocked on the deploy |

---

## Part 6 — Where to change things

| You want to change | Edit |
|---|---|
| How the agent talks, its sequence, its refusals | Root prompt node in the platform editor |
| Negotiation aggressiveness | `.env` knobs, or `CONCESSION_SCHEDULE` in `app/domain/negotiation.py` |
| Round limit | `MAX_NEGOTIATION_ROUNDS` in `.env` |
| OTP length / TTL / attempts | `OTP_*` in `.env` |
| What the carrier can hear about a load | `CarrierLoad` in `app/domain/loads.py` |
| The guidance strings the agent follows | The route handlers in `app/api/routes/` |
| Which KPIs the manager sees | `app/api/routes/ops.py` |
| TMS retry/timeout behaviour | `TMS_*` in `.env`, logic in `app/tms/client.py` |
| What gets logged per call | `EventType` and emit calls, `app/domain/audit.py` |

**Good candidates for the next iteration**, roughly by value:

1. **Deploy + wire the workflow**, then actually talk to it. Everything else is
   guesswork until you hear it on a call.
2. **Twin + the Apps dashboard** — two explicit requirements in the brief, and the
   dashboard is the thing a reviewer will want to see on screen.
3. **The commercial case** — margin protected per load is already computed. Turning it
   into an annualised ROI number is the "Value Orientation" competency the guide names.
4. **Real OTP delivery** so the demo doesn't need a log grep.
5. **Adversarial suites on-platform** — the code-level attacks pass; the conversational
   equivalents test whether the *agent* volunteers what the API withheld.
