"""Idempotent seed data: policy corpus + evaluation scenarios.

Run manually:
    python -m app.db.seed
or automatically on startup (development) from ``app.main``.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import configure_logging, get_logger
from app.db.base import Base
from app.db.session import SessionLocal, engine
from app.evaluation.scenarios import load_scenarios, sync_scenarios
from app.models.entities import KYCPolicy
from app.services.policies import load_documents

logger = get_logger(__name__)


def seed_policies(db: Session) -> int:
    count = 0
    for document in load_documents():
        row = db.get(KYCPolicy, document.ref)
        if row is None:
            row = KYCPolicy(ref=document.ref)
            db.add(row)
        row.title = document.title
        row.category = document.category
        row.content = document.content
        row.version = document.version
        row.tags = list(document.tags)
        count += 1
    db.flush()
    return count


def seed_scenarios(db: Session) -> int:
    try:
        scenarios = load_scenarios()
    except FileNotFoundError:
        logger.warning("seed.scenarios.missing dir=%s", settings.scenarios_dir)
        return 0
    return sync_scenarios(db, scenarios)


def seed_all(db: Session) -> dict[str, int]:
    policies = seed_policies(db)
    scenarios = seed_scenarios(db)
    db.flush()
    logger.info("seed.completed policies=%s scenarios=%s", policies, scenarios)
    return {"policies": policies, "scenarios": scenarios}


def main() -> int:
    configure_logging()
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        counts = seed_all(db)
        db.commit()
    print(f"Seeded {counts['policies']} policies and {counts['scenarios']} evaluation scenarios.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
