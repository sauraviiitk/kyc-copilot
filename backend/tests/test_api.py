"""API contract tests: live context, KYC endpoints, SSE and action protocol."""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.runner import create_run, execute_run
from app.models.entities import AgentAction, AuditLog, PIIRecord, ScreenState, UserSession
from app.schemas import ScreenContext
from app.services.journey import upsert_screen_state
from app.services.pii import value_hash
from tests.conftest import dump

PAN = "ABCDE1234F"
LOWER_PAN = "abcde1234f"


def _publish(db: Session, session: UserSession, screen: str, fields: list[dict[str, Any]]) -> None:
    context = ScreenContext(session_id=session.id, screen_id=screen, fields=fields)
    upsert_screen_state(db, session, context)
    db.commit()


def test_health_reports_offline_mocks(client: TestClient) -> None:
    response = client.get("/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["checks"]["llm"]["effective"] == "mock"
    assert body["checks"]["policy_store"]["documents"] >= 1
    assert body["kill_switch"]["enabled"] is False


def test_screen_context_is_live_and_masked_at_rest(
    client: TestClient, db: Session, session: UserSession
) -> None:
    created = client.post("/api/sessions", json={"consent_given": True})
    assert created.status_code == 200
    session_id = created.json()["session_id"]

    first = client.post(
        "/api/screen-context/update",
        json={
            "session_id": session_id,
            "context": {
                "screen_id": "kyc.pan_entry",
                "fields": [{"name": "pan", "value": PAN}],
            },
            "reason": "first publish",
        },
    )
    assert first.status_code == 200 and first.json()["revision"] == 1

    current = client.get("/api/screen-context", params={"session_id": session_id})
    assert current.status_code == 200
    field = next(f for f in current.json()["context"]["fields"] if f["name"] == "pan")
    assert field["value"] == PAN  # restored for the owning client only
    assert field["sensitive"] is True

    stored = db.scalar(
        select(ScreenState).where(ScreenState.session_id == session_id).order_by(ScreenState.revision.desc())
    )
    assert stored is not None and PAN not in dump(stored.context)
    vault_rows = list(db.scalars(select(PIIRecord).where(PIIRecord.session_id == session_id)).all())
    assert vault_rows and all(PAN not in row.ciphertext for row in vault_rows)

    second = client.post(
        "/api/screen-context/update",
        json={
            "session_id": session_id,
            "context": {
                "screen_id": "kyc.address",
                "fields": [{"name": "city", "value": "Mumbai"}],
            },
            "reason": "navigated",
        },
    )
    assert second.json()["revision"] == 2
    updated = client.get("/api/screen-context", params={"session_id": session_id}).json()
    assert updated["screen_id"] == "kyc.address"
    assert updated["context"]["fields"][0]["value"] == "Mumbai"
    assert updated["context"]["actions"] == ["highlight", "fill", "submit", "navigate", "escalate"]


def test_pan_endpoint_reports_structural_error_then_verifies(
    client: TestClient, db: Session, session: UserSession
) -> None:
    base = "/api/kyc/pan/verify"
    failed = client.post(
        base, json={"session_id": session.id, "full_name": "Aarav Sharma", "pan": LOWER_PAN}
    )
    assert failed.status_code == 200
    assert failed.json()["status"] == "failed"
    assert failed.json()["error_code"] == "pan_lowercase"

    ok = client.post(
        base, json={"session_id": session.id, "full_name": "Aarav Sharma", "pan": PAN}
    )
    assert ok.status_code == 200
    assert ok.json()["status"] == "verified"
    assert ok.json()["next_screen"] == "kyc.document_upload"

    replay = client.post(
        base, json={"session_id": session.id, "full_name": "Aarav Sharma", "pan": PAN}
    )
    assert replay.json()["status"] == "verified"
    assert replay.json()["attempts"] == ok.json()["attempts"]

    audit = list(db.scalars(select(AuditLog).where(AuditLog.session_id == session.id)).all())
    assert audit and PAN not in dump([row.detail for row in audit])
    assert LOWER_PAN not in dump([row.detail for row in audit])


def test_mandate_submit_is_idempotent_through_api(
    client: TestClient, db: Session, session: UserSession
) -> None:
    payload = {
        "session_id": session.id,
        "bank": "HDFC Bank",
        "ifsc": "HDFC0001234",
        "account_number": "12345678901234",
        "idempotency_key": "test-key-1234",
    }
    first = client.post("/api/kyc/mandate/submit", json=payload)
    assert first.status_code == 200
    assert first.json()["status"] == "verified"
    assert first.json()["idempotent_replay"] is False

    replay = client.post("/api/kyc/mandate/submit", json={**payload, "account_number": "99999999999999"})
    assert replay.json()["idempotent_replay"] is True
    assert replay.json()["mandate_ref"] == first.json()["mandate_ref"]

    status = client.get(f"/api/kyc/mandate/{session.id}").json()
    assert status["status"] == "verified"
    assert status["masked_account"] == "••••••••••1234"
    assert "12345678901234" not in dump(status)


def test_agent_stream_replays_named_events_without_fill_values(
    client: TestClient, db: Session, session: UserSession
) -> None:
    _publish(db, session, "kyc.pan_entry", [{"name": "pan", "value": PAN}])

    response = client.post(
        "/api/agent/run",
        json={"session_id": session.id, "message": "please check my PAN", "prompt_version": "v2"},
    )
    assert response.status_code == 200
    run_id = response.json()["run_id"]

    detail: dict[str, Any] = {}
    for _ in range(100):
        detail = client.get(f"/api/agent/runs/{run_id}").json()
        if detail["run"]["status"] not in {"queued", "running"}:
            break
        time.sleep(0.05)
    assert detail["run"]["status"] in {"completed", "awaiting_result", "awaiting_confirmation"}
    assert detail["run"]["prompt_version"] == "v2"
    assert detail["run"]["cited_policy_refs"]

    streamed = client.get(f"/api/agent/stream/{run_id}", params={"session_id": session.id})
    assert streamed.status_code == 200
    body = streamed.text
    assert "event: agent_plan" in body
    assert "event: agent_complete" in body
    assert "event: stream_end" in body

    events: list[tuple[str, dict[str, Any]]] = []
    name = ""
    for line in body.splitlines():
        if line.startswith("event: "):
            name = line[len("event: ") :]
        elif line.startswith("data: ") and name:
            events.append((name, json.loads(line[len("data: ") :])))
    assert {name for name, _ in events} >= {"agent_plan", "agent_message", "agent_complete", "stream_end"}
    assert all(
        PAN not in dump(data)
        for name, data in events
        if name in {"action", "action_result"}
    )
    assert PAN not in dump(detail["actions"])


def test_fill_action_value_requires_hash_attestation(
    client: TestClient, db: Session, session: UserSession
) -> None:
    _publish(db, session, "kyc.pan_entry", [{"name": "pan", "value": LOWER_PAN}])
    run = create_run(db, session_id=session.id, message="fix the case", prompt_version="v2")
    db.commit()
    execute_run(db, session=session, run=run, message="fix the case", prompt_version="v2")
    db.commit()

    fill = db.scalar(
        select(AgentAction).where(AgentAction.run_id == run.id, AgentAction.action_type == "fill")
    )
    assert fill is not None
    assert fill.value_masked != PAN  # only a surrogate preview is stored

    url = f"/api/agent/actions/{fill.id}/value"
    value = client.get(url, params={"session_id": session.id})
    assert value.status_code == 200
    assert value.json()["value"] == PAN
    assert value.json()["value_hash"] == value_hash(PAN, "pan")

    base = {
        "session_id": session.id,
        "run_id": run.id,
        "action_id": fill.id,
        "status": "applied",
    }
    bad = client.post("/api/agent/action-result", json={**base, "executed_value_hash": "wrong"})
    assert bad.status_code == 200
    assert bad.json()["ok"] is False
    assert bad.json()["action_status"] == "failed"

    # A failed attestation is terminal: the correct hash cannot revive the action.
    replay = client.post(
        "/api/agent/action-result",
        json={**base, "executed_value_hash": value_hash(PAN, "pan")},
    )
    assert replay.json()["action_status"] == "failed"

    # A fresh action with the correct hash is accepted.
    second = create_run(db, session_id=session.id, message="fix the case", prompt_version="v2")
    db.commit()
    execute_run(db, session=session, run=second, message="fix the case", prompt_version="v2")
    db.commit()
    second_fill = db.scalar(
        select(AgentAction).where(
            AgentAction.run_id == second.id, AgentAction.action_type == "fill"
        )
    )
    assert second_fill is not None
    good = client.post(
        "/api/agent/action-result",
        json={
            "session_id": session.id,
            "run_id": second.id,
            "action_id": second_fill.id,
            "status": "applied",
            "executed_value_hash": value_hash(PAN, "pan"),
        },
    )
    assert good.status_code == 200
    assert good.json()["action_status"] == "applied"
    assert good.json()["ok"] is True

    run_detail = client.get(f"/api/agent/runs/{second.id}").json()
    for pending in run_detail["actions"]:
        if pending["id"] == second_fill.id or pending["status"] != "dispatched":
            continue
        client.post(
            "/api/agent/action-result",
            json={
                "session_id": session.id,
                "run_id": second.id,
                "action_id": pending["id"],
                "status": "applied",
            },
        )
    fresh = client.get(f"/api/agent/runs/{second.id}").json()
    assert fresh["run"]["status"] == "completed"
