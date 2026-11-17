# KYC Copilot

A production-minded KYC assistance copilot. It watches the **live** state of a
multi-screen KYC form, explains what is wrong in plain language, and proposes a
small, bounded set of corrective actions that the **frontend actually executes**.
Sensitive identity data is tokenised before any model call, masked in logs and
events, and only rehydrated inside the browser at the moment a field is filled.

The repository ships with:

- A React + Vite + TypeScript frontend with four working KYC screens.
- A FastAPI + LangGraph backend with strict Pydantic plans and guarded tools.
- A deterministic offline mode that runs with **no API keys and no network**.
- A real v1/v2 evaluation harness (32 scenarios) with measured metrics and
  persisted reports — no fabricated numbers.

> **Status:** the app runs fully offline out of the box. Supabase/Postgres and a
> Gemini/Groq key are optional upgrades, not prerequisites.

---

## 1. Quickstart (no keys, no network)

Requirements: Python 3.12, Node 20+.

```bash
# Backend
cd backend
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt

# Frontend (separate terminal)
cd frontend
npm install
```

Start both dev servers (two terminals):

```bash
# Terminal 1 — API on http://localhost:8000
cd backend && uvicorn app.main:app --reload --port 8000

# Terminal 2 — UI on http://localhost:5173
cd frontend && npm run dev
```

Open <http://localhost:5173>, click **Start KYC**, then **Load demo profile**.
The lowercase PAN + verification error is intentional: ask the copilot
*"my PAN is being rejected, help me fix it"* and watch it highlight and fill the
field. Nothing is submitted until you press **Verify PAN** yourself.

Windows users can use `.\tasks.ps1 <target>` instead of `make` (see §5).

---

## 2. What the demo proves

| Concern | Where to look |
| --- | --- |
| Live screen state, not hardcoded | `frontend/src/lib/fields.ts`, `POST /api/screen-context/update` |
| Strict JSON plans, bounded loops | `backend/app/schemas/__init__.py`, `backend/app/agents/graph.py` |
| Real frontend-executed actions only | `frontend/src/components/CopilotPanel.tsx` |
| PII never reaches the model/logs | `backend/app/services/pii.py`, `backend/tests/test_pii.py` |
| No invented PAN characters | `backend/app/services/pii.py::rehydrate_with_proposed_transform` |
| No duplicate mandates | `backend/app/services/mandate.py` (idempotency key) |
| Real v1 vs v2 measurement | `evals/scenarios/`, `evals/reports/latest.json` |

Interactive API docs: <http://localhost:8000/docs>.

---

## 3. Architecture

```
Browser                         FastAPI backend
───────                         ────────────────
KycPage ──buildScreenContext──▶ POST /api/screen-context/update
   │                                     │
   │  publish masked snapshot            ▼
   │                            ScreenContext row (surrogates only)
   │
CopilotPanel ──POST /api/agent/run───────▶ LangGraph agent
   │                                     │  tokenise → retrieve policies →
   │  SSE /api/agent/stream/{run_id} ◀────┤  plan (Pydantic) → guarded tools
   │  (named events, no raw values)       ▼
   │                            AgentAction rows (ciphertext + hash)
   │
   ├─ GET  /actions/{id}/value ──────────▶ decrypt vault, return value + hash
   ├─ apply to DOM input (fill) / upload / retry
   └─ POST /api/agent/action-result ─────▶ attest with value_hash, close loop
```

Key design points:

- **Screen context is pushed by the frontend** on every meaningful change
  (typing, errors, status, document/mandate state). The backend never guesses
  what is on screen. See [docs/architecture.md](docs/architecture.md).
- **The model only sees surrogates** such as `⟦PAN_7299⟧`. Fill proposals are
  accepted only if they are a case/separator transform of the surrogate, a
  vault token, or a literal value the user already typed. Anything else is
  flagged as a hallucination and refused.
- **Actions are a closed vocabulary**: `highlight`, `fill`, `upload`, `retry`,
  `navigate`, `escalate`. No arbitrary JavaScript, no DOM access from the model.
- **Sensitive submit/retry is gated.** Fills apply immediately; submit/verify
  and any financial retry require an explicit user action. Retrying a bank
  mandate always starts as `awaiting_confirmation`.
- **SSE uses named events** (`agent_status`, `context`, `policy`, `agent_plan`,
  `action`, `action_result`, `agent_message`, `agent_complete`, `error`,
  `stream_end`). Use `addEventListener`, not `onmessage`.
- **Redis and ChromaDB are optional.** Without Redis a process-local TTL cache
  handles idempotency; without Chroma a deterministic BM25-style retriever
  returns the same policy refs. See [docs/security.md](docs/security.md).

