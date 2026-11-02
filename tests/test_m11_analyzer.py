from uuid import uuid4

import pytest

from apps.api.app.quality.analyzer import (
    FAILURE_CLASSES,
    FailureAnalysisError,
    fingerprint_failure,
    sanitize_summary,
    validate_failure_analysis,
)


@pytest.mark.parametrize("classification", sorted(FAILURE_CLASSES))
def test_failure_analysis_accepts_all_v103_classes(classification):
    evidence_id = str(uuid4())
    result = validate_failure_analysis({
        "classification": classification,
        "title": "Action failed",
        "hypothesis": "The action could not complete.",
        "confidence": 0.65,
        "evidence_ids": [evidence_id],
    }, allowed_evidence_ids=[evidence_id])

    assert result.classification == classification
    assert result.evidence_status == "AVAILABLE"
    assert result.evidence_ids == [evidence_id]
    assert not hasattr(result, "root_cause_status")


def test_unavailable_or_fabricated_evidence_is_rejected():
    with pytest.raises(FailureAnalysisError, match="unsupported evidence"):
        validate_failure_analysis({
            "classification": "ASSERTION",
            "title": "Assertion failed",
            "hypothesis": "The expected state was not observed.",
            "confidence": 0.7,
            "evidence_ids": [str(uuid4())],
        }, allowed_evidence_ids=[])


def test_no_evidence_is_explicitly_insufficient_even_if_model_claims_confidence():
    result = validate_failure_analysis({
        "classification": "INFRA",
        "title": "Execution failed",
        "hypothesis": "The browser may have disconnected.",
        "confidence": 0.99,
        "evidence_ids": [],
    }, allowed_evidence_ids=[])

    assert result.evidence_status == "INSUFFICIENT_EVIDENCE"
    assert result.confidence <= 0.25


def test_analysis_does_not_accept_ai_root_cause_confirmation():
    with pytest.raises(FailureAnalysisError):
        validate_failure_analysis({
            "classification": "ASSERTION",
            "title": "Assertion failed",
            "hypothesis": "The expected state was not observed.",
            "confidence": 0.5,
            "evidence_ids": [],
            "root_cause_status": "CONFIRMED",
        }, allowed_evidence_ids=[])


def test_fingerprint_normalizes_volatile_values_without_returning_raw_text():
    first = fingerprint_failure({
        "action_type": "assert:text",
        "failure_phase": "assertion",
        "error_code": "ASSERTION_MISMATCH",
        "message": "Expected user-482 to see https://example.test/accounts/13",
    })
    second = fingerprint_failure({
        "action_type": "assert:text",
        "failure_phase": "assertion",
        "error_code": "ASSERTION_MISMATCH",
        "message": "Expected user-917 to see https://example.test/accounts/94",
    })

    assert first == second
    assert len(first) == 64
    assert "example.test" not in first


def test_model_summary_redacts_pii_credentials_and_urls():
    summary = sanitize_summary(
        "Failure for alice@example.test token=eyJhbGciOiJIUzI1NiJ9.abc.def "
        "at https://app.example.test/order/42"
    )

    assert "alice@example.test" not in summary
    assert "eyJhbGciOiJIUzI1NiJ9" not in summary
    assert "https://app.example.test" not in summary
