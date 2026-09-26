# Care Coordinator API

FastAPI backend for the demo. All people, providers, prices and plan details are sample data.

- Local: `http://localhost:8000` · Interactive docs: `/docs`
- Run: `cd backend && .venv/bin/uvicorn app.main:app --reload --port 8000`
- Needs `backend/.env` with `AWS_BEARER_TOKEN_BEDROCK`, `AWS_REGION`, `BEDROCK_MODEL_ID`.

## Sessions

Send an `X-Session-Id` header on every request so each person trying the demo gets their own state.
Generate it once and keep it in localStorage:

```js
const SID = localStorage.sid || (localStorage.sid = crypto.randomUUID());
const api = (path, opts = {}) => fetch(API + path, {
  ...opts, headers: {'X-Session-Id': SID, 'Content-Type': 'application/json', ...opts.headers}
}).then(r => r.json());
```

(For file uploads, don't set `Content-Type`; let the browser set the multipart boundary.)
"Reset demo" = `POST /api/reset`.

## Endpoints by screen

| Screen | Call | Notes |
|---|---|---|
| Sign up | `POST /api/signup` `{name, dob, email, phone}` | |
| Plan search | `GET /api/plans?q=blue` | |
| Card scan | `POST /api/card/scan` (optional multipart `file`) | Always returns the sample Blue Ridge card |
| Link sign-in | `GET /api/connections` → `sources.plan` / `sources.portal` | `scopes` = the "wants read-only access to" list, `via` = the fine print |
| Allow | `POST /api/connections/plan`, `POST /api/connections/portal` | Simulated SMART on FHIR sign-in |
| Authorize | `POST /api/consent/authorized-rep` | New step, see below |
| Found doctors | `GET /api/connections` → `foundFromClaims`, `readyToFinish` | Only the portal is linkable; pharmacy + imaging come from claims |
| Today | `GET /api/today` | `coordinator.pendingApprovals` drives the "Review 1 approval" button and the Care tab badge |
| Visit summary | `GET /api/visits/latest` | `summary` is written by the AI from the visit note. Each step has `status` (`booked`/`ordered`/`needs_approval`/`in_progress`) and `channel` (`phone_call`/`pharmacy`/`portal_message`) |
| Approve PT | `GET /api/visits/latest/pt`, then `POST /api/visits/latest/pt/approve` `{autoApprove}` | After approving, poll `GET /api/visits/latest/pt` every 2s. `timeline[].state` = `done`/`busy`/`waiting`; `stage` 5 = booked |
| Call replay | `GET /api/calls/derm` | `lines[].speaker` = `coordinator`/`front_desk` |
| Coverage | `GET /api/coverage` | `meters`, `alerts`, `documents` |
| Upload | `POST /api/documents` multipart `file` | PDF/text get read and used by the chat; photos are just saved |
| Ask about costs | `GET /api/chat` (history + suggestions), `POST /api/chat` `{message}` → `{reply, source}` | `reply` uses `**bold**` and `- ` bullets, same as the demo's `mdlite()`. ~1-2s. `source: "fallback"` = canned answer because Bedrock was down |
| Year in Care | `GET /api/recap` | |
| Raw record | `GET /api/fhir` | FHIR R4 Bundle everything is built from |
