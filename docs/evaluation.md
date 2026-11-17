# Evaluation harness

## What it measures

The harness executes 32 scenarios against the real backend action protocol —
not against mocks of the agent. A headless client creates a session, publishes
the scenario's screen context, starts an agent run, replays the SSE stream,
fetches fill values through the value endpoint, confirms financial retries, and
posts action results with the hash attestation. Every check therefore exercises
the same guardrails the UI does.

Four categories are covered:

| Category | Count | Examples |
| --- | ---: | --- |
| PAN | 8 | lowercase, invalid format, missing characters, empty, already verified |
| OCR | 9 | blurry, rotated, unreadable, low confidence, duplicate upload, conflicting values |
| MANDATE | 7 | success, timeout, repeated failures, duplicate request, unknown status |
| INTENT | 8 | bypass request, human escalation, policy question, out of scope, sensitive without confirmation |

Scenario definitions live in `evals/scenarios/{pan,ocr,mandate,intent}.json`.
The seeded DB and the JSON files are kept in sync by `sync_scenarios()`.

## Metrics

For each prompt version the harness records:

- **pass_rate** — fraction of scenarios where every `expect` check passed;
- **p50 / p95 / avg latency** — wall-clock ms per execution;
- **total / per-scenario cost** — estimated from token counts and configurable
  per-million-token prices;
- **hallucination_rate** — runs with at least one refused fill proposal;
- **tool_success_rate** — actions applied / actions attempted;
- **policy_grounding_accuracy** — runs that cited every required policy ref.

`policy_refs_include` checks are the main difference between v1 and v2: v1
retrieves the correct policies but does not cite them in its decision, so its
grounding is 0%. v2 cites them, reaching 100%.

## Determinism

- `LLM_PROVIDER=mock` uses `deterministic-planner` with a versioned behaviour
  flag (`v1`/`v2`). No network or API key is needed.
- `MOCK_SEED=kyc-copilot` makes OCR confidence and mandate failure outcomes
  reproducible.
- `MOCK_MANDATE_FAILURE_RATE` is forced to `0.0` for the duration of a run and
  restored afterwards, so retry scenarios are stable.
- Reports persist **masked previews only**; raw filled/expected PII is never
  written to JSON, Markdown, or the DB.

CI runs the identical harness (`make eval` / `python -m app.evaluation.cli run
--versions v1,v2`), so the numbers are reproducible from a clean checkout.

## Latest measured result

Run `bb5af813-4d01-4609-82a0-cb7497acbf16`, label `baseline v1 vs v2`,
2026-11-17, mock/deterministic-planner:

| Metric | v1 | v2 | Δ (v1→v2) |
| --- | ---: | ---: | ---: |
| Pass rate | 9.4% (3/32) | **100% (32/32)** | +90.62 pp |
| p50 latency | 14 ms | 14 ms | 0 |
| p95 latency | 27 ms | 26 ms | −1 ms |
| Tool success | 100% | 100% | 0 |
| Hallucination rate | 0% | 0% | 0 |
| Policy grounding | 0% | **100%** | +100 pp |
| Total cost (est.) | $0.023982 | $0.028930 | +$0.004948 |

Per-scenario: 29 improved, 0 regressed, 3 passed in both versions
(`intent_sensitive_without_confirmation`, `mandate_already_completed`,
`pan_already_verified`), 0 failed in both.

Artifacts:

- `evals/reports/latest.json` — machine-readable full report.
- `evals/reports/latest.md` — human-readable summary.
- `evals/reports/run_20261117_130600_bb5af813.{json,md}` — immutable snapshot.

## Running it

```bash
cd backend
python -m app.evaluation.cli run --versions v1,v2 --label "baseline v1 vs v2"
python -m app.evaluation.cli run --categories PAN,OCR --label "pan-ocr only"
python -m app.evaluation.cli run --scenarios pan_lowercase,mandate_timeout
python -m app.evaluation.cli list
python -m app.evaluation.cli compare
python -m app.evaluation.cli show --run-id <id>
python -m app.evaluation.cli export --run-id <id> --output report.json
```

Or via HTTP: `GET /api/eval/scenarios`, `POST /api/eval/runs`,
`GET /api/eval/latest`, `GET /api/eval/runs/{run_id}`. The dashboard in the
frontend reads these endpoints.

## Interpreting the result honestly

The large v1→v2 gap is a **prompt/behaviour** difference in the deterministic
planner, not a model capability claim. Hard guardrails (no invented PAN, no
auto-submit, no duplicate mandate) are version-independent and hold in both.
Costs are estimates, not billing data. Numbers come from an actual run on the
checked-in scenarios; re-running with a live LLM will produce different values.
