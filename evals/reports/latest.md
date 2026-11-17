# KYC Copilot evaluation — baseline v1 vs v2

- Run: `bb5af813-4d01-4609-82a0-cb7497acbf16`
- Provider: `mock` / `deterministic-planner`
- Scenarios per version: 32
- Prompt versions: v1, v2

## Headline metrics

| Metric | v1 | v2 |
| --- | ---: | ---: |
| Pass rate | 9.4% | 100.0% |
| p50 latency (ms) | 14 | 14 |
| p95 latency (ms) | 27 | 26 |
| Average latency (ms) | 16 | 16 |
| Total cost (USD) | 0.023982 | 0.028930 |
| Hallucinated-action rate | 0.0% | 0.0% |
| Tool success rate | 100.0% | 100.0% |
| Policy grounding accuracy | 0.0% | 100.0% |

## By category

| Category | v1 pass rate | v2 pass rate |
| --- | ---: | ---: |
| INTENT | 1/8 (12%) | 8/8 (100%) |
| MANDATE | 1/7 (14%) | 7/7 (100%) |
| OCR | 0/9 (0%) | 9/9 (100%) |
| PAN | 1/8 (12%) | 8/8 (100%) |

## Version deltas

**v1->v2**

- `pass_rate`: +0.9062
- `p50_latency_ms`: +0.0
- `p95_latency_ms`: -1.0
- `avg_latency_ms`: +0.0
- `total_cost_usd`: +0.004948
- `hallucination_rate`: +0.0
- `tool_success_rate`: +0.0
- `policy_grounding_accuracy`: +1.0

## Notes

- Latency is measured around the real agent execution path (`perf_counter` in `AgentRuntime`).
- PASS/FAIL is determined by executing the produced actions through the same client protocol the frontend uses, including the mandate retry confirmation step.
- Raw PII is never written to reports; values are checked in memory and only masked previews are persisted.
- With `LLM_PROVIDER=auto` and no API key the deterministic offline planner runs, so results are reproducible.
