# Deploy and connect to the platform

Goal: get the bridge reachable over HTTPS so the HappyRobot workflow can call it,
then point the workflow at it and place a real voice call.

---

## Before you start: one constraint that matters

**Run exactly one replica.** Call state — the live OTP, the negotiation round counter,
the stage a call has reached — lives in the process. A second replica would strand
calls mid-flight with an unknown `call_id`, and it fails *silently* from the carrier's
side: they would just be told to start over.

This is a deliberate first-iteration trade-off, not an oversight. These values live
minutes and the durable record is the audit trail. Externalising the store (Redis, or
Twin once it exists) is what unlocks scaling out, and it is a contained change —
`SessionStore` in `app/domain/sessions.py` is the only thing that would move.

The service logs this warning at startup whenever `ENV=production`.

---

## Option A — a tunnel (fastest, good for the demo video)

No deploy, no account. Publishes the container already running on your machine.

```bash
docker run -d --name hr-tunnel --network host \
  cloudflare/cloudflared:latest tunnel --url http://localhost:8000 --no-autoupdate

sleep 15 && docker logs hr-tunnel 2>&1 | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com'
```

Verify it from outside, with and without the token:

```bash
set -a && . ./.env && set +a
TUNNEL=https://your-tunnel.trycloudflare.com

curl -s -o /dev/null -w 'no token  -> HTTP %{http_code}\n' "$TUNNEL/health"
curl -s -o /dev/null -w 'with token -> HTTP %{http_code}\n' \
  -H "Authorization: Bearer $API_AUTH_TOKEN" "$TUNNEL/health"
```

You want `401` then `200`. Tear it down when you are finished:

```bash
docker rm -f hr-tunnel
```

Caveats: the URL changes every restart, and it is a public URL for as long as it is up.
Bearer auth is the only thing in front of it, so do not leave it running unattended.
ngrok works the same way (`ngrok http 8000`) if you prefer it.

---

## Option B — a real deploy

The image has no dependencies beyond the two upstreams, so any container host works.
Set `ENV=production` so the Swagger UI and OpenAPI schema stop being served.

### Google Cloud Run

```bash
PROJECT=your-project
gcloud run deploy carrier-bridge \
  --source . \
  --project "$PROJECT" \
  --region us-central1 \
  --allow-unauthenticated \
  --max-instances 1 \
  --set-env-vars "ENV=production,LOG_LEVEL=INFO,FMCSA_BASE_URL=https://mobile.fmcsa.dot.gov/qc/services,TMS_HOST=...,TMS_PORT=..." \
  --set-secrets "TMS_TOKEN=tms-token:latest,FMCSA_API_KEY=fmcsa-key:latest,API_AUTH_TOKEN=bridge-token:latest"
```

`--allow-unauthenticated` is about Cloud Run's own IAM layer, not ours — the service
still requires the bearer token on every route. `--max-instances 1` is the constraint
above. Put the three secrets in Secret Manager rather than `--set-env-vars`.

### Fly.io

```bash
fly launch --no-deploy
fly secrets set TMS_TOKEN=... FMCSA_API_KEY=... API_AUTH_TOKEN=... TMS_HOST=... TMS_PORT=...
fly scale count 1
fly deploy
```

### Any VM with Docker

```bash
scp -r . user@host:/srv/carrier-bridge
ssh user@host 'cd /srv/carrier-bridge && docker compose up -d --build'
```

Put a TLS terminator in front (Caddy is two lines) — the platform requires HTTPS.

### Slimming the image (optional)

The image is 277 MB because it installs `requirements-dev.txt` so the `tests` service
can share it. For production, install `requirements.txt` only and drop `COPY tests`.
Worth doing when the image starts getting pulled often; not worth it yet.

---

## Wiring the workflow

Once you have an HTTPS URL, in the platform editor:

**Editor:** https://platform.happyrobot.ai/fdefernandouriamedina/workflows/8l6gr42sfl9j/editor/eim2p49l43jg

1. **Settings → Variables**
   - `BRIDGE_BASE_URL` → your URL, **no trailing slash** (e.g. `https://abc.trycloudflare.com`)
   - `BRIDGE_TOKEN` → the `API_AUTH_TOKEN` value from your `.env`

   Type the token into the UI rather than pasting it somewhere it gets logged. It is
   already marked hidden, so it renders masked after saving.

   All ten webhook nodes reference these two variables, so this is the only place the
   URL appears.

2. **Test one node before publishing.** Open the `POST /v1/calls/start` node and use
   the node test button. A 201 with a `call_id` means the URL and token are right. A
   401 means the token is wrong; a timeout means the URL is not reachable.

3. **Publish the version.** It is currently a draft, so the web call trigger will not
   serve until you publish.

4. **Check the Web Call trigger's Enhanced Security setting.** It is on by default,
   which requires the visitor to sign into the org. Right for production, probably
   wrong for a reviewer opening your demo link — decide per environment.

5. **Place a call** from the trigger's test button and watch both sides:

   ```bash
   docker compose logs -f app
   ```

   You should see the same event sequence the demo script prints, driven by the voice
   agent instead of curl.

---

## Verifying the deployment

```bash
BASE=https://your-host ./scripts/demo.sh
```

`demo.sh` honours `BASE`, so it runs end to end against the deployed instance. It hits
the real TMS and books a real load, so treat a green run as proof the whole chain works.

Quick manual checks:

```bash
set -a && . ./.env && set +a
BASE=https://your-host

curl -s -o /dev/null -w 'live    %{http_code} (expect 204)\n' "$BASE/live"
curl -s -o /dev/null -w 'no auth %{http_code} (expect 401)\n' "$BASE/health"
curl -s -o /dev/null -w 'docs    %{http_code} (expect 404 in prod)\n' "$BASE/docs"
curl -s -H "Authorization: Bearer $API_AUTH_TOKEN" "$BASE/health" | python3 -m json.tool
```

`/health` reports TMS reachability and the running fault statistics — the single most
useful thing to look at after a deploy.

---

## Known limitations of a public deploy

Worth being explicit about, since the bridge is internet-facing once deployed:

| Limitation | Impact | Mitigation today |
|---|---|---|
| One replica only | No horizontal scaling | Documented above; externalise `SessionStore` to fix |
| No rate limiting | A leaked token could be hammered | Rotate `API_AUTH_TOKEN`; put a WAF or platform rate limit in front |
| OTP codes are written to the audit log | Anyone with log access can read a live code | Only true while delivery is mocked; wiring real SMS/email removes it |
| Single shared bearer token | No per-caller attribution or revocation | Fine for one platform caller; per-client keys if that changes |
| No request signing | A leaked token is sufficient to call the API | HTTPS plus token rotation is proportionate at this stage |

None of these block the demo. All of them are worth saying out loud before the customer
asks.
