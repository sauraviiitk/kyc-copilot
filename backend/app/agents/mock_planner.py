"""Deterministic offline planner.

This is a rule engine, not an LLM. It exists so the full product and the whole
evaluation harness run without API keys. It deliberately implements the two
prompt *versions* differently (v1 is looser) so prompt-version comparison is
meaningful offline. Its output goes through exactly the same schema validation,
permission checks and anti-hallucination gate as a real model.
"""

from __future__ import annotations

import re
from typing import Any

from app.agents.llm import PlanRequest
from app.agents.prompts import PromptSpec
from app.schemas import PAN_PATTERN, AgentDecision, ToolCall

NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")
PAN_RE = re.compile(PAN_PATTERN)
PIN_RE = re.compile(r"^[1-9][0-9]{5}$")

BYPASS_WORDS = ("bypass", "skip kyc", "without kyc", "fake", "just approve", "no kyc", "avoid kyc")
HUMAN_WORDS = ("human", "real person", "support agent", "customer care", "talk to someone", "escalate")
POLICY_WORDS = ("policy", "why", "explain", "requirement", "rule", "how many", "allowed", "limit")
POLICY_QUESTION_WORDS = ("policy", "requirement", "rule", "how many", "what is the", "explain the")
FIX_WORDS = (
    "fix",
    "help me",
    "check my",
    "rejected",
    "error",
    "why can't",
    "why cant",
    "why wont",
    "why won't",
    "failing",
    "fails",
    "correct it",
)
OUT_OF_SCOPE = (
    "interest rate",
    "emi",
    "loan amount",
    "credit score",
    "tax advice",
    "invest",
    "stock",
    "cricket",
    "weather",
    "joke",
    "recipe",
)
NO_CONFIRM_WORDS = ("without confirm", "no confirm", "don't ask", "dont ask", "just submit", "submit it now", "auto submit")
NAV_WORDS = ("go to", "go back", "take me to", "open the", "show me the", "switch to", "navigate")
NAV_TARGETS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("document", "upload"), "kyc.document_upload"),
    (("mandate", "bank"), "kyc.bank_mandate"),
    (("address",), "kyc.address"),
    (("review", "summary"), "kyc.review"),
    (("pan",), "kyc.pan_entry"),
)


def _field(ctx: dict, name: str) -> dict:
    return next((f for f in ctx.get("fields", []) if f.get("name") == name), {})


def _cite(spec: PromptSpec, refs: list[str], *wanted: str) -> list[str]:
    if not spec.require_policy_citation:
        return []
    available = [ref for ref in wanted if ref in refs]
    return available or refs[:2]


def _policy_text(policies: list[dict], ref: str) -> str:
    for policy in policies:
        if policy.get("ref") == ref:
            return str(policy.get("content", ""))
    return ""


def _decision(**kwargs: Any) -> AgentDecision:
    return AgentDecision(**kwargs)


def _tc(tool: str, **args: Any) -> ToolCall:
    return ToolCall(tool=tool, args=args)


