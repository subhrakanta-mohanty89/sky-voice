# Sky Voice AI — Customer Support Backend (Twilio)

Production-grade Flask backend for a Twilio-powered customer support call
centre. Designed to be the single backend for **both web and mobile**
clients (the REST + WebSocket contract is JSON-only and platform-agnostic).

What it does:

- Lets the admin / agents register a softphone identity (web browser or
  native mobile) using the **Twilio Voice SDK**.
- Receives inbound calls to your purchased Twilio number and rings every
  available agent's softphone in parallel.
- Lets agents place outbound calls from the same softphone.
- Provides REST endpoints to **end / forward / transfer / hold / unhold**
  any live call.
- Streams real-time call updates to the admin UI over a WebSocket so the
  Active-Calls list, history, and badges all update instantly.
- Optionally records every conversation and stores the recording URL.

---

## 1. Credentials you need from Twilio

Sign up at https://www.twilio.com/try-twilio (free trial gives you
~$15 of voice credits and a free local number).

| Env var | Where to find it | Required? |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | Console dashboard → "Account Info" panel. Starts with `AC…`. | ✅ |
| `TWILIO_AUTH_TOKEN` | Same panel, under the SID. Click the eye icon to reveal. | ✅ |
| `TWILIO_API_KEY_SID` | [Account → API Keys & Tokens → "Create API Key"](https://console.twilio.com/us1/account/keys-credentials/api-keys). Type = **Standard**. Starts with `SK…`. | ✅ |
| `TWILIO_API_KEY_SECRET` | Shown **once** when you create the API key — copy it immediately. | ✅ |
| `TWILIO_TWIML_APP_SID` | [Voice → Manage → TwiML Apps → "Create new TwiML App"](https://console.twilio.com/us1/develop/voice/manage/twiml-apps). Set its **Voice → Request URL** to `{PUBLIC_BASE_URL}/twilio/voice/outgoing` (POST). Starts with `AP…`. | ✅ |
| `TWILIO_CALLER_ID` | The phone number you purchased ([Phone Numbers → Manage → Active numbers](https://console.twilio.com/us1/develop/phone-numbers/manage/incoming)). E.164 format, e.g. `+14155550100`. Configure its **Voice → "A Call Comes In"** webhook to `{PUBLIC_BASE_URL}/twilio/voice/incoming` (POST). | ✅ |
| `PUBLIC_BASE_URL` | The publicly reachable URL of this backend. In dev, run `ngrok http 5050` and use the `https://…ngrok-free.app` URL. In prod, your domain. | ✅ |
| `APP_SECRET_KEY` | Any long random string. Generate with `python -c "import secrets; print(secrets.token_urlsafe(48))"`. | recommended |
| `FALLBACK_FORWARD_NUMBER` | Optional PSTN fallback when no agent answers. | optional |
| `RECORD_CALLS` | `true` / `false`. | optional |
| `VALIDATE_TWILIO_SIGNATURE` | `true` (default) / `false`. Strongly recommended in prod. | optional |

> **API Key vs Auth Token**: We use API Keys (`SK…` + secret) instead of
> the raw Auth Token to mint JWTs. The Auth Token is still required to
> validate inbound webhook signatures. Both are mandatory.

### Twilio console wiring (one-time)

1. **Buy a phone number** that supports Voice in the country you operate.
2. Open that number's settings → scroll to **Voice & Fax**:
   - "A Call Comes In" → **Webhook** → `https://your-public-url/twilio/voice/incoming` → **HTTP POST**
   - "Call Status Changes" (optional) → `https://your-public-url/twilio/voice/status` → **HTTP POST**
3. Create a **TwiML App** (Voice → Manage → TwiML Apps):
   - Voice → "Request URL" → `https://your-public-url/twilio/voice/outgoing` → **HTTP POST**
   - Save and copy the App SID into `TWILIO_TWIML_APP_SID`.
4. Create an **API Key** (Account → API Keys) of type **Standard**.
   - Copy `SID` → `TWILIO_API_KEY_SID`
   - Copy `Secret` → `TWILIO_API_KEY_SECRET` (only shown once!)

---

## 2. Setup

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env       # then fill in the Twilio creds above
python run.py              # → listens on http://localhost:5050
```

In a second terminal, expose the dev server publicly so Twilio can reach it:

```bash
ngrok http 5050
# → copy the https URL into PUBLIC_BASE_URL in .env, then restart `python run.py`
```

---

## 3. API surface

All JSON. CORS opens for the origins listed in `ALLOWED_ORIGINS`. Works
for any client — the same calls work from the web frontend or a mobile
app (Swift / Kotlin / React Native).

### User auth (login / signup / team)

All routes are under `/api/v1/auth`. Most endpoints return
`{ "success": false, "error": "<code>" }` on failure; the frontend
maps each code to a human message via `src/services/http.ts`.

| Method | Path | Body | Auth | Returns |
|---|---|---|---|---|
| POST | `/signup` | `{ email, password, fullName, phone?, organization? }` | — | `{ success, user, token, expires_at }` |
| POST | `/login` | `{ email, password }` | — | `{ success, user, token, expires_at }` |
| POST | `/logout` | — | Bearer | `{ message: "logged_out" }` |
| GET  | `/me` | — | Bearer | `{ user }` |
| PATCH | `/profile` | `{ fullName?, phone?, organization?, avatarInitials? }` | Bearer | `{ user }` |
| POST | `/change-password` | `{ currentPassword, newPassword }` | Bearer | `{ message }` |
| DELETE | `/account` | — | Bearer | `{ message }` — refuses to delete the last admin |
| POST | `/forgot-password` | `{ email }` | — | `{ message, reset_token_dev }` — dev-mode returns a 15-min JWT inline so you can wire the reset flow without email |
| POST | `/reset-password` | `{ token, newPassword }` | — | `{ message }` |
| GET  | `/team` | — | Bearer | `{ team: User[] }` |
| POST | `/team` | `{ email, fullName, role, phone?, password? }` | Bearer + admin | `{ user, temporary_password }` |
| PATCH | `/team/<user_id>` | `{ status?, role?, fullName?, phone? }` | Bearer + admin | `{ user }` |
| DELETE | `/team/<user_id>` | — | Bearer + admin | `{ message }` — refuses last admin |

Passwords are PBKDF2-SHA256 with 200k rounds. Sessions are stateless
HS256 JWTs signed with `APP_SECRET_KEY` (TTL configurable via
`AUTH_JWT_TTL`, default 7 days). The first user to sign up is auto
promoted to admin when `FIRST_USER_IS_ADMIN=true`.

> The user store is in-memory for this build — restarting `python run.py`
> wipes all users. Wire it to a real database when you add persistence.

### Voice SDK access token (require_auth)

| Method | Path | Body | Returns |
|---|---|---|---|
| POST | `/api/v1/token` | — (identity derived from the Bearer JWT) | `{ token, identity, expires_in }` — pass `token` to the Twilio Voice SDK. |

### Agents

| Method | Path | Purpose |
|---|---|---|
| GET    | `/api/v1/agents` | List all registered agents. |
| POST   | `/api/v1/agents` | Register or update an agent. Body `{ identity, name, role, status }`. |
| PATCH  | `/api/v1/agents/<identity>` | Update fields (status, name, …). |
| DELETE | `/api/v1/agents/<identity>` | Remove (admin cannot be removed). |

Agent statuses: `available` / `busy` / `away` / `offline`. Inbound calls are
ringed to every `available` agent in parallel.

### Calls

| Method | Path | Purpose |
|---|---|---|
| GET   | `/api/v1/calls` | List active calls. |
| GET   | `/api/v1/calls/history?limit=50` | List past calls. |
| GET   | `/api/v1/calls/<sid>` | Single call detail. |
| POST  | `/api/v1/calls` | Place outbound call. Body `{ to, agent_identity? }`. |
| POST  | `/api/v1/calls/<sid>/hangup` | End the call. |
| POST  | `/api/v1/calls/<sid>/forward` | Forward live call to PSTN. Body `{ to }`. |
| POST  | `/api/v1/calls/<sid>/transfer` | Transfer to another agent. Body `{ agent_identity }`. |
| POST  | `/api/v1/calls/<sid>/hold` / `/unhold` | Pause / resume a leg. |

### Twilio webhooks (called by Twilio, not your app)

| Method | Path | Purpose |
|---|---|---|
| POST | `/twilio/voice/incoming` | Customer dials your support number. |
| POST | `/twilio/voice/outgoing` | Voice SDK softphone places an outbound call. |
| POST | `/twilio/voice/status` | Lifecycle updates (initiated/ringing/answered/completed). |
| POST | `/twilio/voice/dial-status` | Fired when an inbound `<Dial>` finishes. |
| POST | `/twilio/voice/recording` | Recording-ready event (only if `RECORD_CALLS=true`). |

### Real-time (WebSocket)

`ws://localhost:5050/ws/admin?token=<JWT>` (or `wss://…` over TLS).

Authentication is mandatory. Pass the user's Bearer JWT via the `token`
query parameter (browsers can't set headers on the WS upgrade). An
invalid or missing token replies with `{event:"error", payload:{error:"unauthenticated"}}`
and closes the socket.

On connect you receive a snapshot that includes the authenticated user:
```json
{ "event": "snapshot", "payload": { "active_calls": [...], "agents": [...], "user": {...} } }
```

After that, every state change is pushed as `{ event, payload }`:
- `call.incoming`, `call.initiated`, `call.answered`, `call.status`,
  `call.ended`, `call.forwarded`, `call.transferred`, `call.held`,
  `call.unheld`, `call.recording_ready`
- `agent.upserted`, `agent.updated`, `agent.removed`

### Legacy `/api/*` shim (for the existing web frontend)

The current `frontend/src/services/api.ts` was written against the older
Plivo backend. The following routes are kept stable so the UI keeps
working without immediate changes:

`/api/make-call`, `/api/active-calls`, `/api/call-history`,
`/api/answer-call/<id>`, `/api/answer-inbound/<id>`, `/api/end-call/<id>`,
`/api/hold-call/<id>`, `/api/unhold-call/<id>`,
`/api/forward-call/<id>`, `/api/send-message/<id>`.

Migrate to the v1 endpoints when convenient — they expose richer data and
new features (transfer, recordings, multi-agent).

---

## 4. How it works (mental model)

```
                                ┌─────────────────────────────┐
   Customer dials +14155550100  │   Twilio Programmable Voice │
   ───────────────────────────► │   (cloud)                   │
                                └────────────┬────────────────┘
                                             │ POST /twilio/voice/incoming
                                             ▼
                          ┌────────────────────────────────────┐
                          │   Sky Voice AI Backend            │
                          │   - decides who to ring            │
                          │   - returns TwiML <Dial><Client>… │
                          │   - tracks state, broadcasts WS    │
                          └────────┬───────────────┬───────────┘
                                   │               │
                  TwiML response   │               │ WebSocket events
                                   ▼               ▼
                           ┌──────────────┐  ┌────────────────┐
                           │   Twilio     │  │  Admin UI      │
                           │   bridges    │  │  (web/mobile)  │
                           │   audio via  │  │  shows call,   │
                           │   <Client>   │  │  Accept/Reject │
                           └──────┬───────┘  └────────┬───────┘
                                  │                   │
                                  ▼                   ▼
                      ┌─────────────────────────────────────────┐
                      │  Voice SDK in browser / iOS / Android   │
                      │  - mic + speaker audio                  │
                      │  - registers as identity "admin"        │
                      └─────────────────────────────────────────┘
```

For **outbound** calls the flow is mirrored: the SDK on the agent's device
calls `Device.connect({ params: { To: "+1…" } })`. Twilio fetches the
TwiML App's voice URL → our `/twilio/voice/outgoing` → we return
`<Dial><Number>+1…</Number></Dial>` and audio is bridged.

For **forward to softphone** there is nothing special — the agent's
softphone *is* a softphone, registered under their identity. To "forward
to a different agent" we issue a Modify-Call REST request that swaps the
TwiML to `<Dial><Client>other_agent</Client></Dial>`.

---

## 5. Web / mobile client integration

### Web (npm)

```bash
npm install @twilio/voice-sdk
```

```ts
import { Device } from "@twilio/voice-sdk";

// 1. Get a token from the backend
const { token } = await fetch("/api/v1/token", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ identity: "admin" }),
}).then(r => r.json());

// 2. Register the softphone
const device = new Device(token, { logLevel: 1 });
await device.register();

// 3. Place an outbound call
const call = await device.connect({ params: { To: "+14155550100" } });

// 4. Receive an incoming call
device.on("incoming", (call) => {
  // ring UI… on Accept:
  call.accept();
});
```

### iOS / Android / React Native

- iOS:    https://github.com/twilio/voice-quickstart-ios
- Android: https://github.com/twilio/voice-quickstart-android
- React Native: `@twilio/voice-react-native-sdk`

Same backend endpoint (`/api/v1/token`) issues their access tokens.

---

## 6. What's not yet implemented (TODO)

- Real call queueing (TaskRouter) — current routing fans out to all
  available agents in parallel; that's good enough for small teams.
- Live audio transcription (Twilio Media Streams + Deepgram) — endpoint
  shape is reserved (`/api/send-message/<sid>`) but currently echoes back.
- Persistent storage — Calls and Agents live in memory and reset on
  restart. Swap `app/models.py` for a SQLAlchemy/Redis-backed store when
  you're ready.
- User authentication on the API itself — for now anyone with network
  access can call `/api/*`. Add JWT/OAuth2 if exposing this beyond an
  internal network.

---

## 7. Useful Twilio docs

- Voice SDK overview: https://www.twilio.com/docs/voice/sdks
- Access Tokens: https://www.twilio.com/docs/iam/access-tokens
- Modify Call: https://www.twilio.com/docs/voice/api/call-resource#update-a-call-resource
- Webhooks reference: https://www.twilio.com/docs/voice/twiml
