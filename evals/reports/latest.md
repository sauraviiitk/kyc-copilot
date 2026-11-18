# KYC Copilot evaluation — 2026-11-17 17:31 gemini

- Run: `228ccddb-9ae2-4b8d-a0bc-be2c47fdf5f3`
- Provider: `gemini` / `gemini-3.1-flash-lite`
- Scenarios per version: 32
- Prompt versions: v1, v2

## Headline metrics

| Metric | v1 | v2 |
| --- | ---: | ---: |
| Pass rate | 62.5% | 71.9% |
| p50 latency (ms) | 10069 | 9964 |
| p95 latency (ms) | 27335 | 35619 |
| Average latency (ms) | 12796 | 11689 |
| Total cost (USD) | 0.035819 | 0.038187 |
| Hallucinated-action rate | 3.1% | 3.1% |
| Tool success rate | 100.0% | 100.0% |
| Policy grounding accuracy | 100.0% | 100.0% |

## By category

| Category | v1 pass rate | v2 pass rate |
| --- | ---: | ---: |
| INTENT | 6/8 (75%) | 6/8 (75%) |
| MANDATE | 5/7 (71%) | 5/7 (71%) |
| OCR | 4/9 (44%) | 5/9 (56%) |
| PAN | 5/8 (62%) | 7/8 (88%) |

## Version deltas

**v1->v2**

- `pass_rate`: +0.0938
- `p50_latency_ms`: -105.0
- `p95_latency_ms`: +8284.0
- `avg_latency_ms`: -1107.0
- `total_cost_usd`: +0.002368
- `hallucination_rate`: +0.0
- `tool_success_rate`: +0.0
- `policy_grounding_accuracy`: +0.0

## Notes

- Latency is measured around the real agent execution path (`perf_counter` in `AgentRuntime`).
- PASS/FAIL is determined by executing the produced actions through the same client protocol the frontend uses, including the mandate retry confirmation step.
- Raw PII is never written to reports; values are checked in memory and only masked previews are persisted.
- With `LLM_PROVIDER=auto` and no API key the deterministic offline planner runs, so results are reproducible.