def plan_offline(request: PlanRequest) -> AgentDecision:
    ctx = request.screen_context or {}
    spec = request.spec
    msg = (request.message_masked or "").lower()
    raw_msg = (request.raw_message or "").lower()
    refs = [p.get("ref", "") for p in request.policies]
    screen = ctx.get("screen_id", "kyc.pan_entry")

    if any(word in msg or word in raw_msg for word in BYPASS_WORDS):
        return _decision(
            plan="User asked to bypass KYC. Refuse and escalate per policy.",
            say=(
                "I can't skip or bypass KYC verification — it's a regulatory requirement. "
                "I can help you fix the failing step, or connect you with a human reviewer."
            ),
            tool_calls=[_tc("escalate_to_human", reason="User requested to bypass KYC verification")],
            confidence=0.97,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.bypass", "kyc.escalation"),
            escalate=True,
            escalation_reason="User requested to bypass KYC verification",
        )

    if any(word in msg or word in raw_msg for word in HUMAN_WORDS):
        return _decision(
            plan="User requested a human. Create an escalation record.",
            say="I've raised this to our KYC support team. A reviewer will pick it up with the context from this session.",
            tool_calls=[_tc("escalate_to_human", reason="User explicitly requested human support")],
            confidence=0.99,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.escalation"),
            escalate=True,
            escalation_reason="User requested human support",
        )

    if any(word in msg or word in raw_msg for word in OUT_OF_SCOPE):
        return _decision(
            plan="Out-of-scope request. Refuse politely and stay inside the KYC journey.",
            say=(
                "That's outside what this KYC assistant can help with. I can help with PAN verification, "
                "document upload, the bank mandate, or your address details."
            ),
            tool_calls=[],
            confidence=0.9,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.bypass"),
        )

    if any(word in msg or word in raw_msg for word in NAV_WORDS):
        for keywords, target in NAV_TARGETS:
            if target == screen:
                continue
            if any(keyword in msg or keyword in raw_msg for keyword in keywords):
                return _decision(
                    plan=f"User asked to open {target}. Navigate the client without submitting anything.",
                    say=f"Opening {target.replace('kyc.', '').replace('_', ' ')} for you.",
                    tool_calls=[_tc("navigate", screen_id=target)],
                    confidence=0.92,
                    requires_confirmation=False,
                    cited_policy_refs=[],
                )

    if screen == "kyc.pan_entry":
        return _pan_plan(request, ctx, spec, refs, msg, raw_msg)
    if screen == "kyc.document_upload":
        return _document_plan(request, ctx, spec, refs, msg, raw_msg)
    if screen == "kyc.bank_mandate":
        return _mandate_plan(request, ctx, spec, refs, msg, raw_msg)
    if screen == "kyc.address":
        return _address_plan(request, ctx, spec, refs, msg, raw_msg)

    return _decision(
        plan="No screen-specific rule matched; ask for a specific problem.",
        say="Tell me which step is failing and I'll take a look at the live screen state.",
        tool_calls=[],
        confidence=0.5,
        requires_confirmation=False,
        cited_policy_refs=[],
    )


# ---------------------------------------------------------------------------
# PAN
# ---------------------------------------------------------------------------


