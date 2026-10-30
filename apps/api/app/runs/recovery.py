"""Typed locator-recovery proposals and deterministic evidence policy."""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from apps.api.app.cases.dsl import Locator


class RecoveryProposal(BaseModel):
    """A locator-only suggestion for one already-published action."""

    model_config = ConfigDict(extra="forbid")

    action_id: UUID
    locator: dict[str, Any]
    source: Literal["a11y_tree", "dom", "action_history"]
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    semantic_descriptor: dict[str, Any]


RecoveryReasonCode = Literal[
    "ACCEPTED",
    "INVALID_PROPOSAL",
    "INVALID_LOCATOR",
    "MATCH_COUNT_NOT_ONE",
    "TARGET_NOT_VISIBLE",
    "SEMANTIC_DESCRIPTOR_INVALID",
]


class RecoveryDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted: bool
    reason_code: RecoveryReasonCode
    proposal: RecoveryProposal | None = None
    observed_descriptor: dict[str, str] = Field(default_factory=dict)


_LOCATOR_ADAPTER = TypeAdapter(Locator)
_DESCRIPTOR_KEYS = frozenset({"role", "name", "label", "placeholder"})
_DESCRIPTOR_VALUE_LIMIT = 512


def _bounded_descriptor(value: Any) -> dict[str, str] | None:
    """Return the small, non-empty semantic fields the review UI may retain."""
    if not isinstance(value, dict) or not value or any(
        key not in _DESCRIPTOR_KEYS for key in value
    ):
        return None
    result: dict[str, str] = {}
    for key, item in value.items():
        if item is None:
            continue
        if not isinstance(item, str) or len(item) > _DESCRIPTOR_VALUE_LIMIT:
            return None
        item = item.strip()
        if item:
            result[key] = item
    return result or None


def _refuse(reason_code: RecoveryReasonCode,
            proposal: RecoveryProposal | None = None) -> RecoveryDecision:
    return RecoveryDecision(
        accepted=False, reason_code=reason_code, proposal=proposal,
    )


def evaluate_recovery_candidate(*, proposal: RecoveryProposal | dict,
                                verification: dict) -> RecoveryDecision:
    """Gate a proposed locator on strict DSL and current Worker evidence.

    Confidence only ranks candidates; it does not override any evidence check.
    """
    try:
        candidate = (proposal if isinstance(proposal, RecoveryProposal)
                     else RecoveryProposal.model_validate(proposal))
    except (ValidationError, TypeError, ValueError):
        return _refuse("INVALID_PROPOSAL")

    try:
        _LOCATOR_ADAPTER.validate_python(candidate.locator)
    except ValidationError:
        return _refuse("INVALID_LOCATOR", candidate)

    if not isinstance(verification, dict):
        return _refuse("MATCH_COUNT_NOT_ONE", candidate)
    match_count = verification.get("match_count")
    if type(match_count) is not int or match_count != 1:
        return _refuse("MATCH_COUNT_NOT_ONE", candidate)
    if verification.get("visible") is not True:
        return _refuse("TARGET_NOT_VISIBLE", candidate)

    observed = _bounded_descriptor(verification.get("descriptor"))
    proposed_semantics = _bounded_descriptor(candidate.semantic_descriptor)
    if observed is None or proposed_semantics is None or any(
        observed.get(key) != value for key, value in proposed_semantics.items()
    ):
        return _refuse("SEMANTIC_DESCRIPTOR_INVALID", candidate)

    return RecoveryDecision(
        accepted=True,
        reason_code="ACCEPTED",
        proposal=candidate,
        observed_descriptor=observed,
    )
