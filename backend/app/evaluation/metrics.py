"""Evaluation metrics.

Everything here is computed from real executed runs (latency measured with
`perf_counter` in the graph, tokens/cost reported by the provider, action
outcomes recorded by the same protocol the frontend uses). No synthetic
numbers are injected.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

METRIC_LABELS: dict[str, tuple[str, str]] = {
    "pass_rate": ("Task pass rate", "%"),
    "p50_latency_ms": ("p50 latency", "ms"),
    "p95_latency_ms": ("p95 latency", "ms"),
    "avg_latency_ms": ("Average latency", "ms"),
    "total_cost_usd": ("Total cost", "USD"),
    "cost_per_scenario_usd": ("Cost / scenario", "USD"),
    "hallucination_rate": ("Hallucinated-action rate", "%"),
    "tool_success_rate": ("Tool success rate", "%"),
    "policy_grounding_accuracy": ("Policy grounding", "%"),
}


def percentile(values: Iterable[float], pct: float) -> float:
    ordered = sorted(float(v) for v in values)
    if not ordered:
        return 0.0
    rank = max(0, min(len(ordered) - 1, math.ceil((pct / 100.0) * len(ordered)) - 1))
    return ordered[int(rank)]


def _ratio(numerator: float, denominator: float, *, empty: float = 1.0) -> float:
    if denominator <= 0:
        return empty
    return round(numerator / denominator, 4)


def summarise_results(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate per-execution result rows into one metric block."""
    total = len(results)
    passed = sum(1 for r in results if r.get("passed"))
    latencies = [int(r.get("latency_ms") or 0) for r in results]
    costs = [float(r.get("cost_usd") or 0.0) for r in results]
    hallucinated = sum(1 for r in results if r.get("hallucinated"))
    actions_total = sum(int(r.get("actions_total") or 0) for r in results)
    actions_applied = sum(int(r.get("actions_applied") or 0) for r in results)
    grounded = sum(1 for r in results if r.get("grounded"))

    total_cost = round(sum(costs), 6)
    block = {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": _ratio(passed, total, empty=0.0),
        "p50_latency_ms": int(round(percentile(latencies, 50))),
        "p95_latency_ms": int(round(percentile(latencies, 95))),
        "avg_latency_ms": int(round(sum(latencies) / total)) if total else 0,
        "total_cost_usd": total_cost,
        "cost_per_scenario_usd": round(total_cost / total, 6) if total else 0.0,
        "hallucinated_actions": hallucinated,
        "hallucination_rate": _ratio(hallucinated, total, empty=0.0),
        "tool_success_rate": _ratio(actions_applied, actions_total, empty=1.0),
        "actions_total": actions_total,
        "actions_applied": actions_applied,
        "policy_grounding_accuracy": _ratio(grounded, total, empty=0.0),
    }
    block["by_category"] = _by_category(results)
    block["metrics"] = [
        {
            "key": key,
            "label": label,
            "value": block[key],
            "unit": unit,
            "direction": "lower_is_better" if key in {"p50_latency_ms", "p95_latency_ms", "avg_latency_ms", "total_cost_usd", "cost_per_scenario_usd", "hallucination_rate"} else "higher_is_better",
        }
        for key, (label, unit) in METRIC_LABELS.items()
    ]
    return block