def _pan_plan(
    request: PlanRequest, ctx: dict, spec: PromptSpec, refs: list[str], msg: str, raw_msg: str
) -> AgentDecision:
    pan_field = _field(ctx, "pan")
    pan_value = str(pan_field.get("value") or "")
    status = ctx.get("status") or {}
    verified = status.get("pan") == "verified" or bool(pan_field.get("disabled"))

    if verified:
        wants_change = any(word in msg for word in ("change", "update", "different", "replace", "edit"))
        if wants_change:
            return _decision(
                plan="PAN is already verified (locked). A change requires a human reviewer.",
                say=(
                    "Your PAN is already verified for this application, so I can't replace it. "
                    "I've escalated a request to a human reviewer who can unlock it."
                ),
                tool_calls=[_tc("escalate_to_human", reason="Verified PAN cannot be changed by the agent")],
                confidence=0.95,
                requires_confirmation=False,
                cited_policy_refs=_cite(spec, refs, "kyc.pan.verification", "kyc.escalation"),
                escalate=True,
                escalation_reason="Change requested for a verified PAN",
            )
        return _decision(
            plan="PAN already verified; reassure the user.",
            say="Your PAN is already verified for this application. You can continue to document upload.",
            tool_calls=[],
            confidence=0.9,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.pan.verification"),
        )

    compact = NON_ALNUM_RE.sub("", pan_value)

    asks_policy = any(word in msg for word in POLICY_QUESTION_WORDS)
    asks_fix = any(word in msg for word in FIX_WORDS)
    if asks_policy and not asks_fix:
        policy = _policy_text(request.policies, "kyc.pan.format")
        return _decision(
            plan="Policy question about PAN formatting; answer from retrieved policy.",
            say=policy[:400] or "A PAN must be 10 characters: five letters, four digits, one letter.",
            tool_calls=[],
            confidence=0.86,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.pan.format"),
        )

    if not compact:
        missing = any(word in msg for word in ("missing", "incomplete", "starts", "ends", "think", "maybe", "guess"))
        say = (
            "I can see the PAN field is empty. I can't guess or generate a PAN — please type your 10-character PAN "
            "and I'll check the format before you submit."
            if not missing
            else "I can't reconstruct a PAN from partial clues, and I won't guess the missing characters. "
            "Please enter the full 10-character PAN from your card and I'll validate it."
        )
        return _decision(
            plan="PAN is empty; missing information must come from the user.",
            say=say,
            tool_calls=[_tc("highlight", field="pan")] if spec.highlight_before_fill else [],
            confidence=0.88,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.pan.format"),
        )

    if len(compact) != 10:
        return _decision(
            plan=f"PAN has {len(compact)} characters, which cannot be repaired automatically.",
            say=(
                f"Your PAN has {len(compact)} characters — a PAN must be exactly 10. "
                "I can't add or guess the missing characters, so please re-enter it from your card."
            ),
            tool_calls=[_tc("highlight", field="pan")] if spec.highlight_before_fill else [],
            confidence=0.9,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.pan.format"),
        )

    uppercase = compact.upper()
    if PAN_RE.match(uppercase) and compact != uppercase:
        tool_calls: list[ToolCall] = []
        if spec.highlight_before_fill:
            tool_calls.append(_tc("highlight", field="pan"))
        tool_calls.append(_tc("set_field", field="pan", value=uppercase))
        confirm = spec.require_confirmation_for_financial or True  # submission always needs consent
        return _decision(
            plan="Only the letter case is wrong. Uppercase the existing characters, then ask before verifying.",
            say=(
                "Your PAN was entered in lowercase, which the verifier rejects. "
                f"I've corrected the case to {uppercase}. Please check it, then confirm to verify."
            ),
            tool_calls=tool_calls,
            confidence=0.93,
            requires_confirmation=confirm,
            confirmation_prompt="Review the corrected PAN and confirm to verify it.",
            cited_policy_refs=_cite(spec, refs, "kyc.pan.format", "kyc.pan.verification", "kyc.consent"),
        )

    if PAN_RE.match(compact):
        return _decision(
            plan="PAN format is valid; ask the user to confirm verification.",
            say="Your PAN format looks correct. Please confirm to verify it against the records.",
            tool_calls=[_tc("highlight", field="pan")] if spec.highlight_before_fill else [],
            confidence=0.9,
            requires_confirmation=True,
            confirmation_prompt="Confirm to verify this PAN.",
            cited_policy_refs=_cite(spec, refs, "kyc.pan.format", "kyc.consent"),
        )

    if any(word in msg for word in POLICY_WORDS):
        policy = _policy_text(request.policies, "kyc.pan.format")
        return _decision(
            plan="Policy question about PAN formatting.",
            say=policy[:400] or "A PAN must be 10 characters: five letters, four digits, one letter.",
            tool_calls=[],
            confidence=0.85,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.pan.format"),
        )

    return _decision(
        plan="PAN characters are in the wrong positions; it cannot be normalised safely.",
        say=(
            "The PAN format doesn't match the required pattern (5 letters, 4 digits, 1 letter). "
            "I can't work out what the correct characters should be, so please re-check the PAN on your card."
        ),
        tool_calls=[_tc("highlight", field="pan")] if spec.highlight_before_fill else [],
        confidence=0.84,
        requires_confirmation=False,
        cited_policy_refs=_cite(spec, refs, "kyc.pan.format"),
    )


# ---------------------------------------------------------------------------
# Documents
# ---------------------------------------------------------------------------


