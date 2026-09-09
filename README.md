# Carrier Sales Bridge

Integration layer between the HappyRobot voice platform and HappyRobot Logistics'
systems of record, for the inbound carrier sales desk.

A carrier calls in on a web call. The agent checks their operating authority against
FMCSA, proves their identity with a one-time code, searches the Legacy TMS for a load
on their lane, negotiates a rate inside a ceiling the carrier never learns, books it,
and hands the deal to a senior rep. No dispatcher on the first leg.

This repository is the bridge. It wraps a Legacy TMS that speaks a fixed-width line
protocol over a raw TCP socket, and it holds the business rules that a prompt cannot
be trusted with.

## Why the rules live here and not in the prompt

Two of the brief's requirements are absolute: the rate ceiling is never disclosed, and
the identity check cannot be talked around. A prompt can be argued with. A closed
state machine cannot.

- **The ceiling never leaves the building.** The carrier-facing load type has no
  `max_rate` field at all, so no code path can serialise one. On top of that, a
  response guard inspects the actual outgoing JSON for the ceiling and fails the
  request closed if it finds it (`app/security/leak_guard.py`).
- **Verification is a gate, not an instruction.** Progress is a one-way stage machine
  held server-side. Load search returns 403 until an OTP this service generated has
  been echoed back to it. There is no parameter, header or flag anywhere in the API
  that moves a session forward any other way (`app/domain/sessions.py`).

This matters more than it might sound. During protocol discovery the Legacy TMS
**accepted a booking at three times the load's own `MAX_BUY`** — 13,392 against a
ceiling of 4,464. The system of record does not defend the ceiling. This service is
the only thing that does.

## Running it

Everything runs in Docker. There is no Python on the host.

```bash
cp .env.example .env        # then fill in TMS_*, FMCSA_API_KEY, API_AUTH_TOKEN
docker compose up -d --build
docker compose logs -f app
```

Tests (against the working tree, so a run never exercises a stale image):

```bash
docker compose run --rm tests
```

A narrated end-to-end call against the real TMS and FMCSA:

```bash
./scripts/demo.sh            # carrier haggles once, then books
./scripts/demo.sh hostile    # carrier tries to skip the code and extract the ceiling
./scripts/smoke.sh 1515      # terser variant, any MC number
```

Teardown: `docker compose down`.

## The API

Every route needs `Authorization: Bearer $API_AUTH_TOKEN`. The one exception is
`/live`, which returns an empty 204 for the container's own health probe and reveals
nothing. `/health` is behind the token because it reports dependency state.

| Route | Purpose | Requires |
|---|---|---|
| `POST /v1/calls/start` | Open a call record, get a `call_id` | — |
| `POST /v1/carriers/verify` | FMCSA operating-authority check by MC | call started |
| `POST /v1/identity/otp/send` | Issue a code by SMS or email | authority passed |
| `POST /v1/identity/otp/verify` | Check the code | code sent |
| `POST /v1/loads/search` | Search the open board | identity verified |
| `POST /v1/loads/detail` | Full record for one load | identity verified |
| `POST /v1/negotiate` | Run a carrier's number through the rate desk | identity verified |
| `POST /v1/bookings` | Commit the booking | rate agreed |
| `POST /v1/handoff` | Mocked senior-rep handoff | booked |
| `POST /v1/calls/close` | Close the call, write the outcome | — |
| `GET /v1/calls/{id}` | Full audit trail for one call | — |
| `GET /v1/ops/dashboard` | Northstar KPIs and funnel for the ops manager | — |
| `GET /health` | Dependency and TMS reliability snapshot | — |

Interactive docs at `/docs` once the service is up.

Every response carries an `agent_guidance` string: plain instructions for the voice
agent that reflect the real state of the call, including what *not* to say. The agent
follows the guidance rather than reasoning about policy on its own.

## Layout

```
app/
  tms/          Legacy TMS adapter — wire codec, socket client, fault handling
  domain/       Loads (two shapes), negotiation policy, OTP, sessions, audit trail
  integrations/ FMCSA authority lookup
  security/     Bearer auth, rate-ceiling leak guard
  api/          Routes and request/response contracts
tests/          71 tests: protocol, faults, policy, flow, adversarial
scripts/        smoke.sh — end-to-end against the real upstreams
docs/           Architecture, QA results and KPIs, platform setup
```

## What is deliberately not here

- **A database.** Session state is per-call and lives minutes; the durable audit trail
  belongs in Twin per the brief. Twin is not yet provisioned on the org — see
  `docs/PLATFORM.md`. The audit sink is pluggable so landing it there is one class.
- **Real OTP delivery.** The channel is pluggable; this build writes the code to the
  audit stream where the ops dashboard surfaces it. Production wires HappyRobot Email
  or Twilio SMS at the same call site.
- **A real transfer.** Web calls cannot be transferred, so the handoff is mocked as an
  inspectable queue record — and it is refused when the negotiation failed.

## Documentation

- `docs/WALKTHROUGH.md` — **start here.** How to run it, what each piece does, where to change things
- `docs/DEPLOY.md` — tunnel or deploy, then wire the workflow to it
- `docs/ARCHITECTURE.md` — how it works and why, for IT and business review
- `docs/QA_AND_KPIS.md` — northstar KPIs, test suite, adversarial results
- `docs/PLATFORM.md` — the HappyRobot workflow, and what still needs a human
