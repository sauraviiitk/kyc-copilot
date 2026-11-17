"""Pydantic contracts shared by the API, agent and evaluation harness."""

from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Screens & fields
# ---------------------------------------------------------------------------

SCREEN_IDS = (
    "kyc.pan_entry",
    "kyc.document_upload",
    "kyc.bank_mandate",
    "kyc.address",
    "kyc.review",
)

SCREEN_STEPS: dict[str, int] = {
    "kyc.pan_entry": 1,
    "kyc.document_upload": 2,
    "kyc.bank_mandate": 3,
    "kyc.address": 4,
    "kyc.review": 5,
}

# Fields the agent is allowed to mutate, per screen. Anything else is rejected
# by the permission layer, regardless of what the LLM asks for.
MUTABLE_FIELDS: dict[str, tuple[str, ...]] = {
    "kyc.pan_entry": ("full_name", "pan"),
    "kyc.document_upload": (),
    "kyc.bank_mandate": (),
    "kyc.address": ("house_number", "street", "city", "state", "pin"),
    "kyc.review": (),
}

SENSITIVE_FIELDS: dict[str, str] = {
    "pan": "PAN",
    "aadhaar": "AADHAAR",
    "account_number": "ACCOUNT",
    "phone": "PHONE",
    "email": "EMAIL",
    "full_name": "NAME",
    "house_number": "ADDRESS",
    "street": "ADDRESS",
    "pin": "PINCODE",
}

ALLOWED_ACTIONS: dict[str, tuple[str, ...]] = {
    "kyc.pan_entry": ("highlight", "fill", "submit", "navigate", "escalate"),
    "kyc.document_upload": ("highlight", "upload", "retry", "navigate", "escalate"),
    "kyc.bank_mandate": ("highlight", "submit", "retry", "navigate", "escalate"),
    "kyc.address": ("highlight", "fill", "submit", "navigate", "escalate"),
    "kyc.review": ("navigate", "submit", "escalate"),
}


class ActionType(StrEnum):
    highlight = "highlight"
    fill = "fill"
    upload = "upload"
    retry = "retry"
    navigate = "navigate"
    escalate = "escalate"


class FieldContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    name: str
    label: str = ""
    type: str = "text"
    required: bool = True
    pattern: str | None = None
    value: str | None = None
    error: str | None = None
    options: list[str] | None = None
    sensitive: bool = False
    disabled: bool = False
    hint: str | None = None

    @field_validator("value", mode="before")
    @classmethod
    def _stringify(cls, value: Any) -> Any:
        if value is None:
            return None
        return str(value)


class DocumentContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    document_id: str | None = None
    filename: str | None = None
    doc_type: str | None = None
    status: str | None = None
    quality_score: float | None = None
    ocr_confidence: float | None = None
    rotation_degrees: float | None = None
    blur_score: float | None = None
    issues: list[str] = Field(default_factory=list)


class MandateContext(BaseModel):
    model_config = ConfigDict(extra="allow")

    mandate_ref: str | None = None
    status: str | None = None
    attempts: int = 0
    max_attempts: int = 3
    last_error: str | None = None
    idempotency_key: str | None = None


class ScreenContext(BaseModel):
    """Live snapshot of what the user sees, posted by the frontend."""

    model_config = ConfigDict(extra="allow")

    session_id: str | None = None
    screen_id: str = Field(default="kyc.pan_entry")
    step: int = Field(default=1, ge=1, le=5)
    title: str = ""
    fields: list[FieldContext] = Field(default_factory=list)
    actions: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
    policy_refs: list[str] = Field(default_factory=list)
    document: DocumentContext | None = None
    mandate: MandateContext | None = None
    status: dict[str, Any] = Field(default_factory=dict)
    revision: int = 0
    captured_at: datetime | None = None

    @field_validator("screen_id")
    @classmethod
    def _known_screen(cls, value: str) -> str:
        if value not in SCREEN_IDS:
            raise ValueError(f"unknown screen_id: {value}")
        return value

    @model_validator(mode="after")
    def _sanitize_allowed(self) -> ScreenContext:
        # Never trust the client's allowed-action list: derive it server side.
        self.actions = list(ALLOWED_ACTIONS.get(self.screen_id, ()))
        self.step = SCREEN_STEPS.get(self.screen_id, self.step)
        for field in self.fields:
            field.sensitive = field.name in SENSITIVE_FIELDS
        return self

    def field(self, name: str) -> FieldContext | None:
        return next((f for f in self.fields if f.name == name), None)