def _document_plan(
    request: PlanRequest, ctx: dict, spec: PromptSpec, refs: list[str], msg: str, raw_msg: str
) -> AgentDecision:
    doc = ctx.get("document") or {}
    document_id = doc.get("document_id")
    confidence = float(doc.get("ocr_confidence") or 0.0)
    blur = float(doc.get("blur_score") or 0.0)
    rotation = abs(float(doc.get("rotation_degrees") or 0.0))
    doc_status = doc.get("status") or "missing"
    issues = doc.get("issues") or []
    attempts = int((ctx.get("status") or {}).get("document_attempts") or 0)

    if not document_id:
        return _decision(
            plan="No document present. Ask for an upload; do not fabricate one.",
            say=(
                "There's no identity document uploaded yet. Add a clear JPG, PNG or PDF of your PAN or Aadhaar, "
                "and I'll check the quality before you continue."
            ),
            tool_calls=[_tc("highlight", field="upload_area")],
            confidence=0.9,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.document.quality"),
        )

    failing = confidence < 0.85 or blur < 0.60 or rotation > 5.0
    issue_text = " ".join(str(i) for i in issues).lower()

    if "unsupported file type" in issue_text or "larger than" in issue_text:
        return _decision(
            plan="File type/size is rejected by deterministic validation; ask for a supported file.",
            say=(
                "That file can't be accepted. Please upload your document as a JPG, PNG or PDF "
                "(up to 8 MB) — the upload area is highlighted."
            ),
            tool_calls=[_tc("highlight", field="upload_area")],
            confidence=0.92,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.document.quality"),
        )

    if spec.ocr_confidence_gate and (
        "duplicate" in issue_text or "already uploaded" in issue_text or "already exists" in issue_text
    ):
        return _decision(
            plan="Duplicate document detected; do not create a second verification record.",
            say=(
                "This looks like a duplicate of a document already uploaded in this session. "
                "I haven't created a second record — you can continue with the existing upload."
            ),
            tool_calls=[],
            confidence=0.88,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.document.retry"),
        )

    if spec.ocr_confidence_gate and failing:
        if attempts >= 3:
            return _decision(
                plan="Document quality has failed repeatedly and retries are exhausted.",
                say=(
                    "This upload still doesn't meet the quality bar and you've used the retry limit. "
                    "I've escalated the document review to a human agent."
                ),
                tool_calls=[_tc("escalate_to_human", reason="Document retry limit exhausted after quality failures")],
                confidence=0.93,
                requires_confirmation=False,
                cited_policy_refs=_cite(spec, refs, "kyc.document.quality", "kyc.document.retry", "kyc.escalation"),
                escalate=True,
                escalation_reason="Document retry limit exhausted",
            )
        reasons: list[str] = []
        if blur < 0.60:
            reasons.append(f"the image blur score is {blur:.2f} (minimum 0.60)")
        if confidence < 0.85:
            reasons.append(f"the OCR confidence is {confidence:.2f} (minimum 0.85)")
        if rotation > 5.0:
            reasons.append(f"the document is rotated {rotation:.1f}° (maximum 5°)")
        reason = " and ".join(reasons) or "it does not meet the quality threshold"
        return _decision(
            plan="Document fails the quality gate; recommend a re-upload and never accept the extraction.",
            say=(
                f"I can't accept this document because {reason}. I won't use the extracted values. "
                "Please re-upload a sharper, straight-on photo — the upload area is highlighted."
            ),
            tool_calls=[_tc("highlight", field="upload_area")],
            confidence=0.92,
            requires_confirmation=True,
            confirmation_prompt="Upload a clearer document to continue.",
            cited_policy_refs=_cite(spec, refs, "kyc.document.quality", "kyc.document.retry"),
        )

    if doc_status == "rejected" and spec.ocr_confidence_gate:
        if attempts >= 3:
            return _decision(
                plan="Rejected document with exhausted retries.",
                say="This document was rejected and the retry limit is reached. I've sent it to a human reviewer.",
                tool_calls=[_tc("escalate_to_human", reason="Rejected document with exhausted retries")],
                confidence=0.9,
                requires_confirmation=False,
                cited_policy_refs=_cite(spec, refs, "kyc.document.retry", "kyc.escalation"),
                escalate=True,
                escalation_reason="Document retry limit exhausted",
            )
        return _decision(
            plan="Document rejected. Recommend a new upload; do not silently re-use it.",
            say=(
                "This document was rejected. Please upload a new, clearer copy — "
                "I won't resubmit the same file automatically."
            ),
            tool_calls=[_tc("highlight", field="upload_area")],
            confidence=0.9,
            requires_confirmation=True,
            confirmation_prompt="Choose a clearer document to upload.",
            cited_policy_refs=_cite(spec, refs, "kyc.document.quality", "kyc.document.retry"),
        )

    if spec.ocr_confidence_gate and (
        "conflict" in issue_text or doc.get("conflicting_values")
    ):
        return _decision(
            plan="Conflicting extracted values must not be auto-resolved.",
            say=(
                "The document contains conflicting values, so I can't safely accept it. "
                "I've escalated it for manual review rather than guessing which value is right."
            ),
            tool_calls=[_tc("escalate_to_human", reason="Conflicting document extraction values")],
            confidence=0.94,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.document.quality", "kyc.escalation"),
            escalate=True,
            escalation_reason="Conflicting extracted values",
        )

    if doc_status == "accepted" or not spec.ocr_confidence_gate:
        if spec.ocr_confidence_gate:
            return _decision(
                plan="Document passes all quality gates.",
                say=(
                    f"This document passes our quality checks (OCR confidence {confidence:.2f}). "
                    "You can continue to the bank mandate step."
                ),
                tool_calls=[],
                confidence=0.9,
                requires_confirmation=False,
                cited_policy_refs=_cite(spec, refs, "kyc.document.quality"),
            )
        return _decision(
            plan="v1: no quality gate, retry the existing document reference.",
            say="I've resubmitted the document for you.",
            tool_calls=[_tc("upload_doc", document_id=document_id)],
            confidence=0.7,
            requires_confirmation=False,
            cited_policy_refs=[],
        )

    if "unreadable" in " ".join(issues).lower():
        return _decision(
            plan="Document is unreadable; escalate instead of retrying silently.",
            say="This file is unreadable. I've escalated it so a human can review the upload.",
            tool_calls=[_tc("escalate_to_human", reason="Unreadable identity document")],
            confidence=0.9,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.document.quality", "kyc.escalation"),
            escalate=True,
            escalation_reason="Unreadable document",
        )

    return _decision(
        plan="No safe automatic action; ask the user to re-upload.",
        say="I can't verify this document automatically. Please upload a new copy.",
        tool_calls=[_tc("highlight", field="upload_area")],
        confidence=0.7,
        requires_confirmation=False,
        cited_policy_refs=_cite(spec, refs, "kyc.document.quality"),
    )


