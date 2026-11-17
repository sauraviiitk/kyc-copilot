# API reference

Base URL: `http://localhost:8000` in development. In Docker, the frontend nginx
proxies `/api` and `/health` to the backend service; public UI is on port 8080.
Interactive OpenAPI docs are at `/docs`.

All request/response bodies are JSON. Session-scoped endpoints take a
`session_id`; agent streams require it as a query parameter.

## System

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/health` | Readiness, effective provider/model, policy backend, Redis mode, schema mode |
| `GET` | `/metrics` | In-process counters/latency histograms (no PII) |
| `POST` | `/admin/kill-switch` | Enable/disable agent action dispatch (`ADMIN_TOKEN`) |
| `GET` | `/admin/kill-switch` | Read current kill-switch state |

Example `/health` (offline defaults):

```json
{
  "status": "ok",
  "provider": "mock",
  "model": "deterministic-planner",
  "policy_backend": "local-bm25",
  "checks": { "database": {"ok": true}, "redis": {"mode": "in-memory"}, "llm": {"effective": "mock"} }
}
```

## Sessions

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/sessions` | Create a session (`consent_given`, optional `external_ref`); returns `session_id` |
| `GET` | `/api/sessions/{session_id}` | Session metadata/status |
| `GET` | `/api/sessions/{session_id}/events` | Replay session event history (SSE frames) |
| `PATCH` | `/api/sessions/{session_id}` | Update consent / session state |

## Screen context

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/screen-context/update` | Publish the frontend's live snapshot (`session_id`, `context`, `reason`); returns new `revision` |
| `GET` | `/api/screen-context?session_id=…` | Latest snapshot + revision |

The `context` body contains `screen_id`, `fields[]` (`name`, `value`,
`required`, `pattern`, `error`, `options`, `sensitive`), `errors[]`, `status`,
and optional `document`/`mandate` objects. Values arriving here are masked and
tokenised server-side — see [security.md](security.md).

## KYC domain actions

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/kyc/pan/verify` | Verify PAN (`session_id`, `full_name`, `pan`); returns status, attempts, `next_screen` |
| `POST` | `/api/kyc/document/upload` | Upload/register a document; returns deterministic mock OCR fields |
| `POST` | `/api/kyc/document/accept` | Accept OCR values / confirm document |
| `POST` | `/api/kyc/mandate/submit` | Submit mandate with `idempotency_key`; replay returns original `mandate_ref` |
| `POST` | `/api/kyc/mandate/retry` | Retry with the same idempotency key; capped attempts |
| `GET` | `/api/kyc/mandate/{session_id}` | Masked mandate status (`masked_account`, attempts, retryable) |
| `POST` | `/api/kyc/address/verify` | Verify address fields |
| `GET` | `/api/kyc/suggestions?query=…` | Deterministic state/city suggestions |

Representative mandate response:

```json
{
  "mandate_ref": "MND-21393AE1AA",
  "status": "verified",
  "attempts": 1,
  "max_attempts": 3,
  "idempotency_key": "smoke-key-1",
  "idempotent_replay": false,
  "retryable": false,
  "requires_confirmation": false
}
```

## Agent

| Method | Path | Description |
| --- | --- | --- |
| `POST` | `/api/agent/run` | Start a run (`session_id`, `message`, optional `prompt_version` v1/v2); returns `run_id`, `stream_url` |
| `GET` | `/api/agent/stream/{run_id}?session_id=…` | SSE event stream / replay |
| `GET` | `/api/agent/runs/{run_id}` | Run detail with actions and statuses |
| `GET` | `/api/agent/actions/{action_id}/value?session_id=…` | Fetch a sensitive fill's plaintext + `value_hash` for DOM attestation |
| `POST` | `/api/agent/action-result` | Report execution: `status`, `executed_value_hash`, optional `detail` |

`POST /api/agent/run` returns immediately (queued). The run executes in a
thread pool; connect to the stream URL to follow progress. `prompt_version`
defaults to the frontend toggle (v2).

### SSE event names

Use `EventSource.addEventListener(name, handler)` — these are **named** events,
not the default `message` event.

| Event | Payload highlights |
| --- | --- |
| `agent_status` | `status` (queued/planning/executing/…) |
| `context` | masked live screen snapshot |
| `policy` | `refs` retrieved for the plan |
| `agent_plan` | `plan`, `confidence` |
| `action` | `action_id`, `type`, `field`, `preview`, `sensitive` — **no raw value** |
| `action_result` | action status update |
| `agent_message` | `say` (masked text), `masked: true` |
| `agent_complete` | `status`, `confidence`, `requires_confirmation`, `confirmation_prompt`, `degraded` |
| `error` | error detail (redacted) |
| `stream_end` | `reason` — stream closed by the client |

A run may finish as `completed`, `awaiting_result` (actions dispatched, waiting
for the client), or `awaiting_confirmation` (financial retry pending user
confirmation). The stream emits `stream_end` once `agent_complete` is in the
history or the run is terminal.

### Attestation behaviour

`POST /api/agent/action-result` normally returns HTTP 200. If
`executed_value_hash` does not match the stored hash, the response is still 200
but contains `ok: false` and `action_status: "failed"`; the action is not
applied. HTTP 409 is reserved for protocol/session errors (unknown action,
wrong session).

## Audit & evaluation

| Method | Path | Description |
| --- | --- | --- |
| `GET` | `/api/audit` | Audit log for a session/run (redacted) |
| `GET` | `/api/audit/{run_id}` | Events for one agent run |
| `GET` | `/api/audit/escalations/all` | Human escalation queue |
| `GET` | `/api/eval/scenarios` | List the 32 seeded scenarios |
| `POST` | `/api/eval/runs` | Start an evaluation run (202) |
| `GET` | `/api/eval/runs` | Past runs |
| `GET` | `/api/eval/latest` | Latest run + comparison summary (falls back to report file) |
| `GET` | `/api/eval/runs/{run_id}` | One run with per-scenario results |

## Error shape

Validation errors use FastAPI's default `detail` array. Domain errors use a
structured object:

```json
{ "detail": { "code": "no_eval_runs", "message": "No evaluation runs yet." } }
```

Error messages never include raw PII; domain services redact values before
constructing responses and logs.
