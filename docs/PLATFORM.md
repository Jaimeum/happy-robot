# HappyRobot platform setup

## The workflow

**Inbound Carrier Sales — HappyRobot Logistics**
Editor: https://platform.happyrobot.ai/fdefernandouriamedina/workflows/8l6gr42sfl9j/editor/eim2p49l43jg
Workflow `01a07fb4-6df3-7c0f-9415-2e9a409067da` · version 1 (draft, engine v3) · 23 nodes.

```
Carrier Web Call  (trigger 6e32e01e-722f-4b8b-9372-500b845686d1)
└── Carrier Sales Agent  (inbound voice 0192e5dc-08df-78bf-a549-f43c6bf9f087)
    └── root prompt
        ├── start_call                → POST /v1/calls/start
        ├── verify_carrier_authority  → POST /v1/carriers/verify
        ├── send_verification_code    → POST /v1/identity/otp/send
        ├── check_verification_code   → POST /v1/identity/otp/verify
        ├── search_loads              → POST /v1/loads/search
        ├── get_load_details          → POST /v1/loads/detail
        ├── check_rate                → POST /v1/negotiate
        ├── book_load                 → POST /v1/bookings
        ├── transfer_to_senior_rep    → POST /v1/handoff
        └── end_call_log              → POST /v1/calls/close
```

Each tool is a schema the agent sees; the work happens in its child webhook node. No
policy lives in the prompt — the prompt states the rules so the agent phrases them
naturally, but the bridge enforces them, and every tool response carries an
`agent_guidance` string the agent follows.

The web call trigger means no phone number is provisioned, as the brief requires.

## Workflow variables

| Key | Purpose | Current value |
|---|---|---|
| `BRIDGE_BASE_URL` | Base URL of the bridge service | set to the live tunnel — **wired** |
| `BRIDGE_TOKEN` | Bearer token for the bridge (hidden in UI) | set to `API_AUTH_TOKEN` — **wired** |

All ten webhook nodes reference these, so pointing the workflow at a real deployment is
two value edits, not twenty node edits. Set `BRIDGE_TOKEN` to the `API_AUTH_TOKEN` from
your `.env`. Enter it in the platform UI rather than pasting it into a transcript.

Both are wired and verified: `test-all` reports **12 passed, 0 failed**, with the bridge
answering `201` on `/v1/calls/start` and `422` on the rest. The `422`s are correct for a
test-all — the `{{$var:…}}` tool parameters have no values outside a live call — and they
prove the bearer token is accepted, since a bad token returns `401` instead.

Behind a tunnel, every webhook node also needs a `ngrok-skip-browser-warning: true`
header. ngrok's free tier serves an HTML interstitial instead of the API when the
request looks like it came from a browser, and the resulting parse failure looks like a
broken bridge rather than a tunnel policy.

## What still needs a human

**1. Deploy the bridge and set `BRIDGE_BASE_URL`.** The workflow cannot reach a service
on localhost. Any container host works — the image has no dependencies beyond the two
upstreams. For a quick demo without deploying, a tunnel to the local container is
enough:

```bash
docker run -d --name hr-tunnel --network host \
  cloudflare/cloudflared:latest tunnel --url http://localhost:8000 --no-autoupdate
docker logs hr-tunnel 2>&1 | grep trycloudflare.com
```

That publishes the local service to the internet behind bearer auth. It is fine for a
recorded demo and should be torn down afterwards (`docker rm -f hr-tunnel`).

**2. Provision Twin.** `get_schema` returns `404 Twin database not available`, so the
org has no Twin database yet. Check **Settings → Twin Database** or ask HappyRobot to
enable it. Once it exists, the intended tables are:

- `call_log` — one row per call: `call_id`, `mc_number`, `carrier_name`, `load_id`,
  `outcome`, `agreed_rate`, `booking_ref`, `handoff_ref`, `started_at`, `ended_at`