class ScreenContextUpdate(BaseModel):
    session_id: str
    context: ScreenContext
    reason: str = "field_change"


class ScreenContextRead(BaseModel):
    session_id: str
    screen_id: str
    step: int
    revision: int
    updated_at: datetime | None = None
    context: ScreenContext


# ---------------------------------------------------------------------------
# Agent structured output
# ---------------------------------------------------------------------------


class ToolName(StrEnum):
    set_field = "set_field"
    highlight = "highlight"
    upload_doc = "upload_doc"
    retry_step = "retry_step"
    navigate = "navigate"
    escalate_to_human = "escalate_to_human"
    search_policy = "search_policy"


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: ToolName
    args: dict[str, Any] = Field(default_factory=dict)


class AgentDecision(BaseModel):
    """Strict schema the LLM must satisfy. Anything else is rejected."""

    model_config = ConfigDict(extra="forbid")

    plan: str = Field(min_length=1, max_length=4000)
    say: str = Field(min_length=1, max_length=4000)
    tool_calls: list[ToolCall] = Field(default_factory=list, max_length=8)
    confidence: float = Field(ge=0.0, le=1.0)
    requires_confirmation: bool = False
    confirmation_prompt: str | None = Field(default=None, max_length=500)
    cited_policy_refs: list[str] = Field(default_factory=list, max_length=8)
    escalate: bool = False
    escalation_reason: str | None = Field(default=None, max_length=1000)


class ActionProposal(BaseModel):
    """Validated, permission-checked action ready for client execution."""

    action_id: str
    type: ActionType
    field: str | None = None
    target: str | None = None
    step_id: str | None = None
    idempotency_key: str | None = None
    value_preview: str | None = None
    requires_confirmation: bool = False
    sensitive: bool = False
    reason: str = ""


class AgentRunRequest(BaseModel):
    session_id: str
    message: str = Field(min_length=1, max_length=2000)
    prompt_version: Literal["v1", "v2"] = "v2"
    client_action_results: dict[str, Any] | None = None

    @field_validator("message")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class AgentRunResponse(BaseModel):
    run_id: str
    session_id: str
    status: str
    stream_url: str


class ActionResultRequest(BaseModel):
    run_id: str
    action_id: str
    session_id: str
    status: Literal["applied", "failed", "rejected", "cancelled"]
    detail: dict[str, Any] = Field(default_factory=dict)
    executed_value_hash: str | None = None


class ActionResultResponse(BaseModel):
    ok: bool
    run_status: str
    action_status: str
    message: str


class ActionValueResponse(BaseModel):
    action_id: str
    field: str | None
    value: str
    value_hash: str | None = None
    expires_at: datetime | None = None


# ---------------------------------------------------------------------------
# KYC domain contracts
# ---------------------------------------------------------------------------

PAN_PATTERN = r"^[A-Z]{5}[0-9]{4}[A-Z]$"
PAN_RE = re.compile(PAN_PATTERN)
PIN_PATTERN = r"^[1-9][0-9]{5}$"
PIN_RE = re.compile(PIN_PATTERN)
IFSC_PATTERN = r"^[A-Z]{4}0[A-Z0-9]{6}$"
IFSC_RE = re.compile(IFSC_PATTERN)

INDIAN_STATES = (
    "Andhra Pradesh",
    "Delhi",
    "Gujarat",
    "Haryana",
    "Karnataka",
    "Kerala",
    "Madhya Pradesh",
    "Maharashtra",
    "Punjab",
    "Rajasthan",
    "Tamil Nadu",
    "Telangana",
    "Uttar Pradesh",
    "West Bengal",
)

CITY_SUGGESTIONS: dict[str, tuple[str, ...]] = {
    "Karnataka": ("Bengaluru", "Mysuru", "Mangaluru"),
    "Maharashtra": ("Mumbai", "Pune", "Nagpur"),
    "Delhi": ("New Delhi", "Dwarka", "Rohini"),
    "Tamil Nadu": ("Chennai", "Coimbatore", "Madurai"),
    "Telangana": ("Hyderabad", "Warangal", "Nizamabad"),
    "Gujarat": ("Ahmedabad", "Surat", "Vadodara"),
    "West Bengal": ("Kolkata", "Howrah", "Siliguri"),
}


class PANVerifyRequest(BaseModel):
    session_id: str
    full_name: str = Field(min_length=1, max_length=120)
    pan: str = Field(min_length=1, max_length=20)
    idempotency_key: str | None = None