def _by_category(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for result in results:
        grouped.setdefault(str(result.get("category") or "UNKNOWN"), []).append(result)
    out: dict[str, dict[str, Any]] = {}
    for category, rows in sorted(grouped.items()):
        latencies = [int(r.get("latency_ms") or 0) for r in rows]
        passed = sum(1 for r in rows if r.get("passed"))
        out[category] = {
            "total": len(rows),
            "passed": passed,
            "failed": len(rows) - passed,
            "pass_rate": _ratio(passed, len(rows), empty=0.0),
            "p50_latency_ms": int(round(percentile(latencies, 50))),
            "p95_latency_ms": int(round(percentile(latencies, 95))),
            "cost_usd": round(sum(float(r.get("cost_usd") or 0.0) for r in rows), 6),
            "hallucination_rate": _ratio(
                sum(1 for r in rows if r.get("hallucinated")), len(rows), empty=0.0
            ),
            "policy_grounding_accuracy": _ratio(
                sum(1 for r in rows if r.get("grounded")), len(rows), empty=0.0
            ),
        }
    return out


def compare_versions(summaries: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Pairwise comparison between every prompt version and the newest one."""
    if not summaries:
        return {}
    versions = sorted(summaries)
    baseline_version = versions[0]
    comparison: dict[str, Any] = {
        "baseline": baseline_version,
        "versions": versions,
        "deltas": {},
        "per_scenario": {},
    }
    for version in versions[1:]:
        baseline = summaries[baseline_version]
        candidate = summaries[version]
        comparison["deltas"][f"{baseline_version}->{version}"] = {
            key: round(float(candidate.get(key, 0.0)) - float(baseline.get(key, 0.0)), 6)
            for key in (
                "pass_rate",
                "p50_latency_ms",
                "p95_latency_ms",
                "avg_latency_ms",
                "total_cost_usd",
                "hallucination_rate",
                "tool_success_rate",
                "policy_grounding_accuracy",
            )
        }
    return comparison


def build_metric_table(summary: dict[str, Any]) -> list[dict[str, Any]]:
    return list(summary.get("metrics") or [])


def markdown_summary(
    *,
    label: str,
    run_id: str,
    provider: str,
    model: str,
    summaries: dict[str, dict[str, Any]],
    comparison: dict[str, Any],
    scenario_count: int,
) -> str:
    versions = sorted(summaries)
    lines = [
        f"# KYC Copilot evaluation — {label}",
        "",
        f"- Run: `{run_id}`",
        f"- Provider: `{provider}` / `{model}`",
        f"- Scenarios per version: {scenario_count}",
        f"- Prompt versions: {', '.join(versions)}",
        "",
        "## Headline metrics",
        "",
        "| Metric | " + " | ".join(versions) + " |",
        "| --- | " + " | ".join("---:" for _ in versions) + " |",
    ]

    def row(key: str, label_text: str, fmt: str = "{:.2f}") -> str:
        cells = []
        for version in versions:
            value = summaries[version].get(key, 0)
            cells.append(fmt.format(value))
        return f"| {label_text} | " + " | ".join(cells) + " |"

    lines += [
        row("pass_rate", "Pass rate", "{:.1%}"),
        row("p50_latency_ms", "p50 latency (ms)", "{:d}"),
        row("p95_latency_ms", "p95 latency (ms)", "{:d}"),
        row("avg_latency_ms", "Average latency (ms)", "{:d}"),
        row("total_cost_usd", "Total cost (USD)", "{:.6f}"),
        row("hallucination_rate", "Hallucinated-action rate", "{:.1%}"),
        row("tool_success_rate", "Tool success rate", "{:.1%}"),
        row("policy_grounding_accuracy", "Policy grounding accuracy", "{:.1%}"),
        "",
        "## By category",
        "",
    ]
    categories = sorted({c for version in versions for c in summaries[version].get("by_category", {})})
    lines.append("| Category | " + " | ".join(f"{v} pass rate" for v in versions) + " |")
    lines.append("| --- | " + " | ".join("---:" for _ in versions) + " |")
    for category in categories:
        cells = []
        for version in versions:
            block = summaries[version].get("by_category", {}).get(category, {})
            cells.append(f"{block.get('passed', 0)}/{block.get('total', 0)} ({block.get('pass_rate', 0):.0%})")
        lines.append(f"| {category} | " + " | ".join(cells) + " |")

    deltas = comparison.get("deltas") or {}
    if deltas:
        lines += ["", "## Version deltas", ""]
        for pair, values in deltas.items():
            lines.append(f"**{pair}**")
            lines.append("")
            for key, value in values.items():
                lines.append(f"- `{key}`: {'+' if value >= 0 else ''}{value}")
            lines.append("")

    lines += [
        "## Notes",
        "",
        "- Latency is measured around the real agent execution path (`perf_counter` in `AgentRuntime`).",
        "- PASS/FAIL is determined by executing the produced actions through the same client "
        "protocol the frontend uses, including the mandate retry confirmation step.",
        "- Raw PII is never written to reports; values are checked in memory and only masked previews are persisted.",
        "- With `LLM_PROVIDER=auto` and no API key the deterministic offline planner runs, so results are reproducible.",
        "",
    ]
    return "\n".join(lines)