- `negotiation_rounds` — one row per round: `call_id`, `load_id`, `round`,
  `carrier_offer`, `broker_counter`, `decision`, `within_ceiling`, `at`
- `control_events` — bypass attempts, ceiling-breach attempts, degraded TMS calls

The bridge's audit sink is a protocol with two implementations today (`LogSink`,
`MemorySink`). Adding `TwinSink` lands the same events there without touching a single
emit call site.

**3. Build the Apps dashboard.** The data is already served, aggregated and free of raw
logs, by `GET /v1/ops/dashboard`. The remaining work is the Next.js app that renders
it: KPI tiles for the two invariants and margin protected, the funnel, the controls
counters, TMS reliability, and the recent-calls table.

**4. Enter the northstars.** Six behavioural criteria are written out in
`docs/QA_AND_KPIS.md`. Creating them via MCP still fails after retrying with an explicit
`version_id`, and the two node ids fail differently: the prompt node returns `404 Prompt
node not found for this agent`, the agent node `404 Node not found for this workflow` —
while `list` resolves both. That points at the endpoint, not the ids, so enter them in
the Evaluate tab.

**5. Choose the agent's voice, language and model.** Now set: name `Carrier Sales
Agent`, language English, voice *Ellen — Serious, Direct and Confident* (`en-US`), which
matches the delivery northstar. These were not "platform defaults" as previously
recorded — they were unset, and being unset is a hard validation error that blocks both
publishing and `test-all`. The voice is a one-field change if a different register is
wanted.

**The model matters more than it looks.** The first live call failed on the model, not
on the integration. On `gpt-5.6-luna` — the catalogue's cost-optimised tier, with no
reasoning level — the agent received

```json
{"verified": true, "carrier_name": "BOLT BUS",
 "agent_guidance": "Authority checks out for BOLT BUS. Next, confirm their identity…"}
