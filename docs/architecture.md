# Architecture

## Components

| Layer | Technology | Entry point |
| --- | --- | --- |
| UI | React 18, Vite, TypeScript, Tailwind, shadcn-style primitives, Zustand, Axios | `frontend/src/pages/KycPage.tsx` |
| API | FastAPI, Pydantic v2 | `backend/app/main.py` |
| Agent | LangGraph state machine + LangChain-core providers | `backend/app/agents/graph.py` |
| Persistence | SQLAlchemy 2.0, Alembic, Postgres/SQLite | `backend/app/db/` |
| Policy RAG | ChromaDB or deterministic local BM25 | `backend/app/services/policies.py` |
| Cache / idempotency | Redis or in-memory TTL | `backend/app/services/cache.py` |
| Events | In-process SSE `EventHub` | `backend/app/services/events.py` |
| Evaluation | Pytest-style scenario harness | `backend/app/evaluation/` |

## The live-state contract

The agent must never guess what is on screen. The frontend owns the source of
truth and publishes a `ScreenContext` snapshot whenever anything meaningful
changes:

- every keystroke on a field,
- verification/upload/mandate status changes,
- field-level and screen-level errors,
- live state/city suggestion options.

`buildScreenContext()` in `frontend/src/lib/fields.ts` selects only the fields
belonging to the active screen, so a value entered on another step is never
leaked into the current snapshot. `POST /api/screen-context/update` stores the
snapshot (with **masked** values — see [security.md](security.md)) and bumps a
revision counter. `GET /api/screen-context` returns the latest revision. The
agent reads that same stored snapshot; there is no second, hardcoded notion of
screen state.

`KycPage` also subscribes to the session SSE stream and treats incoming
`context` events as authoritative, so an action that changes the server-side
state is reflected in the UI.

## Agent graph

The LangGraph graph is a bounded loop:

```
ingest_context → tokenise → retrieve_policies → plan → guard → dispatch → finish
                    ▲                                       │
                    └────────── replan (max 2) ─────────────┘
```

1. **ingest_context** loads the latest screen context plus domain state
   (PAN status, document OCR result, mandate attempts/status).
2. **tokenise** replaces sensitive values with format-preserving surrogates and
   builds a `ToolContext` containing both the masked context (safe for the
   model) and a reference to the vault (used only by guarded tools).
3. **retrieve_policies** returns policy docs and refs from Chroma or the local
   retriever. v2 plans are required to cite refs.
4. **plan** asks the configured provider for a `AgentDecision` — strict Pydantic
   JSON with `say`, `tools`, `cited_policy_refs`, `confidence`, and an optional
   confirmation prompt. Invalid JSON or schema failures raise `LLMError` and
   fall back to the deterministic mock planner when provider is `auto`.
5. **guard** validates each tool call against:
   - screen-specific allowed action types (`ACTION_WHITELIST`),
   - bounded loop/replan limits,
   - the kill switch,
   - fill-value safety (no invented characters),
   - confirmation requirements for submit/verify/financial retry.
6. **dispatch** persists `AgentAction` rows, emits SSE frames, and sets run
   status to `completed`, `awaiting_result`, or `awaiting_confirmation`.

The planner is versioned. `v1` omits policy citations, re-uploads documents on
OCR issues, and retries mandates without policy grounding. `v2` adds the OCR
gate, citations, highlight-before-fill, and policy-aware retry. Guardrails are
identical for both versions.

## Action protocol

Actions are the only way the agent can affect the UI. The vocabulary is:

| Action | Effect | Confirmation |
| --- | --- | --- |
| `highlight` | Pulse a field / upload area | immediate |
| `fill` | Set an input value | immediate (value fetched separately) |
| `upload` | Trigger the upload control | immediate |
| `retry` | Retry a step (e.g. mandate) | **always** for financial retries |
| `navigate` | Move to another allowed screen | immediate |
| `escalate` | Open a human-review ticket | immediate |

The value of a sensitive fill is **never** placed in an SSE frame, audit log, or
action-result payload. The flow is:

1. Backend stores the value encrypted in the action row with a SHA-256 hash.
2. SSE emits only a masked `preview` (e.g. `••••••••F`).
3. Frontend calls `GET /api/agent/actions/{id}/value?session_id=…` to obtain the
   plaintext for the DOM input.
4. Frontend calls `POST /api/agent/action-result` with `executed_value_hash`.
5. Backend compares the hash; a mismatch returns HTTP 200 with `ok: false` and
   `action_status: "failed"` — the action is not marked applied.

A final `db.flush()` before run-status evaluation ensures the last action's
status is visible when deciding whether the run is terminal.

## Mandate idempotency

`POST /api/kyc/mandate/submit` requires an `idempotency_key`. Reusing a key
returns the original `mandate_ref` with `idempotent_replay: true` and does not
create a second mandate or consume a retry. The key is stored in Redis when
available, otherwise in a process-local TTL map. Attempts are capped (default
3); further failures trigger escalation rather than another charge attempt.

## Persistence

`create_all` is used in development; `alembic upgrade head` is the production
path (`backend/migrations/`). `backend/app/db/seed.py` idempotently loads 11
policy documents and the 32 evaluation scenarios on startup or via `make seed`.

## Known limitation: event fan-out

`EventHub` is process-local. Multiple uvicorn replicas will not share SSE
events. Redis pub/sub is the natural next step; idempotency already scales with
Redis. This is documented rather than hidden — see README §9.
