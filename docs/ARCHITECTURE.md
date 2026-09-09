# Build description — Inbound Carrier Sales Automation

For HappyRobot Logistics' IT and business reviewers. What was built, how it behaves,
and which decisions are load-bearing.

## The problem in one paragraph

Inbound carrier calls have grown with load volume; the carrier desk has not. Calls are
missed at peak, every dispatcher works to a different rate ceiling, FMCSA lookups are
done by hand, and there is no structured record of what was offered or agreed. The
first leg of these calls — qualify, verify, match, negotiate — is repetitive and
high-volume, which is exactly the part that can be automated without touching the
relationships that matter.

## What was built

Two pieces.

**A voice workflow on the HappyRobot platform.** Web call trigger, inbound voice agent,
ten tools. The agent handles conversation, tone and pacing. It holds no policy.

**A bridge service in this repository.** An authenticated HTTP API that wraps the
Legacy TMS, calls FMCSA, issues and checks one-time codes, enforces the rate ceiling
and the three-round negotiation limit, and writes the audit trail. Containerised, one
command to deploy.

```
Carrier (web call)
      │
      ▼
HappyRobot voice agent ──tools──▶  Carrier Sales Bridge  ──TCP──▶  Legacy TMS
                                            │
                                            ├──REST──▶  FMCSA
                                            ├──▶  OTP issue / verify
                                            └──▶  Audit trail  ──▶  Twin (pending)
                                                        │
                                                        ▼
                                            Ops dashboard (Apps)
```

The split is deliberate: **the agent decides what to say, the bridge decides what is
allowed.** Anything the brokerage would be unhappy to see negotiated away lives on the
bridge side of that line.

## The finding that shaped the design

The Legacy TMS was probed before any code was written. Two things stood out.

**The wire does not match the manual.** The protocol reference shows zero-padded
numerics (`RATE:0002150`) and error code `UNKNOWN_LOAD`. The live server sends
left-aligned space-padded numerics (`RATE:2290    `) and returns `NOT_FOUND`. It also
rejects an unknown equipment type with an error, where the manual says it returns no
records. Parsing by counting field widths — which the manual recommends — would have
produced silently wrong numbers. The adapter parses by key and strips, so padding
changes cannot hurt it.

**The TMS does not enforce the rate ceiling.** A booking was accepted at 13,392 against
a load whose `MAX_BUY` was 4,464 — three times over. There is no server-side guard.
This is the margin leakage the brokerage described, and it means the ceiling can only
be defended in the integration layer. Every design decision below follows from that.

## The five controls

### 1. The rate ceiling never reaches the carrier

Three independent layers, because one is a promise and three is a guarantee.

- **Type separation.** `Load` (internal) carries `max_rate`. `CarrierLoad` — the only
  thing serialised to a caller — has no such field. A future edit cannot accidentally
  add it to a response because there is nowhere to put it.
- **Counters are capped below the ceiling.** Our offers never exceed
  `max_rate × (1 − 3%)`. Agreement always lands on a number the *carrier* named. The
  ceiling is therefore never spoken, even by a carrier who spends all three rounds
  probing for it.
- **A response guard.** Any request that loads a ceiling registers it; the outgoing
  JSON is walked for that value — including inside free text and thousands-separated
  numbers — and the request fails closed with a 500 and a CRITICAL log if it appears.
  A number the carrier themselves offered is exempt, since echoing their own figure
  back discloses nothing.

The audit-trail endpoint additionally strips `max_rate` and `margin_protected` at any
depth, so even the operator-facing view of a call cannot become the leak.

### 2. Identity verification cannot be talked around

The brief requires the OTP step to survive social engineering "under any framing". A
prompt cannot promise that — a caller only has to find the phrasing nobody anticipated.

So the guarantee is structural. A call moves through an ordered, one-way stage machine
held server-side. Each endpoint declares the stage it requires. Load search returns 403
until a code this service generated has been echoed back to it, and **no argument,
header or flag anywhere in the API advances a session any other way.**

The service does recognise common bypass phrasings — "already in your system", "my
phone died", "the dispatcher always skips this" — but only to count them for the ops
team. Detection is not what blocks the caller. An unrecognised phrasing is refused just
as hard, which is the point.

Codes are generated with a CSPRNG, compared in constant time, expire in five minutes,
and allow three attempts. A burned challenge terminates the call rather than offering a
fresh code, since re-issuing on demand would make the attempt cap a formality.

A subtle bug found by the test suite is worth recording: rejected calls were initially
marked `CLOSED`, the highest stage value — which meant a rejected carrier satisfied
*every* ordering check instead of failing them all. Terminal state is now tracked
separately from progress.

### 3. Negotiation is bounded and deterministic