# ---------------------------------------------------------------------------
# Mandate
# ---------------------------------------------------------------------------


def _mandate_plan(
    request: PlanRequest, ctx: dict, spec: PromptSpec, refs: list[str], msg: str, raw_msg: str
) -> AgentDecision:
    mandate = ctx.get("mandate") or {}
    status = mandate.get("status") or "not_started"
    attempts = int(mandate.get("attempts") or 0)
    max_attempts = int(mandate.get("max_attempts") or 3)
    key = mandate.get("idempotency_key")

    if status == "verified":
        asks_again = any(word in msg for word in ("resubmit", "again", "retry", "submit again", "new mandate"))
        return _decision(
            plan="Mandate already verified; a second submission must not be created.",
            say=(
                "Your bank mandate is already verified. A repeat submission would return the same mandate, "
                "so there's nothing to retry."
                if asks_again
                else "Your bank mandate is already verified. You can continue to address verification."
            ),
            tool_calls=[],
            confidence=0.92,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.mandate.idempotency"),
        )

    if status == "unknown":
        if spec.policy_aware_retry:
            return _decision(
                plan="Unknown transaction status must not be retried; escalate to a human.",
                say=(
                    "The bank didn't confirm this mandate's status, so a debit may already exist. "
                    "I won't retry it automatically — I've escalated it for a human to check first."
                ),
                tool_calls=[_tc("escalate_to_human", reason="Mandate status unknown after gateway timeout")],
                confidence=0.95,
                requires_confirmation=False,
                cited_policy_refs=_cite(spec, refs, "kyc.mandate.retry", "kyc.escalation"),
                escalate=True,
                escalation_reason="Unknown mandate status",
            )
        return _decision(
            plan="v1: retry the mandate even though the status is unknown.",
            say="Let me try submitting the mandate again.",
            tool_calls=[_tc("retry_step", step_id="kyc.bank_mandate", idempotency_key=key or "unknown")],
            confidence=0.6,
            requires_confirmation=False,
            cited_policy_refs=[],
        )

    if status == "failed":
        if attempts >= max_attempts:
            return _decision(
                plan="Retry limit reached; escalate.",
                say=(
                    f"The mandate has failed {attempts} times, which is the limit. "
                    "I've escalated it to a human reviewer instead of retrying again."
                ),
                tool_calls=[_tc("escalate_to_human", reason="Mandate retry limit reached")],
                confidence=0.94,
                requires_confirmation=False,
                cited_policy_refs=_cite(spec, refs, "kyc.mandate.retry", "kyc.escalation"),
                escalate=True,
                escalation_reason="Mandate retry limit reached",
            )
        if not key:
            return _decision(
                plan="Failed mandate but no idempotency key available; ask the user to restart the mandate.",
                say="I can't safely retry this mandate without its idempotency key. Please restart the mandate step.",
                tool_calls=[],
                confidence=0.7,
                requires_confirmation=False,
                cited_policy_refs=_cite(spec, refs, "kyc.mandate.retry"),
            )
        return _decision(
            plan="Mandate failed with a hard error; retry safely with the same idempotency key after confirmation.",
            say=(
                "The bank gateway timed out, which is a hard failure. This is safe to retry with the same "
                "idempotency key — it can't create a duplicate mandate. Please confirm to retry."
            ),
            tool_calls=[_tc("retry_step", step_id="kyc.bank_mandate", idempotency_key=key)],
            confidence=0.9,
            requires_confirmation=spec.require_confirmation_for_financial,
            confirmation_prompt="Confirm the mandate retry with the same idempotency key.",
            cited_policy_refs=_cite(spec, refs, "kyc.mandate.retry", "kyc.mandate.idempotency", "kyc.consent"),
        )

    if status == "pending":
        return _decision(
            plan="Mandate is pending; do not start a parallel submission.",
            say="The mandate is still pending at the bank. I'll wait for the status to settle rather than submitting another one.",
            tool_calls=[],
            confidence=0.85,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.mandate.idempotency"),
        )

    if any(word in msg for word in POLICY_WORDS):
        policy = _policy_text(request.policies, "kyc.mandate.retry")
        return _decision(
            plan="Policy question about mandate retries.",
            say=policy[:420] or "Mandate retries are limited and always reuse the same idempotency key.",
            tool_calls=[],
            confidence=0.85,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.mandate.retry", "kyc.mandate.idempotency"),
        )

    return _decision(
        plan="No mandate exists yet. The agent cannot submit banking details on the user's behalf.",
        say=(
            "No bank mandate has been started. Fill in your bank, IFSC and account number, then confirm the "
            "mandate yourself — I can't submit financial details for you."
        ),
        tool_calls=[_tc("highlight", field="mandate_panel")],
        confidence=0.88,
        requires_confirmation=False,
        cited_policy_refs=_cite(spec, refs, "kyc.consent", "kyc.mandate.idempotency"),
    )


