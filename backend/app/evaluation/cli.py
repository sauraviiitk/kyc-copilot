"""Evaluation CLI.

Usage:
    python -m app.evaluation.cli run --versions v1,v2
    python -m app.evaluation.cli run --categories PAN,OCR --label "pan-ocr"
    python -m app.evaluation.cli list
    python -m app.evaluation.cli show --run-id <id>
    python -m app.evaluation.cli compare --run-id <id>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from sqlalchemy import select

from app.core.config import settings
from app.core.logging import configure_logging
from app.db.seed import seed_all
from app.db.session import SessionLocal, engine
from app.models.entities import EvaluationResult, EvaluationRun


def _prepare() -> None:
    from app.db.base import Base

    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_all(db)
        db.commit()


def _cmd_run(args: argparse.Namespace) -> int:
    from app.evaluation.harness import run_evaluation

    _prepare()
    versions = [version.strip() for version in args.versions.split(",") if version.strip()]
    categories = (
        [category.strip() for category in args.categories.split(",") if category.strip()]
        if args.categories
        else None
    )
    scenario_ids = (
        [sid.strip() for sid in args.scenarios.split(",") if sid.strip()] if args.scenarios else None
    )
    artifacts = run_evaluation(
        label=args.label,
        versions=versions,
        categories=categories,
        scenario_ids=scenario_ids,
    )
    report = artifacts.report
    versions_run = report["run"]["prompt_versions"]
    summaries = report["summaries"]

    print()
    print(f"Run: {report['run']['id']}  ({report['run']['provider']}/{report['run']['model']})")
    header = f"{'metric':<28}" + "".join(f"{version:>12}" for version in versions_run)
    print(header)
    print("-" * len(header))
    rows = [
        ("pass_rate", lambda v: f"{summaries[v]['pass_rate']:.1%}"),
        ("passed/total", lambda v: f"{summaries[v]['passed']}/{summaries[v]['total']}"),
        ("p50 latency (ms)", lambda v: str(summaries[v]["p50_latency_ms"])),
        ("p95 latency (ms)", lambda v: str(summaries[v]["p95_latency_ms"])),
        ("total cost (USD)", lambda v: f"{summaries[v]['total_cost_usd']:.6f}"),
        ("hallucination rate", lambda v: f"{summaries[v]['hallucination_rate']:.1%}"),
        ("tool success", lambda v: f"{summaries[v]['tool_success_rate']:.1%}"),
        ("policy grounding", lambda v: f"{summaries[v]['policy_grounding_accuracy']:.1%}"),
    ]
    for label, formatter in rows:
        print(f"{label:<28}" + "".join(f"{formatter(version):>12}" for version in versions_run))

    comparison = report.get("comparison") or {}
    gate = (comparison.get("per_scenario") or {})
    if gate:
        print()
        print(f"Per-scenario ({gate.get('baseline')} -> {gate.get('candidate')}):")
        print(f"  improved: {', '.join(gate.get('improved') or []) or '-'}")
        print(f"  regressed: {', '.join(gate.get('regressed') or []) or '-'}")
        print(f"  both failed: {', '.join(gate.get('both_failed') or []) or '-'}")

    print()
    print(f"JSON report:     {artifacts.json_path}")
    print(f"Markdown report: {artifacts.markdown_path}")
    print(f"Latest report:   {settings.reports_dir / 'latest.json'}")
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    _prepare()
    with SessionLocal() as db:
        runs = db.scalars(select(EvaluationRun).order_by(EvaluationRun.started_at.desc())).all()
    if not runs:
        print("No evaluation runs yet. Run: python -m app.evaluation.cli run")
        return 0
    print(f"{'id':<38} {'status':<10} {'versions':<10} {'pass':<10} {'label'}")
    for run in runs:
        print(
            f"{run.id:<38} {run.status:<10} {','.join(run.prompt_versions or []):<10} "
            f"{run.passed}/{run.total_scenarios:<7} {run.label}"
        )
    return 0


def _read_run(run_id: str | None) -> dict:
    _prepare()
    with SessionLocal() as db:
        run = db.get(EvaluationRun, run_id) if run_id else db.scalar(
            select(EvaluationRun).order_by(EvaluationRun.started_at.desc())
        )
        if run is None:
            raise SystemExit("evaluation run not found")
        results = db.scalars(
            select(EvaluationResult).where(EvaluationResult.run_id == run.id)
        ).all()
    return {
        "run": {
            "id": run.id,
            "label": run.label,
            "status": run.status,
            "provider": run.provider,
            "model": run.model,
            "prompt_versions": run.prompt_versions,
            "passed": run.passed,
            "failed": run.failed,
            "total": run.total_scenarios,
            "p50_latency_ms": run.p50_latency_ms,
            "p95_latency_ms": run.p95_latency_ms,
            "total_cost_usd": run.total_cost_usd,
            "started_at": run.started_at.isoformat() if run.started_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None,
        },
        "summary": run.summary,
        "results": [
            {
                "scenario_id": result.scenario_id,
                "prompt_version": result.prompt_version,
                "category": result.category,
                "passed": result.passed,
                "checks": result.checks,
                "failure_reasons": result.failure_reasons,
                "actual_actions": result.actual_actions,
                "latency_ms": result.latency_ms,
                "cost_usd": result.cost_usd,
            }
            for result in results
        ],
    }


def _cmd_show(args: argparse.Namespace) -> int:
    payload = _read_run(args.run_id)
    print(json.dumps(payload, indent=2, default=str))
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    payload = _read_run(args.run_id)
    summary = payload.get("summary") or {}
    by_version = summary.get("by_version") or {}
    if not by_version:
        print("This run has no summary yet.")
        return 1
    versions = sorted(by_version)
    print(f"Run {payload['run']['id']} — {payload['run']['label']}")
    for version in versions:
        block = by_version[version]
        print(
            f"  {version}: pass {block['pass_rate']:.1%} ({block['passed']}/{block['total']}), "
            f"p95 {block['p95_latency_ms']} ms, hallucination {block['hallucination_rate']:.1%}, "
            f"grounding {block['policy_grounding_accuracy']:.1%}"
        )
    deltas = (summary.get("comparison") or {}).get("deltas") or {}
    for pair, values in deltas.items():
        print(f"  delta {pair}: {json.dumps(values)}")
    return 0


def _cmd_export(args: argparse.Namespace) -> int:
    payload = _read_run(args.run_id)
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"wrote {target}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m app.evaluation.cli", description="KYC Copilot evals")
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run the evaluation harness")
    run.add_argument("--versions", default="v1,v2", help="comma separated prompt versions (default v1,v2)")
    run.add_argument("--categories", default=None, help="comma separated categories: PAN,OCR,MANDATE,INTENT")
    run.add_argument("--scenarios", default=None, help="comma separated scenario ids")
    run.add_argument("--label", default=None, help="human readable label for the run")
    run.set_defaults(func=_cmd_run)

    listing = sub.add_parser("list", help="list evaluation runs")
    listing.set_defaults(func=_cmd_list)

    show = sub.add_parser("show", help="print one evaluation run as JSON")
    show.add_argument("--run-id", default=None)
    show.set_defaults(func=_cmd_show)

    compare = sub.add_parser("compare", help="compare prompt versions in a run")
    compare.add_argument("--run-id", default=None)
    compare.set_defaults(func=_cmd_compare)

    export = sub.add_parser("export", help="export one run to a JSON file")
    export.add_argument("--run-id", default=None)
    export.add_argument("--output", required=True)
    export.set_defaults(func=_cmd_export)

    return parser


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