---

## 4. Evaluation (real numbers)

The harness runs both prompt versions over the same 32 scenarios across four
categories (PAN, OCR, MANDATE, INTENT) using a headless client that executes the
same action protocol as the UI. The mandate gateway failure rate is forced to
`0.0` during evaluation for determinism and restored afterwards.

Latest measured run (`baseline v1 vs v2`, mock/deterministic planner):

| Metric | v1 | v2 |
| --- | ---: | ---: |
| Pass rate | 9.4% (3/32) | **100% (32/32)** |
| p50 / p95 latency | 14 / 27 ms | 14 / 26 ms |
| Tool success | 100% | 100% |
| Hallucinated actions | 0% | 0% |
| Policy grounding | 0% | **100%** |
| Total cost (est.) | $0.023982 | $0.028930 |

v1 fails mainly because it skips policy citations, re-uploads documents that
should only be highlighted, and retries mandates without policy grounding. v2
adds an OCR gate, policy citations, highlight-before-fill, and policy-aware
retry. The hard safety guardrails (no invented PAN, no auto-submit, no duplicate
mandate) are version-independent and hold in both.

Full report: [`evals/reports/latest.md`](evals/reports/latest.md),
[`evals/reports/latest.json`](evals/reports/latest.json).
Methodology: [docs/evaluation.md](docs/evaluation.md).

---

## 5. Commands

`make` targets and the Windows PowerShell equivalent `tasks.ps1` cover the same
workflow.

| Target | What it does |
| --- | --- |
| `install` | Install backend + frontend dependencies |
| `dev-backend` / `dev-frontend` | Run the two dev servers |
| `test` | Backend pytest + frontend Vitest |
| `lint` | `ruff check app tests` + `tsc --noEmit` |
| `eval` | Run the v1 vs v2 evaluation harness |
| `eval-report` | Print the latest stored evaluation run |
| `migrate` / `seed` | Alembic upgrade / seed policies + scenarios |
| `e2e` | Playwright (optional; not part of CI) |
| `docker-up` / `docker-down` | Docker Compose lifecycle |

Examples:

```bash
make test                       # Linux/macOS
.\tasks.ps1 test                # Windows

cd backend
python -m pytest -q
python -m ruff check app tests
python -m app.evaluation.cli run --versions v1,v2 --label "baseline v1 vs v2"
python -m app.evaluation.cli compare
```

Verified locally: 24 backend tests, 12 frontend tests, ruff clean, `tsc` clean,
`vite build` succeeds, `docker compose config` validates.

---

## 6. Configuration

All secrets come from the environment. Copy `.env.example` to `.env` and fill
in only what you have. With an empty config the app uses SQLite, in-memory
caching, the local policy index, and the deterministic planner.

To use Supabase later, set:

```bash
DATABASE_URL=postgresql://postgres.<ref>:<password>@<host>:6543/postgres?sslmode=require
```

To use a live model, set `GEMINI_API_KEY` (and optionally `LLM_PROVIDER=gemini`).
Without a key, `LLM_PROVIDER=auto` gracefully falls back to the mock planner and
the `/health` response reports the effective provider. No key is required for
tests, evaluation, or the UI demo.

See [docs/api.md](docs/api.md) for the HTTP surface and
[docs/security.md](docs/security.md) for the PII model.

---

## 7. Docker

```bash
cp .env.example .env      # optional; defaults work offline
docker compose up --build
```

- UI: <http://localhost:8080> (nginx proxies `/api` and `/health` to the backend,
  with SSE buffering disabled).
- API: <http://localhost:8000>.
- `evals/` is mounted into the backend so the same scenarios and reports are
  available in the container.

---

## 8. CI

`.github/workflows/ci.yml` runs three jobs on every push/PR:

1. **backend** — `ruff check` + `pytest` on Python 3.12.
2. **frontend** — `tsc --noEmit` + Vitest + `vite build` on Node 20.
3. **evaluation** — a real v1/v2 harness run, uploading JSON/Markdown reports
   as an artifact. The harness is deterministic, so no API key is needed.

---

## 9. Important limitations

- **SSE fan-out is in-process** (`EventHub`). With multiple backend replicas,
  events are not shared across processes; a Redis pub/sub adapter is the next
  step. Idempotency already works across replicas when Redis is configured.
- **Integrations are mocked**: OCR confidence, PAN registry, bank gateway and
  address geocoder are deterministic simulations. No real KYC provider is called.
- **Cost numbers are estimates** from configurable per-million-token prices,
  not billing data.
- This is a demonstration/reference implementation, not a certified KYC system.