# ---------------------------------------------------------------------------
# Address
# ---------------------------------------------------------------------------


def _address_plan(
    request: PlanRequest, ctx: dict, spec: PromptSpec, refs: list[str], msg: str, raw_msg: str
) -> AgentDecision:
    pin_field = _field(ctx, "pin")
    city_field = _field(ctx, "city")
    state_field = _field(ctx, "state")

    pin_value = str(pin_field.get("value") or "")
    compact_pin = re.sub(r"[\s\-]", "", pin_value)
    city_value = str(city_field.get("value") or "")
    city_options = city_field.get("options") or []
    city_error = city_field.get("error")
    state_options = state_field.get("options") or []

    tool_calls: list[ToolCall] = []
    fixes: list[str] = []

    if pin_value and compact_pin != pin_value and PIN_RE.match(compact_pin):
        tool_calls.append(_tc("highlight", field="pin"))
        tool_calls.append(_tc("set_field", field="pin", value=compact_pin))
        fixes.append("removed the spaces from your PIN code")
    elif pin_value and not PIN_RE.match(compact_pin):
        if compact_pin.startswith("0") or len(compact_pin) != 6:
            return _decision(
                plan="PIN is invalid and cannot be repaired without inventing digits.",
                say=(
                    "Your PIN code isn't valid — it must be 6 digits and can't start with zero. "
                    "I can't guess the correct PIN, so please check it on your address proof."
                ),
                tool_calls=[_tc("highlight", field="pin")] if spec.highlight_before_fill else [],
                confidence=0.88,
                requires_confirmation=False,
                cited_policy_refs=_cite(spec, refs, "kyc.address.validation"),
            )

    state_value = str(state_field.get("value") or "")
    if state_value and state_options and state_value not in state_options:
        canonical = next((opt for opt in state_options if opt.lower() == state_value.strip().lower()), None)
        if canonical:
            tool_calls.extend([_tc("highlight", field="state"), _tc("set_field", field="state", value=canonical)])
            fixes.append(f"matched your state to {canonical}")

    if city_error and city_options and city_value.lower() not in {opt.lower() for opt in city_options}:
        suggestion = city_options[0]
        tool_calls.extend([_tc("highlight", field="city"), _tc("set_field", field="city", value=suggestion)])
        fixes.append(f"suggested {suggestion} for the city (from the verified suggestions)")

    if fixes:
        return _decision(
            plan="Apply safe formatting/suggestion fixes only. Never touch house number or street text.",
            say=(
                "I've made these safe corrections: " + "; ".join(fixes) + ". "
                "I haven't changed your house number, street or anything that has to come from you. "
                "Please review and confirm to verify the address."
            ),
            tool_calls=tool_calls,
            confidence=0.88,
            requires_confirmation=True,
            confirmation_prompt="Confirm the address corrections before verification.",
            cited_policy_refs=_cite(spec, refs, "kyc.address.validation", "kyc.consent"),
        )

    if any(word in msg for word in POLICY_WORDS):
        policy = _policy_text(request.policies, "kyc.address.validation")
        return _decision(
            plan="Policy question about address validation.",
            say=policy[:400] or "PIN codes are 6 digits and cannot start with zero.",
            tool_calls=[],
            confidence=0.85,
            requires_confirmation=False,
            cited_policy_refs=_cite(spec, refs, "kyc.address.validation"),
        )

    return _decision(
        plan="Address needs information the agent cannot infer.",
        say=(
            "I can't invent any part of your address. Please check the highlighted fields, "
            "and pick a city/state from the suggestions if they don't match."
        ),
        tool_calls=[_tc("highlight", field="street")] if spec.highlight_before_fill else [],
        confidence=0.8,
        requires_confirmation=True,
        confirmation_prompt="Review your address details before verifying.",
        cited_policy_refs=_cite(spec, refs, "kyc.address.validation"),
    )
