"""Scenario discovery, validation and persistence.

Scenario JSON files under ``EVALS_DIR`` are the source of truth. They are
validated with the same Pydantic model the harness uses, so a malformed
expectation fails loudly instead of silently scoring zero.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import get_logger
from app.models.entities import EvaluationScenario
from app.schemas import Scenario

logger = get_logger(__name__)

CATEGORY_ORDER = ("PAN", "OCR", "MANDATE", "INTENT")


def load_scenarios(
    directory: Path | None = None,
    *,
    scenario_ids: Iterable[str] | None = None,
    categories: Iterable[str] | None = None,
) -> list[Scenario]:
    directory = directory or settings.scenarios_dir
    if not directory.exists():
        raise FileNotFoundError(f"scenario directory does not exist: {directory}")

    wanted_ids = set(scenario_ids or [])
    wanted_categories = {c.upper() for c in (categories or [])}
    scenarios: list[Scenario] = []
    seen: set[str] = set()

    for path in sorted(directory.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            raise ValueError(f"{path.name}: expected a JSON array of scenarios")
        for item in raw:
            scenario = Scenario.model_validate(item)
            if scenario.id in seen:
                raise ValueError(f"duplicate scenario id: {scenario.id}")
            seen.add(scenario.id)
            if wanted_ids and scenario.id not in wanted_ids:
                continue
            if wanted_categories and scenario.category.upper() not in wanted_categories:
                continue
            scenarios.append(scenario)

    if wanted_ids:
        missing = wanted_ids - seen
        if missing:
            raise ValueError(f"unknown scenario ids: {', '.join(sorted(missing))}")

    scenarios.sort(key=lambda s: (CATEGORY_ORDER.index(s.category), s.id))
    logger.info("eval.scenarios.loaded count=%s dir=%s", len(scenarios), directory)
    return scenarios


def sync_scenarios(db: Session, scenarios: list[Scenario]) -> int:
    """Upsert scenarios so the dashboard can browse them without file access."""
    count = 0
    for scenario in scenarios:
        row = db.get(EvaluationScenario, scenario.id)
        if row is None:
            row = EvaluationScenario(scenario_id=scenario.id)
            db.add(row)
        row.category = scenario.category
        row.fail_class = scenario.fail_class
        row.screen_id = scenario.screen
        row.description = scenario.description
        row.intent = scenario.intent
        row.seed = scenario.seed
        row.expect = scenario.expect
        count += 1
    db.flush()
    return count


def stored_scenarios(db: Session) -> list[EvaluationScenario]:
    return list(
        db.scalars(select(EvaluationScenario).order_by(EvaluationScenario.category, EvaluationScenario.scenario_id)).all()
    )


def scenario_summary(scenario: Scenario) -> dict:
    return {
        "id": scenario.id,
        "category": scenario.category,
        "fail_class": scenario.fail_class,
        "screen": scenario.screen,
        "description": scenario.description,
        "intent": scenario.intent,
        "expect": scenario.expect,
    }
