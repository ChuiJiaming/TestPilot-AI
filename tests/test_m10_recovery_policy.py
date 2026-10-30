from uuid import uuid4

import pytest
from pydantic import ValidationError

from apps.api.app.runs.recovery import RecoveryProposal, evaluate_recovery_candidate
from apps.api.app.runs.planner import RunAgentProvider


def _proposal(**overrides):
    payload = {
        "action_id": uuid4(),
        "locator": {"by": "role", "value": "button", "name": "Continue"},
        "source": "a11y_tree",
        "confidence": 0.82,
        "semantic_descriptor": {"role": "button", "name": "Continue"},
    }
    payload.update(overrides)
    return RecoveryProposal.model_validate(payload)


def _verification(**overrides):
    result = {
        "match_count": 1,
        "visible": True,
        "descriptor": {"role": "button", "name": "Continue"},
    }
    result.update(overrides)
    return result


def test_accepts_only_unique_visible_candidate_with_matching_observed_semantics():
    decision = evaluate_recovery_candidate(
        proposal=_proposal(), verification=_verification()
    )

    assert decision.accepted is True
    assert decision.reason_code == "ACCEPTED"
    assert decision.observed_descriptor == {"role": "button", "name": "Continue"}


def test_low_confidence_does_not_override_or_block_deterministic_evidence():
    decision = evaluate_recovery_candidate(
        proposal=_proposal(confidence=0.01), verification=_verification()
    )

    assert decision.accepted is True


@pytest.mark.parametrize(
    "locator",
    [
        {"strategy": "role", "value": "button"},
        {"by": "role", "value": "button", "unexpected": "field"},
        {"by": "role", "value": ""},
    ],
)
def test_refuses_locator_outside_frozen_dsl_v2(locator):
    decision = evaluate_recovery_candidate(
        proposal=_proposal(locator=locator), verification=_verification()
    )

    assert decision.accepted is False
    assert decision.reason_code == "INVALID_LOCATOR"


@pytest.mark.parametrize("match_count", [0, 2])
def test_refuses_zero_or_ambiguous_match_count(match_count):
    decision = evaluate_recovery_candidate(
        proposal=_proposal(), verification=_verification(match_count=match_count)
    )

    assert decision.accepted is False
    assert decision.reason_code == "MATCH_COUNT_NOT_ONE"


def test_refuses_hidden_target():
    decision = evaluate_recovery_candidate(
        proposal=_proposal(), verification=_verification(visible=False)
    )

    assert decision.accepted is False
    assert decision.reason_code == "TARGET_NOT_VISIBLE"


@pytest.mark.parametrize(
    "descriptor",
    [{}, None, {"role": "button", "name": "Other"}, {"raw_html": "<form>"}],
)
def test_refuses_missing_or_unmatched_semantic_descriptor(descriptor):
    decision = evaluate_recovery_candidate(
        proposal=_proposal(semantic_descriptor={"role": "button", "name": "Continue"}),
        verification=_verification(descriptor=descriptor),
    )

    assert decision.accepted is False
    assert decision.reason_code == "SEMANTIC_DESCRIPTOR_INVALID"


@pytest.mark.parametrize("confidence", [-0.01, 1.01, float("nan"), float("inf")])
def test_proposal_rejects_confidence_outside_unit_interval(confidence):
    with pytest.raises(ValidationError):
        _proposal(confidence=confidence)


def test_provider_rejects_output_that_attempts_to_edit_published_case_fields():
    class Model:
        def invoke(self, _messages):
            return {
                "action_id": uuid4(),
                "locator": {"by": "role", "value": "button", "name": "Continue"},
                "source": "a11y_tree",
                "confidence": 0.8,
                "semantic_descriptor": {"role": "button", "name": "Continue"},
                "action": "fill",
                "value": "attacker-controlled",
                "assertions": [],
            }

    provider = RunAgentProvider.__new__(RunAgentProvider)
    provider.recovery_model = Model()

    with pytest.raises(ValidationError):
        provider.recover(
            {"budget_left": 1},
            {"schema_version": 2},
            {"action_id": str(uuid4()), "action": "click"},
            {"text": "bounded observation"},
        )


def test_provider_rejects_proposal_for_a_different_action_id():
    failed_action_id = uuid4()

    class Model:
        def invoke(self, _messages):
            return _proposal(action_id=uuid4())

    provider = RunAgentProvider.__new__(RunAgentProvider)
    provider.recovery_model = Model()

    with pytest.raises(ValueError, match="failed published action"):
        provider.recover(
            {"budget_left": 1},
            {"schema_version": 2},
            {"action_id": str(failed_action_id), "action": "click"},
            {"text": "bounded observation"},
        )