The engine opens at the posted rate and concedes a shrinking share of the gap each
round (50%, 65%, 80%), never above the capped counter. It takes any ask at or below its
standing offer, and on the final round takes any ask inside the ceiling rather than
losing a workable load over the last few percent.

Three rounds, hard. Round four does not exist: the engine refuses it, marks the
negotiation failed, and **withholds transfer** — the brief is explicit that a failed
negotiation is not handed to a senior rep. The booking endpoint independently re-checks
the agreed rate against the ceiling rather than trusting the engine, so corrupted state
cannot produce an over-ceiling booking.

If a load has no `MAX_BUY` on record — the TMS omits it entirely on tokens not flagged
for it — the engine refuses to deal rather than improvising a ceiling. Absent is not
zero.

### 4. The TMS is assumed to be unreliable

The non-production instance injects faults on every operational command without
signalling them, at roughly 5% of calls. Four shapes are documented and all four are
handled:

| Fault | Handling |
|---|---|
| Timeout — no response written | Client deadline well under the server's 30s idle timeout; retried with backoff |
| Partial response — no `END` terminator | Treated as a fault, never as a short-but-valid result |
| Malformed framing | Rejected and retried |
| Delayed termination — socket held open after a good response | Returns the moment `END` arrives instead of waiting for close |

Reads are retried. **Bookings are never blind-retried** — a timed-out `LOAD_BOOK` may
or may not have landed, and retrying could double-book. That case returns "rate locked,
a rep will confirm", flags the call for manual check, and tells the agent not to retry.

When the board is unreachable, search degrades to a graceful in-conversation response
rather than an error: the agent tells the carrier the board is briefly down and offers
a callback. A socket hiccup never sounds like a dead call.

`DEBUG_ECHO` bypasses the fault layer entirely, so `/health` reports it as "reachable
(transport and auth only)" rather than as a green light for the whole system.

### 5. Everything is logged

Every call emits an ordered event stream: authority result, code issued and verified,
loads searched and pitched, every offer and counter with a timestamp and whether it was
inside the ceiling, ceiling-breach attempts, bypass attempts, agreed rate, booking
reference, handoff, and outcome. This is the audit trail the brokerage does not have
today, and it is what makes a disputed call resolvable.

Two sinks today — structured JSON on stdout, and a bounded in-memory buffer that backs
the dashboard. Neither is the system of record. Twin is, per the brief, and landing
these events there is one class at an unchanged call site.

## Operational signals

`GET /v1/ops/dashboard` is built for the carrier-sales manager, not for engineers. It
aggregates rather than exposing a transcript firehose, which is what the brief means by
surfacing signals "without accessing raw platform logs":

- **Northstar KPIs** — ceiling breaches and OTP bypasses (both must be zero; anything
  else is an incident), booking conversion, margin protected, average rounds to close.
- **Funnel** — calls handled, authority pass rate, identity pass rate, loads pitched,
  rates agreed, bookings, handoffs, failed negotiations.
- **Controls** — breach attempts blocked, bypass attempts blocked, handoffs withheld,
  bookings needing manual check.
- **TMS reliability** — attempts, faults absorbed, fault rate, retries, p95 latency.
  This is what tells the brokerage whether their legacy system is degrading.

## Security posture

Every endpoint requires a bearer token; the service refuses to start serving if the
token is unset rather than silently running open. The only unauthenticated route is
`/live`, which returns an empty 204. Requests use strict schemas that reject unknown
fields — an attempt to pass `skip_otp` or `override` is a 422 before it reaches any
handler. The container runs as a non-root user. Secrets come from the environment; the
repository holds only `.env.example`.

## Deployment

`docker compose up -d --build` builds and runs the whole thing. The image is a single
service with no external dependencies beyond the two upstreams, so it drops onto any
container host the customer prefers — Cloud Run, ECS, Fly, a VM — with no changes.

Configuration is entirely environment variables. The HappyRobot workflow points at the
bridge through a `BRIDGE_BASE_URL` workflow variable, so moving between environments is
one value, not ten node edits.

## Known gaps

Honest list, all tracked in `docs/PLATFORM.md`:

1. **Twin is not provisioned on the org** (`404 Twin database not available`). The
   audit trail is designed for it and currently lands in logs plus memory.
2. **The Apps dashboard is not built.** The data behind it is done and served by
   `/v1/ops/dashboard`; the UI is the next increment.
3. **Northstars could not be created through the API** (404 linking agent to prompt).
   The criteria are written out in `docs/QA_AND_KPIS.md` ready to be entered.
4. **The bridge is not yet deployed to a public host,** so the workflow's tools point
   at a placeholder. One variable change once a host exists.
5. **OTP delivery is mocked** to the audit stream rather than sent by SMS or email.