```

and told the carrier *"I couldn't verify authority for MC 1515."* It read the tool
result and took the opposite branch, then ended the call. The bridge returned `200`, the
audit trail recorded `authorised: true`, and FMCSA was never the problem.

That failure mode is the direct cost of the architecture: because the prompt holds no
policy and every decision comes back from a tool, an agent that misreads a tool response
has nothing to fall back on. Orchestrating ten tools under hard rules needs a model that
reliably reads a boolean. Set to `gpt-5.6-terra-medium` on v2 — the smallest step up that
fixes it without punishing voice latency.

The prompt now also spells out how to read a result: a `true` boolean means the step
passed, an empty `failure_reasons: []` is not a failure, and a tool *error* must never be
reported to the carrier as a failed check.

**The model was only half of it.** On v2 the agent stopped giving the wrong answer and
started looping instead: `verify_carrier_authority` ran four times, each returning
`verified: true`, and the call never reached the code step. Two causes, both introduced
or missed on the previous pass:

1. Every tool had `message.type: "none"`, so the line went silent while tools ran. The
   carrier fills that silence — the first live transcript has them saying *"Hello?"*
   right after `start_call`, and repeating their MC number after `verify_carrier_authority`.
2. The new prompt said to "call the tool once more" on an unreadable result, with no
   cap. A repeated MC number plus an uncapped retry is a loop.

Fixed on v3: AI fillers on all nine conversational tools, and a prompt rule that a step
is finished once its tool returns — never re-run a step, acknowledge a repeat and carry
on from where the call actually is, and retry a genuine error at most once.

**6. Decide on Web Call enhanced security.** It is on by default, which requires a
viewer to sign into the org. That is right for production and probably wrong for a
reviewer opening a demo link — check the per-environment setting before recording.

**7. Publish the version.** Still a draft, and now the only thing standing between the
workflow and a live web call: the variables are wired, the agent validates, and
`test-all` is clean. `BRIDGE_BASE_URL` already points at a real tunnel.

## Org state as found

Greenfield: one empty scratch workflow (`prueba`), no phone numbers, no SIP trunks, no
MCP servers, no knowledge bases, no Twin. Nothing was deleted or modified — the new
workflow was created alongside.

## Notes for whoever edits this next

- `update_workflow_nodes` replaces `configuration` wholesale. Fetch the current config
  with `get_node_details` first or you will silently wipe sibling fields.
- **A tool node's `function` is a full replace too**, and this one bites hard. Sending
  `{"function": {"message": …}}` to add a filler wiped that tool's `parameters` and
  `description` — the agent was left a tool it could no longer call correctly. The tool
  description says only that tool nodes "accept `function` field updates", which reads
  like a merge. Always send the complete `function`: `message`, `parameters`,
  `description`, `tool_index_id` and `tool_index_hash`. Change one tool, re-read it, then
  do the rest.
- **`test-all` overwrites every webhook node's stored output, and that output IS the
  schema the agent is handed.** This is the trap that cost three live calls. A test-all
  run sends empty `{{$var:…}}` parameters, so nine of the ten webhooks answer `422`, and
  their recorded output becomes `{"error": …}`. From then on the agent gets an `error`
  field on every tool — even when the bridge answers `200` with `verified: true`. It
  reads that correctly and reports a failure, so the symptom looks like a bad model or a
  bad prompt and is neither.
  `POST /v1/calls/start` is the tell: it has a static body, returns `201` under test-all,
  and is the one tool that kept working through all three broken calls.
  Check with `get_available_variables` — read-only, safe. If a webhook group shows only
  **Error**, the schema is poisoned. Restore it with `set_custom_output` per node, and
  **do not run `test-all` or `fix_broken_vars` afterwards** or you undo the repair.
  Verify with `get_available_variables`, never with a test run.
- **A tool's `message` is what the caller hears while the tool runs**, not what comes
  back to the agent. `none` means the line goes dead for the whole call — including a
  multi-second TMS search. On a voice call that silence is not cosmetic: the carrier
  assumes they were not heard and repeats themselves, and the agent treats the repeat as
  new input. Use `ai` (the agent generates a contextual line, guided by `description` and
  `example`) or `fixed`. Only `end_call_log` should stay silent.
  Watch the wording on `check_rate`: "let me see what I can do" or "how much room I
  have" hints at a ceiling the agent is forbidden to disclose. "Let me run that number"
  is neutral.
- Paragraph fields (`url`, header values) need real Plate arrays. The string-template
  shorthand is **not** transformed on `update_workflow_nodes` for these fields — passing
  a plain string returns `400 expected array, received string`.
- Webhook v2 bodies go in `body.raw` as a JSON string with `{{$var:<node_id>.<param>}}`
  tokens, using the **tool node's** persistent id — not the action node's.
- Workflow variables live under `group_id: "use_case_variables"`, **not** `"env"`.
  An earlier note here claimed `env` was confirmed working; it was wrong, and it cost a
  whole debugging session. A reference with `group_id: "env"` resolves to nothing, so the
  webhook URL renders empty and the call fails with
  `unsupported protocol scheme ""` — which reads like a bridge outage, not a bad
  reference. Confirm the group id with `get_available_variables` before wiring anything,
  and use `fix_broken_vars` (`dry_run=true` first) to audit the whole version.
- The voice agent node requires `agent.name`, `agent.languages` and `agent.voices`.
  Languages and voices are arrays of `templated_value` objects
  (`{"type":"static","static":{"id":…,"name":…}}`), not bare id strings. Without them
  `test-all` refuses to run at all, which hides every other node's status.
- `manage_northstars` `create` is broken for this workflow shape: it 404s on both the
  prompt node ("Prompt node not found for this agent") and the agent node ("Node not
  found for this workflow"), even though `list` resolves both. Its
  `positive_examples`/`negative_examples` also want Plate records, not the strings the
  tool description advertises. Enter northstars in the Evaluate tab instead.
