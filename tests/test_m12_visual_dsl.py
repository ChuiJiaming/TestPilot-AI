from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from apps.api.app.runs.executor import ExecutionContractError, normalize_case


def test_v2_visual_assertion_must_reference_a_published_action():
    case_id, version_id = uuid4(), uuid4()
    action_id = uuid5(NAMESPACE_URL, f"testpilot:v2:{version_id}:1")
    case = {
        "schema_version": 2,
        "case_id": str(case_id),
        "case_version_id": str(version_id),
        "steps": [{"action": "navigate", "url": "https://example.test"}],
        "assertions": [{"type": "visual", "action_id": str(action_id)}],
    }

    _steps, assertions = normalize_case(case, fixtures={})

    assert assertions[0]["assertion"] == {
        "type": "visual", "action_id": str(action_id), "baseline_id": None,
    }


def test_v2_visual_assertion_rejects_action_id_outside_published_case():
    case_id, version_id = uuid4(), uuid4()
    case = {
        "schema_version": 2,
        "case_id": str(case_id),
        "case_version_id": str(version_id),
        "steps": [{"action": "navigate", "url": "https://example.test"}],
        "assertions": [{"type": "visual", "action_id": str(uuid4())}],
    }

    with pytest.raises(ExecutionContractError, match="INVALID_DSL_VISUAL_ACTION"):
        normalize_case(case, fixtures={})


def test_v3_visual_assertion_rejects_invalid_or_unpublished_ids():
    case_id, version_id, action_id = uuid4(), uuid4(), uuid4()
    case = {
        "schema_version": 3,
        "case_id": str(case_id),
        "case_version_id": str(version_id),
        "steps": [{
            "action_id": str(action_id), "action": "navigate",
            "url": "https://example.test", "replay_policy": "SAFE",
        }],
        "assertions": [{
            "assert_id": str(uuid4()), "type": "visual",
            "action_id": str(uuid4()), "severity": "BLOCKER",
        }],
    }

    with pytest.raises(ExecutionContractError, match="INVALID_DSL_VISUAL_ACTION"):
        normalize_case(case, fixtures={})
