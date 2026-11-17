"""Evaluation API for the developer dashboard.

Runs execute in a single background worker thread so the HTTP request returns
immediately; the dashboard polls the run until its status is `completed`.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.deps import get_db
from app.core.config import settings
from app.core.logging import get_logger
from app.db.seed import seed_all
from app.evaluation.harness import load_report, run_evaluation
from app.evaluation.scenarios import load_scenarios, scenario_summary
from app.models.entities import EvaluationResult, EvaluationRun
from app.schemas import EvalRunRequest

logger = get_logger(__name__)
router = APIRouter(prefix="/api/eval", tags=["evaluation"])

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="eval-run")


def _run_in_background(label: str | None, versions: list[str], scenario_ids: list[str] | None, categories: list[str] | None) -> None:
    try:
        run_evaluation(
            label=label,
            versions=versions,
            scenario_ids=scenario_ids,
            categories=categories,
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception("eval.background.failed")


@router.get("/scenarios")
def scenarios() -> dict[str, Any]:
    try:
        items = [scenario_summary(scenario) for scenario in load_scenarios()]
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail={"code": "scenarios_missing", "message": str(exc)}) from exc
    return {"count": len(items), "scenarios": items, "source": str(settings.scenarios_dir)}


@router.post("/runs", status_code=202)
def start_run(payload: EvalRunRequest, db: Session = Depends(get_db)) -> dict[str, Any]:
    seed_all(db)
    db.commit()
    try:
        scenarios = load_scenarios(
            scenario_ids=payload.scenario_ids, categories=payload.categories
        )
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(status_code=422, detail={"code": "invalid_selection", "message": str(exc)}) from exc
    if not scenarios:
        raise HTTPException(status_code=422, detail={"code": "no_scenarios", "message": "No scenarios matched"})

    _executor.submit(
        _run_in_background,
        payload.label,
        list(payload.prompt_versions),
        payload.scenario_ids,
        payload.categories,
    )
    return {
        "accepted": True,
        "scenario_count": len(scenarios),
        "prompt_versions": list(payload.prompt_versions),
        "poll": "/api/eval/runs",
    }


@router.get("/runs")
def list_runs(limit: int = 20, db: Session = Depends(get_db)) -> dict[str, Any]:
    rows = db.scalars(
        select(EvaluationRun).order_by(EvaluationRun.started_at.desc()).limit(min(limit, 100))
    ).all()
    return {
        "runs": [
            {
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
            }
            for run in rows
        ]
    }


@router.get("/latest")
def latest(db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.scalar(select(EvaluationRun).order_by(EvaluationRun.started_at.desc()))
    if run is None:
        try:
            return {"source": "file", **load_report()}
        except FileNotFoundError:
            raise HTTPException(
                status_code=404,
                detail={"code": "no_eval_runs", "message": "No evaluation runs yet."},
            ) from None
    return _serialise_run(db, run)


@router.get("/runs/{run_id}")
def read_run(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.get(EvaluationRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail={"code": "run_not_found", "message": "Evaluation run not found"})
    return _serialise_run(db, run)


def _serialise_run(db: Session, run: EvaluationRun) -> dict[str, Any]:
    results = db.scalars(
        select(EvaluationResult)
        .where(EvaluationResult.run_id == run.id)
        .order_by(EvaluationResult.category, EvaluationResult.scenario_id, EvaluationResult.prompt_version)
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
            "config": run.config,
        },
        "summary": run.summary,
        "results": [
            {
                "id": result.id,
                "scenario_id": result.scenario_id,
                "prompt_version": result.prompt_version,
                "category": result.category,
                "screen_id": result.screen_id,
                "passed": result.passed,
                "checks": result.checks,
                "failure_reasons": result.failure_reasons,
                "actual_actions": result.actual_actions,
                "actual": result.actual,
                "agent_run_id": result.agent_run_id,
                "latency_ms": result.latency_ms,
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "cost_usd": result.cost_usd,
            }
            for result in results
        ],
        "generated_at": datetime.now(UTC).isoformat(),
    }


__all__ = ["router"]
