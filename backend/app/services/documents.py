"""Simulated identity-document ingestion.

NO real OCR is performed. A deterministic profile is derived from the filename
so demos and evaluations are reproducible. Every response is explicitly marked
as simulated, and low-confidence documents never produce identity values.
"""

from __future__ import annotations

import hashlib
import random

from app.core.config import settings
from app.schemas import DocumentQuality

ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/png": ".png",
    "application/pdf": ".pdf",
}
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".pdf"}
MAX_BYTES = 8 * 1024 * 1024

MIN_CONFIDENCE = settings.mock_ocr_min_confidence
MIN_BLUR = 0.60
MAX_ROTATION = 5.0


def _profile(filename: str) -> str:
    name = (filename or "").lower()
    if "unreadable" in name:
        return "unreadable"
    if "blur" in name:
        return "blurry"
    if "rotate" in name or "skew" in name:
        return "rotated"
    if "lowconf" in name or "low_confidence" in name:
        return "low_confidence"
    if "conflict" in name:
        return "conflicting"
    if "duplicate" in name:
        return "duplicate"
    digest = hashlib.sha256(name.encode()).digest()
    return "good" if digest[0] % 10 < 8 else "blurry"


def analyse_document(filename: str, content_type: str, size_bytes: int) -> tuple[DocumentQuality, dict, str]:
    """Return (quality, masked_ocr_payload, status)."""
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in (filename or "") else ""
    if content_type not in ALLOWED_TYPES or ext not in ALLOWED_EXTENSIONS:
        quality = DocumentQuality(
            quality_score=0.0,
            ocr_confidence=0.0,
            rotation_degrees=0.0,
            blur_score=0.0,
            issues=["Unsupported file type. Upload a JPG, PNG or PDF."],
            acceptable=False,
        )
        return quality, _ocr_payload("unsupported", 0.0, []), "rejected"

    if size_bytes > MAX_BYTES:
        quality = DocumentQuality(
            quality_score=0.0,
            ocr_confidence=0.0,
            rotation_degrees=0.0,
            blur_score=0.0,
            issues=["File is larger than 8 MB."],
            acceptable=False,
        )
        return quality, _ocr_payload("too_large", 0.0, []), "rejected"

    profile = _profile(filename)
    rng = random.Random(f"{settings.mock_seed}:{filename}:{size_bytes}:{profile}")

    base = {
        "good": (0.95, 0.86, 0.9, 0.95),
        "blurry": (0.41, 1.8, 0.33, 0.72),
        "rotated": (0.66, 14.0, 0.74, 0.84),
        "low_confidence": (0.58, 1.0, 0.71, 0.62),
        "conflicting": (0.83, 1.0, 0.82, 0.90),
        "duplicate": (0.93, 0.0, 0.92, 0.95),
        "unreadable": (0.10, 2.0, 0.08, 0.12),
    }[profile]
    quality_score, rotation, blur, confidence = base
    # Small deterministic jitter so numbers look real without being random per call.
    jitter = rng.uniform(-0.01, 0.01)
    confidence = round(min(0.99, max(0.0, confidence + jitter)), 3)
    blur = round(min(1.0, max(0.0, blur + jitter)), 3)
    quality_score = round(min(0.99, max(0.0, quality_score + jitter)), 3)

    issues: list[str] = []
    if confidence < MIN_CONFIDENCE:
        issues.append(f"OCR confidence {confidence:.2f} is below the {MIN_CONFIDENCE:.2f} minimum.")
    if blur < MIN_BLUR:
        issues.append(f"Image blur score {blur:.2f} is below the {MIN_BLUR:.2f} minimum.")
    if abs(rotation) > MAX_ROTATION:
        issues.append(f"Document is rotated {rotation:.1f}° (max {MAX_ROTATION:.0f}°).")
    if profile == "conflicting":
        issues.append("Extracted values conflict between front and back of the document.")
    if profile == "duplicate":
        issues.append("This document appears to match a document already uploaded in this session.")
    if profile == "unreadable":
        issues.append("Document is unreadable; no reliable fields could be extracted.")

    acceptable = (
        confidence >= MIN_CONFIDENCE
        and blur >= MIN_BLUR
        and abs(rotation) <= MAX_ROTATION
        and profile not in {"conflicting", "duplicate", "unreadable", "unsupported"}
    )
    quality = DocumentQuality(
        quality_score=quality_score,
        ocr_confidence=confidence,
        rotation_degrees=rotation,
        blur_score=blur,
        issues=issues,
        acceptable=acceptable,
    )
    status = "accepted" if acceptable else ("needs_review" if profile in {"conflicting", "low_confidence"} else "rejected")

    fields = ["pan", "name", "date_of_birth"] if profile not in {"unreadable", "unsupported"} else []
    return quality, _ocr_payload(profile, confidence, fields, issues), status


def _ocr_payload(profile: str, confidence: float, fields: list[str], issues: list[str] | None = None) -> dict:
    """Masked, simulated extraction result.

    Intentionally contains no raw identity numbers: the preview is masked and
    the agent is never allowed to use it to populate a field.
    """
    preview: dict[str, str] = {}
    if "pan" in fields:
        preview["pan"] = "•••••••••F"
    if "name" in fields:
        preview["name"] = "•••• ••••••"
    if "date_of_birth" in fields:
        preview["date_of_birth"] = "••/••/••••"
    if profile == "conflicting":
        preview["name"] = "conflict: 2 variants detected"
    return {
        "simulated": True,
        "profile": profile,
        "confidence": confidence,
        "detected_fields": fields,
        "extraction_preview": preview,
        "issues": issues or [],
        "notice": "Simulated OCR for demo data. No real OCR or identity lookup was performed.",
    }
