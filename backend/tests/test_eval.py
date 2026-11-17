"""Evaluation harness, metrics and report-safety tests."""

from __future__ import annotations

import json
from pathlib import Path

from app.core.config import settings
from app.evaluation.harness import evaluate_expectations, run_evaluation
from app.evaluation.metrics import compare_versions, percentile, summarise_results
from app.evaluation.scenarios import load_scenarios
from app.schemas import Scenario
from tests.conftest import dump


def test_scenario_library_covers_all_categories() -> None:
    scenarios = load_scenarios()
    assert len(scenarios) >= 30
    assert len({scenario.id for scenario in scenarios}) == len(scenarios)
    assert {scenario.category for scenario in scenarios} == {"PAN", "OCR", "MANDATE", "INTENT"}
    assert all(scenario.intent for scenario in scenarios)
    assert all(scenario.expect for scenario in scenarios)

    by_id = {scenario.id: scenario for scenario in scenarios}
    assert by_id["pan_lowercase"].expect["values"]["pan"] == "ABCDE1234F"
    assert by_id["mandate_duplicate_request"].category == "MANDATE"


def test_metrics_are_computed_from_rows() -> None:
    rows = [
        {"passed": True, "latency_ms": 20, "cost_usd": 0.01, "hallucinated": False, "actions_total": 2, "actions_applied": 2, "grounded": True, "category": "PAN"},
        {"passed": False, "latency_ms": 40, "cost_usd": 0.03, "hallucinated": True, "actions_total": 1, "actions_applied": 0, "grounded": False, "category": "OCR"},
    ]
    summary = summarise_results(rows)
    assert summary["total"] == 2 and summary["passed"] == 1
    assert summary["pass_rate"] == 0.5
    assert summary["p95_latency_ms"] == 40
    assert summary["hallucination_rate"] == 0.5
    assert summary["tool_success_rate"] == round(2 / 3, 4)
    assert summary["policy_grounding_accuracy"] == 0.5
    assert summary["by_category"]["PAN"]["pass_rate"] == 1.0
    assert percentile([1, 2, 3, 4], 50) == 2

    comparison = compare_versions({"v1": summary, "v2": summary})
    assert comparison["baseline"] == "v1"
    assert comparison["deltas"]["v1->v2"]["pass_rate"] == 0.0


def test_expectations_fail_when_action_is_missing() -> None:
    scenario = Scenario(
        id="test_expectations",
        category="PAN",
        fail_class="PAN",
        screen="kyc.pan_entry",
        intent="test",
        expect={"actions_include": ["fill:pan"], "no_mutation": True},
    )
    passed, checks, failures = evaluate_expectations(
        scenario, {"actions": ["highlight:pan"], "hallucinated": False}
    )
    assert passed is False
    assert checks["actions_include"] is False
    assert any("fill:pan" in reason for reason in failures)


def test_harness_executes_real_plan_and_writes_masked_report(tmp_path: Path) -> None:
    all_scenarios = {scenario.id: scenario for scenario in load_scenarios()}
    selected = [
        all_scenarios["pan_lowercase"],
        all_scenarios["mandate_duplicate_request"],
    ]
    original_rate = settings.mock_mandate_failure_rate

    artifacts = run_evaluation(
        label="pytest subset",
        versions=["v1", "v2"],
        scenarios=selected,
        reports_dir=tmp_path,
    )

    assert settings.mock_mandate_failure_rate == original_rate
    report = artifacts.report
    assert report["run"]["executions"] == 4
    assert set(report["summaries"]) == {"v1", "v2"}
    assert report["summaries"]["v2"]["pass_rate"] == 1.0
    assert report["summaries"]["v2"]["hallucination_rate"] == 0.0
    assert report["comparison"]["deltas"]["v1->v2"]["pass_rate"] > 0
    assert artifacts.json_path.exists() and artifacts.markdown_path.exists()

    written = json.loads(artifacts.json_path.read_text(encoding="utf-8"))
    assert written["run"]["label"] == "pytest subset"
    # Reports hold masked previews only: neither the seeded PAN nor its correct
    # value appears anywhere in the JSON or Markdown artefacts.
    raw = dump(written) + artifacts.markdown_path.read_text(encoding="utf-8")
    assert "abcde1234f" not in raw.lower()
    assert "ABCDE1234F" not in raw
    for result in written["results"]:
        value_checks = result["actual"].get("value_checks") or {}
        for check in value_checks.values():
            assert "expected_preview" in check and "actual_preview" in check