class PANVerifyResponse(BaseModel):
    status: Literal["verified", "failed"]
    message: str
    error_code: str | None = None
    attempts: int
    remaining_attempts: int
    next_screen: str | None = None
    normalized_pan_hint: str | None = None


class DocumentQuality(BaseModel):
    quality_score: float
    ocr_confidence: float
    rotation_degrees: float
    blur_score: float
    issues: list[str] = Field(default_factory=list)
    acceptable: bool = False


class DocumentUploadResponse(BaseModel):
    document_id: str
    doc_type: str
    filename: str
    content_type: str
    size_bytes: int
    quality: DocumentQuality
    ocr: dict[str, Any]
    status: Literal["accepted", "rejected", "needs_review"]
    message: str
    requires_user_confirmation: bool = False


class DocumentAcceptRequest(BaseModel):
    session_id: str
    document_id: str
    confirm: bool = False


class MandateSubmitRequest(BaseModel):
    session_id: str
    bank: str = Field(min_length=2, max_length=64)
    ifsc: str = Field(min_length=11, max_length=11)
    account_number: str = Field(min_length=9, max_length=18)
    idempotency_key: str | None = Field(default=None, max_length=128)


class MandateResponse(BaseModel):
    mandate_ref: str
    status: Literal["pending", "verified", "failed", "unknown"]
    attempts: int
    max_attempts: int
    message: str
    error_code: str | None = None
    idempotency_key: str
    idempotent_replay: bool = False
    retryable: bool = False
    requires_confirmation: bool = False


class MandateRetryRequest(BaseModel):
    session_id: str
    idempotency_key: str = Field(min_length=4, max_length=128)


class AddressVerifyRequest(BaseModel):
    session_id: str
    house_number: str = Field(min_length=1, max_length=20)
    street: str = Field(min_length=2, max_length=120)
    city: str = Field(min_length=2, max_length=60)
    state: str = Field(min_length=2, max_length=60)
    pin: str = Field(min_length=1, max_length=10)


class AddressVerifyResponse(BaseModel):
    status: Literal["verified", "failed"]
    message: str
    field_errors: dict[str, str] = Field(default_factory=dict)
    suggestions: dict[str, list[str]] = Field(default_factory=dict)
    next_screen: str | None = None


# ---------------------------------------------------------------------------
# Evaluation contracts
# ---------------------------------------------------------------------------


class Scenario(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    category: Literal["PAN", "OCR", "MANDATE", "INTENT"]
    fail_class: str
    screen: str
    description: str = ""
    seed: dict[str, Any] = Field(default_factory=dict)
    intent: str
    expect: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def _id_shape(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z0-9_]{3,64}", value):
            raise ValueError("scenario id must be snake_case")
        return value


class EvalRunRequest(BaseModel):
    label: str | None = None
    prompt_versions: list[Literal["v1", "v2"]] = Field(default_factory=lambda: ["v1", "v2"])
    scenario_ids: list[str] | None = None
    categories: list[Literal["PAN", "OCR", "MANDATE", "INTENT"]] | None = None


class EvalMetric(BaseModel):
    key: str
    label: str
    value: float
    unit: str = ""
    direction: Literal["higher_is_better", "lower_is_better"] = "higher_is_better"


class EvalRunSummary(BaseModel):
    run_id: str
    label: str
    status: str
    prompt_versions: list[str]
    provider: str
    model: str
    total: int
    passed: int
    failed: int
    pass_rate: float
    p50_latency_ms: int
    p95_latency_ms: int
    total_cost_usd: float
    hallucinated_actions: int
    tool_success_rate: float
    policy_grounding_accuracy: float
    categories: dict[str, dict[str, float]]
    started_at: datetime
    finished_at: datetime | None = None


class EvalResultOut(BaseModel):
    id: str
    scenario_id: str
    prompt_version: str
    category: str
    screen_id: str
    passed: bool
    checks: dict[str, Any]
    failure_reasons: list[str]
    actual_actions: list[str]
    actual: dict[str, Any]
    agent_run_id: str | None
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


# ---------------------------------------------------------------------------
# Ops
# ---------------------------------------------------------------------------


class KillSwitchRequest(BaseModel):
    enabled: bool
    reason: str = Field(default="manual", max_length=300)
    admin_token: str


class MetricsResponse(BaseModel):
    counters: dict[str, float]
    histograms: dict[str, dict[str, float]]
    generated_at: datetime
