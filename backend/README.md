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

Insurance and money come from what the member enters (or the sample card). The medical record (visit note,
orders, labs, doctors) is a dummy FHIR record for everyone. Text like the booking call is filled in with the
member's name, payer and copays.

## Endpoints by screen

| Screen | Call | Notes |
|---|---|---|
| Sign up | `POST /api/signup` `{name, dob, email, phone}` | |
| Plan search | `GET /api/plans?q=aet` | Payer suggestions, plan types and the optional benefit fields. Any payer name is accepted |
| **Enter your insurance** | `POST /api/insurance` `{payer, planName, planType, memberName, memberId, groupNumber, coverageSource, premiumMonthly, benefits: {deductible, oopMax, specialistCopay, deductibleMet, ...}}` | Only `payer` is required. Anything skipped uses typical values for the plan type and is listed in `estimatedFields`. `nameMatchesAccount` flags a card/account name mismatch |
| Upload plan documents | `POST /api/insurance/document` multipart `file` | Summary of Benefits (PDF/text). AI reads the benefits and replaces the typical values. Returns `updatedFields` |
| Use sample card | `POST /api/card/scan` | Loads the sample Blue Ridge plan (Maya), for judges who don't want to type anything |
| Link sign-in | `GET /api/connections` → `sources.plan` / `sources.portal` | `scopes` = the "wants read-only access to" list, `via` = the fine print |
| Allow | `POST /api/connections/plan`, `POST /api/connections/portal` | Simulated SMART on FHIR sign-in |
| Authorize | `POST /api/consent/authorized-rep` | New onboarding step: member e-signs a HIPAA authorization so the coordinator can call offices, talk to the plan and dispute bills for them. `readyToFinish` requires it |
| Found doctors | `GET /api/connections` → `foundFromClaims`, `readyToFinish` | Only the portal is linkable; pharmacy + imaging come from claims |
| Today | `GET /api/today` | `coordinator.pendingApprovals` drives the "Review 1 approval" button and the Care tab badge |
| Visit summary | `GET /api/visits/latest` | `summary` is written by the AI from the visit note. Each step has `status` (`booked`/`ordered`/`needs_approval`/`in_progress`) and `channel` (`phone_call`/`pharmacy`/`portal_message`) |
| Approve PT | `GET /api/visits/latest/pt`, then `POST /api/visits/latest/pt/approve` `{autoApprove}` | After approving, poll `GET /api/visits/latest/pt` every 2s. `timeline[].state` = `done`/`busy`/`waiting`; `stage` 5 = booked |
| Call replay | `GET /api/calls/derm` | `lines[].speaker` = `coordinator`/`front_desk` |
| Coverage | `GET /api/coverage` | `meters`, `alerts`, `documents` |
| Upload | `POST /api/documents` multipart `file` | PDF/text get read and used by the chat; photos are just saved |
| Ask about costs | `GET /api/chat` (history + suggestions), `POST /api/chat` `{message}` → `{reply, source}` | `reply` uses `**bold**` and `- ` bullets, same as the demo's `mdlite()`. ~1-2s. `source: "fallback"` = canned answer because Bedrock was down |
| Year in Care | `GET /api/recap` | |
| Lab results | `GET /api/results` | `status: pending` until `POST /api/demo/results-in` (demo fast-forward). Then: results with ranges/flags, trend, AI plain-English explanation, doctor's comment. `GET /api/today` gets a `newResults` card |
| Cost estimates | `GET /api/estimates`, `GET /api/estimates/{serviceId}` | Computed by the cost engine from the member's plan, with a line-by-line breakdown per provider. The chat quotes these |
| Raw record | `GET /api/fhir` | FHIR R4 Bundle everything is built from |
| LLM usage | `GET /api/metrics` | Bedrock calls, input/output tokens by purpose, chat cache hits, timeouts |

## Token budget

- Chat sends only what the question needs: the estimates for the services asked about, the plan comparison
  only for plan questions, and the 2 best-matching chunks of uploaded documents (not the whole document).
- Identical prompts share one cached answer (`source: "cache"`), so judges tapping the same suggestions cost one call.
- Visit summary and lab explanation are generated once per server start; plan-document extraction is cached by file hash.
- gpt-oss runs with low reasoning effort for chat, and every call has a hard deadline (12s for chat) with a
  cost-engine fallback, so a slow Bedrock never hangs the demo.
- Sessions expire after 12h idle (max 1000 in memory).
