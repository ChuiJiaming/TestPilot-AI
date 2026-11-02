"""Bounded, auditable failure classification; model output is always a hypothesis."""

from hashlib import sha256
import json
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError


FAILURE_CLASSES = frozenset({
    "LOCATOR", "ASSERTION", "ENVIRONMENT", "INFRA", "SECURITY", "DATA", "FLAKY",
})

_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\b")
_URL = re.compile(r"\b(?:https?|wss?)://[^\s\]\[()<>\"']+", re.I)
_UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}\b", re.I)
_SECRET = re.compile(
    r"(?i)\b(?:bearer\s+|password|passwd|token|secret|api[_-]?key|authorization)\s*[:=]\s*[^\s,;]+"
)
_QUOTED = re.compile(r"(['\"])[^'\"\r\n]{1,512}\1")
_NUMBER = re.compile(r"\b\d+\b")
_SPACE = re.compile(r"\s+")


class FailureAnalysisError(ValueError):
    """Model output did not match the safe, evidence-bound analysis contract."""


class FailureAnalysisProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: str
    title: str = Field(min_length=1, max_length=160)
    hypothesis: str = Field(min_length=1, max_length=800)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[str] = Field(max_length=10)


class FailureAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")

    classification: str
    title: str
    hypothesis: str
    confidence: float
    evidence_ids: list[str]
    evidence_status: str


def sanitize_summary(value: str, *, maximum: int = 400) -> str:
    """Redact common identifiers and credentials before storing model/report text."""
    if not isinstance(value, str):
        return ""
    text = value[:4096]
    text = _SECRET.sub("[redacted-secret]", text)
    text = _JWT.sub("[redacted-token]", text)
    text = _EMAIL.sub("[redacted-email]", text)
    text = _URL.sub("[redacted-url]", text)
    text = _UUID.sub("[redacted-id]", text)
    text = re.sub(r"\b(?:\d[ -]?){13,19}\b", "[redacted-number]", text)
    return _SPACE.sub(" ", text).strip()[:maximum]


def validate_failure_analysis(proposal, *, allowed_evidence_ids) -> FailureAnalysis:
    """Validate model claims against evidence IDs produced by the deterministic runner."""
    try:
        parsed = FailureAnalysisProposal.model_validate(proposal)
    except (ValidationError, TypeError, ValueError) as exc:
        raise FailureAnalysisError("analysis proposal is invalid") from exc
    if parsed.classification not in FAILURE_CLASSES:
        raise FailureAnalysisError("analysis classification is unsupported")
    try:
        allowed = {str(UUID(str(value))) for value in allowed_evidence_ids}
        evidence_ids = [str(UUID(value)) for value in parsed.evidence_ids]
    except (ValueError, TypeError, AttributeError) as exc:
        raise FailureAnalysisError("analysis evidence ID is invalid") from exc
    if any(value not in allowed for value in evidence_ids):
        raise FailureAnalysisError("analysis contains unsupported evidence")
    evidence_ids = list(dict.fromkeys(evidence_ids))
    title = sanitize_summary(parsed.title, maximum=120)
    hypothesis = sanitize_summary(parsed.hypothesis, maximum=400)
    if not title or not hypothesis:
        raise FailureAnalysisError("analysis summary is empty after redaction")
    evidence_status = "AVAILABLE" if evidence_ids else "INSUFFICIENT_EVIDENCE"
    confidence = parsed.confidence if evidence_ids else min(parsed.confidence, 0.25)
    return FailureAnalysis(
        classification=parsed.classification,
        title=title,
        hypothesis=hypothesis,
        confidence=confidence,
        evidence_ids=evidence_ids,
        evidence_status=evidence_status,
    )


def fingerprint_failure(failure: dict) -> str:
    """Hash a normalized failure signature; never return the signature itself."""
    if not isinstance(failure, dict):
        raise ValueError("failure must be an object")
    components = []
    for key in ("action_type", "failure_phase", "error_code", "message"):
        value = failure.get(key, "")
        if not isinstance(value, str):
            value = ""
        normalized = sanitize_summary(value, maximum=512).casefold()
        normalized = _NUMBER.sub("#", normalized)
        normalized = _SPACE.sub(" ", normalized).strip()
        components.append((key, normalized))
    canonical = json.dumps(components, separators=(",", ":"), ensure_ascii=True)
    return sha256(canonical.encode("utf-8")).hexdigest()


def insufficient_analysis(failure: dict, evidence_ids=()) -> FailureAnalysis:
    """Build a safe fallback when the Analyzer is unavailable or rejects its output."""
    error_code = failure.get("error_code") if isinstance(failure, dict) else None
    phase = failure.get("failure_phase") if isinstance(failure, dict) else None
    classification = "ASSERTION"
    if error_code == "LOCATOR_NOT_UNIQUE":
        classification = "LOCATOR"
    elif error_code in {"BROWSER_WORKER_UNAVAILABLE", "BROWSER_TIMEOUT", "BROWSER_ACTION_FAILED"}:
        classification = "INFRA"
    elif phase in {"environment", "navigation"}:
        classification = "ENVIRONMENT"
    safe_evidence = []
    for value in evidence_ids:
        try:
            safe_evidence.append(str(UUID(str(value))))
        except (ValueError, TypeError, AttributeError):
            continue
    safe_evidence = list(dict.fromkeys(safe_evidence))[:10]
    return FailureAnalysis(
        classification=classification,
        title="Run failed; review the available evidence",
        hypothesis="The available execution metadata is not sufficient to confirm a root cause.",
        confidence=0.0,
        evidence_ids=safe_evidence,
        evidence_status="AVAILABLE" if safe_evidence else "INSUFFICIENT_EVIDENCE",
    )
