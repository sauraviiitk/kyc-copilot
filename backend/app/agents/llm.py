"""LLM provider abstraction.

Providers: Gemini (REST), Groq (OpenAI-compatible REST), and a deterministic
offline planner. All of them return a validated `AgentDecision`.

Every call is bounded: explicit HTTP timeouts, a retry budget, and a schema
repair round. If the configured provider keeps failing, the runner degrades to
the mock planner and marks the run as degraded instead of failing the user.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from app.agents.prompts import PromptSpec, build_user_prompt, get_prompt
from app.core.config import settings
from app.core.logging import get_logger
from app.observability.metrics import incr, observe
from app.schemas import AgentDecision

logger = get_logger(__name__)

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GROQ_ENDPOINT = "https://api.groq.com/openai/v1/chat/completions"

TOOL_NAMES = ["set_field", "highlight", "upload_doc", "retry_step", "escalate_to_human", "search_policy"]

DECISION_SCHEMA: dict[str, Any] = {
    "type": "OBJECT",
    "properties": {
        "plan": {"type": "STRING"},
        "say": {"type": "STRING"},
        "tool_calls": {
            "type": "ARRAY",
            "items": {
                "type": "OBJECT",
                "properties": {
                    "tool": {"type": "STRING", "enum": TOOL_NAMES},
                    "args": {
                        "type": "OBJECT",
                        "properties": {
                            "field": {"type": "STRING"},
                            "value": {"type": "STRING"},
                            "document_id": {"type": "STRING"},
                            "step_id": {"type": "STRING"},
                            "idempotency_key": {"type": "STRING"},
                            "reason": {"type": "STRING"},
                            "query": {"type": "STRING"},
                        },
                    },
                },
                "required": ["tool"],
            },
        },
        "confidence": {"type": "NUMBER"},
        "requires_confirmation": {"type": "BOOLEAN"},
        "confirmation_prompt": {"type": "STRING"},
        "cited_policy_refs": {"type": "ARRAY", "items": {"type": "STRING"}},
        "escalate": {"type": "BOOLEAN"},
        "escalation_reason": {"type": "STRING"},
    },
    "required": ["plan", "say", "tool_calls", "confidence", "requires_confirmation", "cited_policy_refs"],
}


@dataclass
class PlanRequest:
    session_id: str
    run_id: str
    message_masked: str
    raw_message: str
    screen_context: dict[str, Any]
    policies: list[dict[str, Any]]
    prompt_version: str
    limits: dict[str, Any] = field(default_factory=dict)
    raw_context: dict[str, Any] = field(default_factory=dict)

    @property
    def spec(self) -> PromptSpec:
        return get_prompt(self.prompt_version)

    def user_prompt(self) -> str:
        return build_user_prompt(
            version=self.prompt_version,
            screen_context=self.screen_context,
            message_masked=self.message_masked,
            policies=self.policies,
            limits=self.limits,
        )


@dataclass
class LLMResult:
    decision: AgentDecision
    provider: str
    model: str
    latency_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    attempts: int = 1
    degraded: bool = False
    error: str | None = None
    raw_text: str | None = None


class LLMError(RuntimeError):
    def __init__(self, message: str, *, raw: str | None = None):
        super().__init__(message)
        self.raw = raw


class LLMProvider(Protocol):
    provider: str
    model: str

    def plan(self, request: PlanRequest) -> LLMResult: ...


def extract_json(text: str) -> dict[str, Any]:
    if not text or not text.strip():
        raise LLMError("empty model response")
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```[a-zA-Z]*\s*", "", cleaned)
        cleaned = re.sub(r"\s*```$", "", cleaned)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            candidate = cleaned[start : end + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError as exc:
                raise LLMError(f"invalid JSON: {exc}", raw=text[:500]) from exc
        raise LLMError("model response contained no JSON object", raw=text[:500]) from None


def validate_decision(payload: dict[str, Any]) -> AgentDecision:
    try:
        return AgentDecision.model_validate(payload)
    except ValidationError as exc:
        raise LLMError(f"schema validation failed: {exc.errors()[:2]}", raw=json.dumps(payload)[:500]) from exc


class GeminiProvider:
    provider = "gemini"

    def __init__(self) -> None:
        self.api_key = settings.gemini_api_key or ""
        self.model = settings.gemini_model

    def plan(self, request: PlanRequest) -> LLMResult:
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not configured")
        body = {
            "systemInstruction": {"parts": [{"text": request.spec.system}]},
            "contents": [{"role": "user", "parts": [{"text": request.user_prompt()}]}],
            "generationConfig": {
                "temperature": 0.1,
                "maxOutputTokens": settings.llm_max_output_tokens,
                "responseMimeType": "application/json",
                "responseSchema": DECISION_SCHEMA,
            },
        }
        last_error: Exception | None = None
        attempt = 0
        for attempt in range(1, settings.llm_max_retries + 2):
            start = time.perf_counter()
            try:
                with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
                    response = client.post(
                        GEMINI_ENDPOINT.format(model=self.model),
                        params={"key": self.api_key},
                        json=body,
                    )
                latency = int((time.perf_counter() - start) * 1000)
                observe("llm.latency_ms", latency, provider=self.provider)
                if response.status_code >= 400:
                    raise LLMError(f"gemini http {response.status_code}: {response.text[:200]}")
                payload = response.json()
                text = _gemini_text(payload)
                usage = payload.get("usageMetadata", {})
                decision = validate_decision(extract_json(text))
                return LLMResult(
                    decision=decision,
                    provider=self.provider,
                    model=self.model,
                    latency_ms=latency,
                    input_tokens=int(usage.get("promptTokenCount") or 0),
                    output_tokens=int(usage.get("candidatesTokenCount") or 0),
                    attempts=attempt,
                    raw_text=text[:2000],
                )
            except (LLMError, httpx.HTTPError, ValueError) as exc:
                last_error = exc
                incr("llm.errors", provider=self.provider)
                logger.warning("llm.gemini.attempt_failed attempt=%s: %s", attempt, exc)
                if attempt <= settings.llm_max_retries:
                    time.sleep(min(2.0, 0.35 * (2 ** (attempt - 1))))
        raise LLMError(f"gemini failed after retries: {last_error}")


def _gemini_text(payload: dict[str, Any]) -> str:
    candidates = payload.get("candidates") or []
    if not candidates:
        feedback = payload.get("promptFeedback") or {}
        raise LLMError(f"gemini returned no candidates ({feedback.get('blockReason') or 'unknown'})")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    return "".join(part.get("text", "") for part in parts)


class GroqProvider:
    provider = "groq"

    def __init__(self) -> None:
        self.api_key = settings.groq_api_key or ""
        self.model = settings.groq_model

    def plan(self, request: PlanRequest) -> LLMResult:
        if not self.api_key:
            raise LLMError("GROQ_API_KEY is not configured")
        body = {
            "model": self.model,
            "temperature": 0.1,
            "max_tokens": settings.llm_max_output_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": request.spec.system},
                {"role": "user", "content": request.user_prompt()},
            ],
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}
        last_error: Exception | None = None
        for attempt in range(1, settings.llm_max_retries + 2):
            start = time.perf_counter()
            try:
                with httpx.Client(timeout=settings.llm_timeout_seconds) as client:
                    response = client.post(GROQ_ENDPOINT, headers=headers, json=body)
                latency = int((time.perf_counter() - start) * 1000)
                observe("llm.latency_ms", latency, provider=self.provider)
                if response.status_code >= 400:
                    raise LLMError(f"groq http {response.status_code}: {response.text[:200]}")
                payload = response.json()
                text = payload["choices"][0]["message"]["content"]
                usage = payload.get("usage", {})
                decision = validate_decision(extract_json(text))
                return LLMResult(
                    decision=decision,
                    provider=self.provider,
                    model=self.model,
                    latency_ms=latency,
                    input_tokens=int(usage.get("prompt_tokens") or 0),
                    output_tokens=int(usage.get("completion_tokens") or 0),
                    attempts=attempt,
                    raw_text=text[:2000],
                )
            except (LLMError, httpx.HTTPError, KeyError, ValueError) as exc:
                last_error = exc
                incr("llm.errors", provider=self.provider)
                logger.warning("llm.groq.attempt_failed attempt=%s: %s", attempt, exc)
                if attempt <= settings.llm_max_retries:
                    time.sleep(min(2.0, 0.35 * (2 ** (attempt - 1))))
        raise LLMError(f"groq failed after retries: {last_error}")


class MockProvider:
    """Deterministic offline planner.

    Implements the same contract as the hosted providers so the entire product
    (including the evaluation harness) runs without API keys.
    """

    provider = "mock"

    def __init__(self, prompt_version: str = "v2") -> None:
        self.model = f"deterministic-planner:{prompt_version}"
        self.prompt_version = prompt_version

    def plan(self, request: PlanRequest) -> LLMResult:
        from app.agents.mock_planner import plan_offline

        start = time.perf_counter()
        decision = plan_offline(request)
        latency = int((time.perf_counter() - start) * 1000) + 8
        prompt_chars = len(request.user_prompt()) + len(request.spec.system)
        return LLMResult(
            decision=decision,
            provider=self.provider,
            model=self.model,
            latency_ms=latency,
            input_tokens=max(1, prompt_chars // 4),
            output_tokens=max(1, len(decision.model_dump_json()) // 4),
            attempts=1,
        )


def get_provider(prompt_version: str) -> LLMProvider:
    provider = settings.effective_llm_provider()
    if provider == "gemini":
        return GeminiProvider()
    if provider == "groq":
        return GroqProvider()
    return MockProvider(prompt_version)


def provider_health() -> dict[str, Any]:
    return {
        "configured": settings.llm_provider,
        "effective": settings.effective_llm_provider(),
        "gemini_model": settings.gemini_model if settings.gemini_api_key else None,
        "groq_model": settings.groq_model if settings.groq_api_key else None,
        "mock_available": True,
    }
