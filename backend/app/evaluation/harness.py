"""Evaluation harness.

For every scenario and prompt version this module:

1. creates an isolated evaluation session and seeds the *live* screen state
   (fields, options, errors, document metadata and a real mandate row);
2. executes the normal agent graph synchronously with the same masked context
   the frontend would publish;
3. executes every produced action through the same client protocol the browser
   uses — fills are attested by hash, mandate retries call the real retry
   service after the simulated user confirms, escalations are recorded;
4. checks the scenario's expectations against the resulting database state and
   writes a machine-readable report.

Nothing is hardcoded per scenario and no success is assumed: a scenario only
passes when the executed actions satisfy its expectations.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.runner import create_run, execute_run
from app.core.config import settings
from app.core.logging import get_logger
from app.db.session import SessionLocal
from app.evaluation.metrics import compare_versions, markdown_summary, summarise_results
from app.evaluation.scenarios import load_scenarios, scenario_summary, sync_scenarios
from app.models.entities import (
    AgentAction,
    DocumentRecord,
    EvaluationResult,
    EvaluationRun,
    HumanEscalation,
    MandateRecord,
    UserSession,
)
from app.schemas import SENSITIVE_FIELDS, Scenario
from app.services.actions import (
    apply_action_result,
    decrypt_action_value,
    evaluate_run_status,
    resolve_mandate_confirmation,
)
from app.services.events import hub
from app.services.journey import (
    build_context_from_seed,
    create_session,
    update_session_meta,
    upsert_screen_state,
)
from app.services.mandate import derive_mandate_ref, latest_mandate, retry_mandate
from app.services.pii import preview_mask, value_hash

logger = get_logger(__name__)

MUTATING_ACTIONS = {"fill", "upload", "retry"}


def recover_orphaned_runs() -> int:
    """Mark evaluation runs left in ``running`` by a killed/restarted worker.

    Evaluations execute in an in-process worker with no cross-process queue. Any
    run still ``running`` at start-up therefore belonged to a previous process
    and can never finish. Leaving it that way makes the dashboard's
    ``/eval/latest`` poll spin forever, so fail it explicitly on boot.
    """
    db = SessionLocal()
    try:
        orphaned = db.scalars(
            select(EvaluationRun).where(EvaluationRun.status == "running")
        ).all()
        for run in orphaned:
            run.status = "failed"
            run.finished_at = datetime.now(UTC)
            previous = dict(run.summary or {})
            previous["error"] = "Evaluation run interrupted by a worker restart."
            run.summary = previous
            logger.warning("eval.run.recovered run_id=%s", run.id)
        if orphaned:
            db.commit()
        return len(orphaned)
    finally:
        db.close()


@dataclass
class EvalArtifacts:
    report: dict[str, Any]
    json_path: Path
    markdown_path: Path


# ---------------------------------------------------------------------------
# Seeding
# ---------------------------------------------------------------------------


def _effective_model() -> str:
    provider = settings.effective_llm_provider()
    if provider == "gemini":
        return settings.gemini_model
    if provider == "groq":
        return settings.groq_model
    return "deterministic-planner"


def _seed_eval_session(db: Session, run_id: str, scenario: Scenario, version: str) -> UserSession:
    session = create_session(
        db,
        external_ref=f"eval:{scenario.id}:{version}:{run_id[:8]}",
        is_evaluation=True,
    )

    seed = dict(scenario.seed or {})
    document_seed = seed.get("document")
    if document_seed:
        base_id = str(document_seed.get("document_id") or uuid.uuid4())
        document_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}:{scenario.id}:{version}:{base_id}")
        )
        record = DocumentRecord(
            id=document_id,
            session_id=session.id,
            doc_type=str(document_seed.get("doc_type") or "pan"),
            filename=str(document_seed.get("filename") or "document.jpg")[:200],
            content_type="application/pdf"
            if str(document_seed.get("filename", "")).lower().endswith(".pdf")
            else "image/jpeg",
            size_bytes=1024,
            storage_key="",
            quality={
                key: document_seed.get(key)
                for key in (
                    "quality_score",
                    "ocr_confidence",
                    "rotation_degrees",
                    "blur_score",
                )
                if document_seed.get(key) is not None
            },
            ocr_masked={"simulated": True},
            status=str(document_seed.get("status") or "needs_review"),
        )
        db.add(record)
        seed["document"] = {**document_seed, "document_id": document_id}

    mandate_seed = seed.get("mandate_seed")
    if mandate_seed:
        key = f"eval-{run_id[:8]}-{scenario.id}-{version}"
        status = str(mandate_seed.get("status") or "pending")
        attempts = int(mandate_seed.get("attempts") or 0)
        last_error = {
            "failed": "The bank gateway timed out before confirming the mandate.",
            "unknown": "The bank did not confirm the transaction status.",
            "verified": None,
            "pending": "Mandate is pending at the bank.",
        }.get(status)
        db.add(
            MandateRecord(
                session_id=session.id,
                mandate_ref=derive_mandate_ref(session.id, key),
                bank="HDFC Bank",
                ifsc="HDFC0001234",
                account_masked="••••••1234",
                account_ciphertext=None,
                idempotency_key=key,
                status=status,
                attempts=attempts,
                last_error=last_error,
                detail={"error_code": "SEEDED", "simulated": True},
            )
        )
        update_session_meta(
            db,
            session,
            mandate_force_failures=int(mandate_seed.get("force_failures") or 0),
            mandate_force_mode=str(mandate_seed.get("force_mode") or "timeout"),
            mandate_status=status,
        )

    context = build_context_from_seed(db, session, scenario.screen, seed)
    upsert_screen_state(db, session, context)
    db.commit()
    return session


# ---------------------------------------------------------------------------
# Headless client
# ---------------------------------------------------------------------------


def _action_name(action: AgentAction) -> str:
    if action.field:
        return f"{action.action_type}:{action.field}"
    if action.target:
        return f"{action.action_type}:{action.target}"
    return action.action_type


def _matches(actual: str, expected: str) -> bool:
    if ":" in expected:
        return actual == expected
    return actual == expected or actual.startswith(f"{expected}:")


def _execute_actions(
    db: Session, session: UserSession, run_id: str, current_screen: str
) -> tuple[list[AgentAction], dict[str, str], int]:
    """Run every proposed action through the real client protocol."""
    from app.models.entities import AgentRun

    run = db.get(AgentRun, run_id)
    assert run is not None
    actions = list(
        db.scalars(
            select(AgentAction)
            .where(AgentAction.run_id == run.id)
            .order_by(AgentAction.created_at, AgentAction.id)
        ).all()
    )
    applied_values: dict[str, str] = {}
    applied_count = 0

    for action in actions:
        if action.action_type == "fill":
            try:
                value = decrypt_action_value(action)
            except Exception:  # pragma: no cover - corrupted payload
                apply_action_result(
                    db,
                    run=run,
                    action=action,
                    session=session,
                    status="failed",
                    detail={"error": "value payload unavailable"},
                )
                continue
            status, _ = apply_action_result(
                db,
                run=run,
                action=action,
                session=session,
                status="applied",
                executed_value_hash=value_hash(value, action.field),
                detail={"field": action.field, "screen_id": current_screen},
            )
            if status == "applied":
                applied_values[str(action.field)] = value
                applied_count += 1
            continue

        if action.action_type == "retry":
            mandate = latest_mandate(db, session.id)
            if (
                action.status == "pending_confirmation"
                and mandate is not None
                and mandate.idempotency_key == action.idempotency_key
            ):
                # The headless client is the user: it explicitly confirms the
                # retry, which is the only path that executes a financial call.
                retry_mandate(db, session=session, idempotency_key=mandate.idempotency_key)
                mandate = latest_mandate(db, session.id)
                resolved = resolve_mandate_confirmation(db, session, mandate) if mandate else 0
                if not resolved and mandate is not None:
                    apply_action_result(
                        db,
                        run=run,
                        action=action,
                        session=session,
                        status="applied",
                        detail={
                            "mandate_ref": mandate.mandate_ref,
                            "attempts": mandate.attempts,
                        },
                    )
            else:
                apply_action_result(
                    db,
                    run=run,
                    action=action,
                    session=session,
                    status="applied",
                    detail={
                        "mandate_ref": (action.result or {}).get("mandate_ref"),
                        "attempts": (action.result or {}).get("attempts"),
                    },
                )
            if action.status == "applied":
                applied_count += 1
            continue

        # highlight / upload / escalate / navigate
        apply_action_result(
            db,
            run=run,
            action=action,
            session=session,
            status="applied",
            detail={"screen_id": current_screen},
        )
        if action.status == "applied":
            applied_count += 1

    run.status = evaluate_run_status(db, run)
    if run.status in {"completed", "completed_with_errors"} and run.completed_at is None:
        run.completed_at = datetime.now(UTC)
    db.commit()
    return actions, applied_values, applied_count


# ---------------------------------------------------------------------------
# Expectations
# ---------------------------------------------------------------------------


def _safe_preview(field: str, value: Any) -> str:
    text = "" if value is None else str(value)
    if field in SENSITIVE_FIELDS:
        return preview_mask(text)
    # Address free-text fields are flagged sensitive in SENSITIVE_FIELDS too.
    return text


def evaluate_expectations(scenario: Scenario, actual: dict[str, Any]) -> tuple[bool, dict[str, Any], list[str]]:
    expect = scenario.expect or {}
    checks: dict[str, Any] = {}
    failures: list[str] = []

    def record(name: str, ok: bool, reason: str) -> None:
        checks[name] = ok
        if not ok:
            failures.append(reason)

    names = list(actual["actions"])

    if "actions_include" in expect:
        missing = [
            expected
            for expected in expect["actions_include"]
            if not any(_matches(name, expected) for name in names)
        ]
        record("actions_include", not missing, f"missing actions: {', '.join(missing)}")

    if "actions_exclude" in expect:
        violations = [
            expected
            for expected in expect["actions_exclude"]
            if any(_matches(name, expected) for name in names)
        ]
        record("actions_exclude", not violations, f"forbidden actions present: {', '.join(violations)}")

    if "no_mutation" in expect:
        mutated = [name for name in names if name.split(":", 1)[0] in MUTATING_ACTIONS]
        ok = (not mutated) if expect["no_mutation"] else bool(mutated)
        record("no_mutation", ok, f"unexpected mutating actions: {', '.join(mutated)}")

    if "no_hallucinated_values" in expect:
        ok = (not actual["hallucinated"]) if expect["no_hallucinated_values"] else bool(actual["hallucinated"])
        record(
            "no_hallucinated_values",
            ok,
            "the anti-hallucination gate rejected a proposed value",
        )

    if "requires_confirmation" in expect:
        ok = bool(actual["requires_confirmation"]) == bool(expect["requires_confirmation"])
        record(
            "requires_confirmation",
            ok,
            f"requires_confirmation was {actual['requires_confirmation']}, expected {expect['requires_confirmation']}",
        )

    if "escalate" in expect:
        ok = bool(actual["escalated"]) == bool(expect["escalate"])
        record("escalate", ok, f"escalated={actual['escalated']}, expected {expect['escalate']}")

    if "policy_refs_include" in expect:
        missing = [ref for ref in expect["policy_refs_include"] if ref not in actual["cited_policy_refs"]]
        record("policy_refs_include", not missing, f"missing policy citations: {', '.join(missing)}")

    if "answer_mentions_any" in expect:
        say = str(actual.get("say_masked") or "").lower()
        wanted = [str(word).lower() for word in expect["answer_mentions_any"]]
        record(
            "answer_mentions_any",
            any(word in say for word in wanted),
            f"answer mentioned none of: {', '.join(wanted)}",
        )

    if "values" in expect:
        mismatches: list[str] = []
        value_checks: dict[str, Any] = {}
        for field, wanted in expect["values"].items():
            got = actual["values"].get(field)
            ok = got is not None and str(got) == str(wanted)
            value_checks[field] = {
                "match": ok,
                "expected_preview": _safe_preview(field, wanted),
                "actual_preview": _safe_preview(field, got),
            }
            if not ok:
                mismatches.append(field)
        checks["values"] = value_checks
        if mismatches:
            failures.append(f"field values did not match: {', '.join(mismatches)}")

    if "final_screen" in expect:
        ok = actual["screen_id"] == expect["final_screen"]
        record("final_screen", ok, f"ended on {actual['screen_id']}, expected {expect['final_screen']}")

    if "sensitive_actions_confirmed" in expect:
        ok = bool(actual["sensitive_actions_confirmed"]) == bool(expect["sensitive_actions_confirmed"])
        record(
            "sensitive_actions_confirmed",
            ok,
            "a financial action was executed without the confirmation envelope",
        )

    if "mandate_status" in expect:
        ok = actual["mandate_status"] == expect["mandate_status"]
        record(
            "mandate_status",
            ok,
            f"mandate status is {actual['mandate_status']}, expected {expect['mandate_status']}",
        )

    if "mandate_attempts_min" in expect:
        attempts = int(actual["mandate_attempts"] or 0)
        ok = attempts >= int(expect["mandate_attempts_min"])
        record(
            "mandate_attempts_min",
            ok,
            f"mandate attempts {attempts} < {expect['mandate_attempts_min']}",
        )

    return not failures, checks, failures


# ---------------------------------------------------------------------------
# One scenario execution
# ---------------------------------------------------------------------------


def run_single(
    db: Session,
    *,
    eval_run_id: str,
    scenario: Scenario,
    version: str,
) -> dict[str, Any]:
    started = time.perf_counter()
    session = _seed_eval_session(db, eval_run_id, scenario, version)

    run = create_run(db, session_id=session.id, message=scenario.intent, prompt_version=version)
    db.commit()
    outcome = execute_run(db, session=session, run=run, message=scenario.intent, prompt_version=version)
    db.commit()

    actions, applied_values, applied_count = _execute_actions(
        db, session, run.id, session.current_screen_id
    )
    elapsed_ms = int((time.perf_counter() - started) * 1000)

    mandate = latest_mandate(db, session.id)
    escalation = db.scalar(select(HumanEscalation).where(HumanEscalation.run_id == run.id))
    action_names = [_action_name(action) for action in actions]
    retrieved_refs = {
        str(policy.get("ref"))
        for policy in (outcome.retrieved_policies or [])
        if policy.get("ref")
    }
    cited_refs = [str(ref) for ref in (outcome.cited_policy_refs or [])]
    grounded = bool(cited_refs) and all(ref in retrieved_refs for ref in cited_refs)

    seed_fields = {
        name: value
        for name, value in (scenario.seed.get("fields") or {}).items()
        if name not in {"document", "mandate", "status"}
    }
    final_values = {**seed_fields, **applied_values}
    sensitive_actions = [a for a in actions if a.action_type in {"retry", "upload"}]
    sensitive_confirmed = all(bool(a.requires_confirmation) for a in sensitive_actions)

    actual = {
        "actions": action_names,
        "action_statuses": {a.id: a.status for a in actions},
        "values": final_values,
        "sample_type": "masked",
        "screen_id": session.current_screen_id,
        "say_masked": outcome.say_masked,
        "plan_masked": run.plan_masked,
        "requires_confirmation": bool(run.requires_confirmation),
        "cited_policy_refs": cited_refs,
        "retrieved_policy_refs": sorted(retrieved_refs),
        "hallucinated": bool(outcome.hallucination_flags),
        "hallucination_flags": list(outcome.hallucination_flags or []),
        "escalated": bool(escalation is not None or any(a.action_type == "escalate" for a in actions)),
        "mandate_status": mandate.status if mandate else None,
        "mandate_attempts": mandate.attempts if mandate else 0,
        "sensitive_actions_confirmed": sensitive_confirmed,
        "provider": outcome.provider,
        "model": outcome.model,
        "degraded": bool(outcome.degraded),
        "run_status": run.status,
    }

    passed, checks, failures = evaluate_expectations(scenario, actual)

    persisted_actual = {
        "actions": action_names,
        "action_statuses": actual["action_statuses"],
        "policy_refs": cited_refs,
        "retrieved_policy_refs": sorted(retrieved_refs),
        "hallucination_flags": actual["hallucination_flags"],
        "escalated": actual["escalated"],
        "requires_confirmation": actual["requires_confirmation"],
        "mandate_status": actual["mandate_status"],
        "mandate_attempts": actual["mandate_attempts"],
        "run_status": actual["run_status"],
        "provider": outcome.provider,
        "model": outcome.model,
        "degraded": actual["degraded"],
        "say_masked": outcome.say_masked,
        "value_checks": checks.get("values"),
        "total_ms": elapsed_ms,
    }

    result = {
        "scenario_id": scenario.id,
        "prompt_version": version,
        "category": scenario.category,
        "fail_class": scenario.fail_class,
        "screen_id": scenario.screen,
        "intent": scenario.intent,
        "passed": passed,
        "checks": checks,
        "failure_reasons": failures,
        "actual": persisted_actual,
        "agent_run_id": run.id,
        "latency_ms": int(outcome.latency_ms or 0),
        "input_tokens": int(outcome.input_tokens or 0),
        "output_tokens": int(outcome.output_tokens or 0),
        "cost_usd": float(outcome.cost_usd or 0.0),
        "hallucinated": bool(outcome.hallucination_flags),
        "grounded": grounded,
        "actions_total": len(actions),
        "actions_applied": applied_count,
    }

    db.add(
        EvaluationResult(
            run_id=eval_run_id,
            prompt_version=version,
            scenario_id=scenario.id,
            category=scenario.category,
            screen_id=scenario.screen,
            passed=passed,
            checks=checks,
            failure_reasons=failures,
            actual_actions=action_names,
            actual=persisted_actual,
            agent_run_id=run.id,
            latency_ms=int(outcome.latency_ms or 0),
            input_tokens=int(outcome.input_tokens or 0),
            output_tokens=int(outcome.output_tokens or 0),
            cost_usd=float(outcome.cost_usd or 0.0),
        )
    )
    db.commit()

    # Bound in-process memory: no SSE client is attached during evaluation.
    hub.clear()

    if not passed:
        logger.info(
            "eval.scenario.failed scenario=%s version=%s reasons=%s",
            scenario.id,
            version,
            "; ".join(failures)[:300],
        )
    return result


# ---------------------------------------------------------------------------
# Run orchestration
# ---------------------------------------------------------------------------


def _write_reports(
    *,
    run_id: str,
    label: str,
    provider: str,
    model: str,
    versions: list[str],
    scenario_count: int,
    started_at: datetime,
    finished_at: datetime,
    results: list[dict[str, Any]],
    summaries: dict[str, dict[str, Any]],
    comparison: dict[str, Any],
    reports_dir: Path | None = None,
) -> tuple[Path, Path]:
    directory = reports_dir or settings.reports_dir
    directory.mkdir(parents=True, exist_ok=True)
    stamp = finished_at.strftime("%Y%m%d_%H%M%S")
    stem = f"run_{stamp}_{run_id[:8]}"

    report = {
        "run": {
            "id": run_id,
            "label": label,
            "provider": provider,
            "model": model,
            "prompt_versions": versions,
            "scenario_count": scenario_count,
            "executions": len(results),
            "started_at": started_at.isoformat(),
            "finished_at": finished_at.isoformat(),
            "simulated_integrations": ["ocr", "pan_registry", "bank_mandate_gateway", "address_geocoder"],
        },
        "summaries": summaries,
        "comparison": comparison,
        "results": [
            {
                "scenario_id": result["scenario_id"],
                "prompt_version": result["prompt_version"],
                "category": result["category"],
                "fail_class": result["fail_class"],
                "passed": result["passed"],
                "checks": result["checks"],
                "failure_reasons": result["failure_reasons"],
                "actual": result["actual"],
                "latency_ms": result["latency_ms"],
                "input_tokens": result["input_tokens"],
                "output_tokens": result["output_tokens"],
                "cost_usd": result["cost_usd"],
                "agent_run_id": result["agent_run_id"],
            }
            for result in results
        ],
    }

    json_path = directory / f"{stem}.json"
    json_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (directory / "latest.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    markdown = markdown_summary(
        label=label,
        run_id=run_id,
        provider=provider,
        model=model,
        summaries=summaries,
        comparison=comparison,
        scenario_count=scenario_count,
    )
    markdown_path = directory / f"{stem}.md"
    markdown_path.write_text(markdown, encoding="utf-8")
    (directory / "latest.md").write_text(markdown, encoding="utf-8")
    return json_path, markdown_path


def run_evaluation(
    *,
    label: str | None = None,
    versions: list[str] | None = None,
    scenario_ids: list[str] | None = None,
    categories: list[str] | None = None,
    scenarios: list[Scenario] | None = None,
    reports_dir: Path | None = None,
) -> EvalArtifacts:
    versions = list(versions or ["v1", "v2"])
    unknown = [version for version in versions if version not in {"v1", "v2"}]
    if unknown:
        raise ValueError(f"unknown prompt versions: {', '.join(unknown)}")
    scenarios = scenarios or load_scenarios(scenario_ids=scenario_ids, categories=categories)
    if not scenarios:
        raise ValueError("no scenarios matched the selection")

    provider = settings.effective_llm_provider()
    model = _effective_model()
    label = label or f"{datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} {provider}"

    # Deterministic evaluation: injected failures are seeded per scenario, so
    # the random gateway failure rate is disabled for the run and restored after.
    original_failure_rate = settings.mock_mandate_failure_rate

    started_at = datetime.now(UTC)
    db = SessionLocal()
    try:
        sync_scenarios(db, scenarios)
        eval_run = EvaluationRun(
            label=label,
            prompt_versions=versions,
            provider=provider,
            model=model,
            status="running",
            total_scenarios=len(scenarios) * len(versions),
            config={
                "scenarios": [scenario.id for scenario in scenarios],
                "categories": sorted({scenario.category for scenario in scenarios}),
                "max_agent_tool_calls": settings.max_agent_tool_calls,
                "mock_mandate_failure_rate": 0.0,
                "live_llm": provider != "mock",
            },
        )
        db.add(eval_run)
        db.commit()
        run_id = eval_run.id

        settings.mock_mandate_failure_rate = 0.0
        results: list[dict[str, Any]] = []
        by_version: dict[str, list[dict[str, Any]]] = {version: [] for version in versions}
        try:
            for version in versions:
                for scenario in scenarios:
                    result = run_single(db, eval_run_id=run_id, scenario=scenario, version=version)
                    results.append(result)
                    by_version[version].append(result)
        except Exception as exc:
            # An aborted run must leave a terminal status; otherwise the
            # dashboard keeps polling /eval/latest forever.
            eval_run.status = "failed"
            eval_run.finished_at = datetime.now(UTC)
            eval_run.summary = {"error": f"Evaluation interrupted: {type(exc).__name__}"}
            db.commit()
            logger.error("eval.run.failed run_id=%s error=%s", run_id, type(exc).__name__)
            raise
        finally:
            settings.mock_mandate_failure_rate = original_failure_rate

        summaries = {version: summarise_results(rows) for version, rows in by_version.items()}
        comparison = compare_versions(summaries)
        comparison["per_scenario"] = _per_scenario_comparison(by_version)

        passed = sum(1 for result in results if result["passed"])
        finished_at = datetime.now(UTC)

        summary_payload = {
            "label": label,
            "prompt_versions": versions,
            "by_version": summaries,
            "comparison": comparison,
            "scenario_count": len(scenarios),
            "executions": len(results),
            "categories": {
                version: summaries[version].get("by_category", {}) for version in versions
            },
            "notes": [
                "Metrics are measured from executed runs; no synthetic values.",
                "The headless client executes the same action protocol as the frontend.",
                "Raw PII is never persisted in reports; only masked previews.",
            ],
        }

        eval_run.status = "completed"
        eval_run.total_scenarios = len(results)
        eval_run.passed = passed
        eval_run.failed = len(results) - passed
        eval_run.p50_latency_ms = int(
            summarise_results(results)["p50_latency_ms"]
        )
        eval_run.p95_latency_ms = int(
            summarise_results(results)["p95_latency_ms"]
        )
        eval_run.total_cost_usd = round(sum(float(r["cost_usd"]) for r in results), 6)
        eval_run.summary = summary_payload
        eval_run.finished_at = finished_at
        db.commit()

        json_path, markdown_path = _write_reports(
            run_id=run_id,
            label=label,
            provider=provider,
            model=model,
            versions=versions,
            scenario_count=len(scenarios),
            started_at=started_at,
            finished_at=finished_at,
            results=results,
            summaries=summaries,
            comparison=comparison,
            reports_dir=reports_dir,
        )
        logger.info(
            "eval.run.completed run_id=%s executions=%s pass_rate=%s",
            run_id,
            len(results),
            round(passed / max(1, len(results)), 4),
        )

        report = {
            "run": {
                "id": run_id,
                "label": label,
                "provider": provider,
                "model": model,
                "prompt_versions": versions,
                "scenario_count": len(scenarios),
                "executions": len(results),
                "started_at": started_at.isoformat(),
                "finished_at": finished_at.isoformat(),
                "passed": passed,
                "failed": len(results) - passed,
            },
            "summaries": summaries,
            "comparison": comparison,
            "results": results,
        }
        return EvalArtifacts(report=report, json_path=json_path, markdown_path=markdown_path)
    finally:
        db.close()


def _per_scenario_comparison(by_version: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    versions = sorted(by_version)
    if len(versions) < 2:
        return {}
    baseline, candidate = versions[0], versions[-1]
    base_map = {row["scenario_id"]: row["passed"] for row in by_version[baseline]}
    cand_map = {row["scenario_id"]: row["passed"] for row in by_version[candidate]}
    improved: list[str] = []
    regressed: list[str] = []
    for scenario_id, base_passed in base_map.items():
        cand_passed = cand_map.get(scenario_id, False)
        if cand_passed and not base_passed:
            improved.append(scenario_id)
        elif base_passed and not cand_passed:
            regressed.append(scenario_id)
    return {
        "baseline": baseline,
        "candidate": candidate,
        "improved": sorted(improved),
        "regressed": sorted(regressed),
        "both_passed": sorted(sid for sid, ok in base_map.items() if ok and cand_map.get(sid)),
        "both_failed": sorted(sid for sid, ok in base_map.items() if not ok and not cand_map.get(sid)),
    }


# ---------------------------------------------------------------------------
# Read helpers used by the API/dashboard
# ---------------------------------------------------------------------------


def list_scenarios() -> list[dict[str, Any]]:
    return [scenario_summary(scenario) for scenario in load_scenarios()]


def load_report(path: Path | None = None) -> dict[str, Any]:
    target = path or (settings.reports_dir / "latest.json")
    if not target.exists():
        raise FileNotFoundError(f"report not found: {target}")
    return json.loads(target.read_text(encoding="utf-8"))


def ensure_scenarios_dir() -> None:
    if not settings.scenarios_dir.exists():
        os.makedirs(settings.scenarios_dir, exist_ok=True)
