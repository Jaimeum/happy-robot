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
| `BRIDGE_BASE_URL` | Base URL of the bridge service | `https://bridge.example.com` — **placeholder** |
| `BRIDGE_TOKEN` | Bearer token for the bridge (hidden in UI) | `REPLACE_WITH_API_AUTH_TOKEN` — **placeholder** |

All ten webhook nodes reference these, so pointing the workflow at a real deployment is
two value edits, not twenty node edits. Set `BRIDGE_TOKEN` to the `API_AUTH_TOKEN` from
your `.env`. Enter it in the platform UI rather than pasting it into a transcript.

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
`docs/QA_AND_KPIS.md`. Creating them via MCP returned `404 Prompt node not found for
this agent` against both the prompt node and the agent node, so they need to be entered
in the Evaluate tab or the API path needs a look.

**5. Choose the agent's voice, language and model.** Left at platform defaults, since
these are identity decisions rather than technical ones.

**6. Decide on Web Call enhanced security.** It is on by default, which requires a
viewer to sign into the org. That is right for production and probably wrong for a
reviewer opening a demo link — check the per-environment setting before recording.

**7. Publish the version.** It is a draft. Publish once `BRIDGE_BASE_URL` points
somewhere real, otherwise the tools will fail against the placeholder.

## Org state as found

Greenfield: one empty scratch workflow (`prueba`), no phone numbers, no SIP trunks, no
MCP servers, no knowledge bases, no Twin. Nothing was deleted or modified — the new
workflow was created alongside.

## Notes for whoever edits this next

- `update_workflow_nodes` replaces `configuration` wholesale. Fetch the current config
  with `get_node_details` first or you will silently wipe sibling fields.
- Paragraph fields (`url`, header values) need real Plate arrays on `create_workflow`;
  the string-template shorthand is only transformed on `update_workflow_nodes`.
- Webhook v2 bodies go in `body.raw` as a JSON string with `{{$var:<node_id>.<param>}}`
  tokens, using the **tool node's** persistent id — not the action node's.
- Environment variables interpolate as `{{ "env.KEY" }}` and are stored as a `variable`
  node with `group_id: "env"`. That syntax is confirmed working here.
