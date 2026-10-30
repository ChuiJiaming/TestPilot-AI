import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import yaml
from fastapi import FastAPI

from apps.api.app.runs.repository import (
    RunApprovalConflict,
    RunApprovalPermissionDenied,
    RunScopeDenied,
)
from apps.api.app.runs.routes import create_run_router
from apps.api.app.main import create_app


ROOT = Path(__file__).resolve().parents[1]


class _AuthSessions:
    def __init__(self, principals):
        self.principals = principals

    def authenticate(self, token):
        return self.principals.get(token)


class _HealingRepository:
    def __init__(self, *, requester_id, reviewer_id, outsider_id, healing_id, project_id):
        self.requester_id = requester_id
        self.reviewer_id = reviewer_id
        self.outsider_id = outsider_id
        self.healing_id = healing_id
        self.project_id = project_id
        self.organization_id = uuid4()
        self.status = "PENDING_APPROVAL"
        self.approval_status = "PENDING"
        self.get_calls = []
        self.decision_calls = []
        self.decision_error = None
        self.expired = False

    @contextmanager
    def session_factory(self):
        class _Session:
            def scalar(_self, _statement):
                return self.organization_id

        yield _Session()

    def project_id_for_healing(self, healing_id):
        return self.project_id if healing_id == self.healing_id else None

    def authorize_approval_project(self, actor_id, project_id):
        if project_id != self.project_id or actor_id == self.outsider_id:
            raise RunScopeDenied

    def get_healing(self, actor_id, healing_id):
        self.get_calls.append((actor_id, healing_id))
        if healing_id != self.healing_id or actor_id == self.outsider_id:
            raise RunScopeDenied
        return {
            "id": self.healing_id, "run_id": uuid4(),
            "run_case_id": uuid4(), "step_id": uuid4(), "action_id": uuid4(),
            "sequence": 2, "action_type": "click", "browser": "chromium",
            "old_locator": {"by": "role", "value": "button", "name": "Continue"},
            "new_locator": {"by": "role", "value": "button", "name": "Submit"},
            "source": "a11y_tree", "confidence": 0.91,
            "evidence": {"verification": {
                "match_count": 1, "visible": True,
                "descriptor": {"role": "button", "name": "Submit"},
            }},
            "status": self.status,
            "approval": {
                "id": uuid4(), "status": self.approval_status,
                "requested_by": self.requester_id,
                "decided_by": self.reviewer_id if self.approval_status != "PENDING" else None,
                "reason": "Reviewed" if self.approval_status != "PENDING" else None,
                "expires_at": datetime(2026, 10, 30, tzinfo=timezone.utc),
            },
            "expires_at": datetime(2026, 10, 30, tzinfo=timezone.utc),
            "new_version_id": None, "applied_at": None,
            "created_at": datetime(2026, 10, 29, tzinfo=timezone.utc),
        }

    def decide_healing(self, actor_id, healing_id, *, decision, reason, now):
        self.decision_calls.append({
            "actor_id": actor_id, "healing_id": healing_id,
            "decision": decision, "reason": reason,
        })
        if self.decision_error is not None:
            raise self.decision_error
        if actor_id == self.requester_id:
            raise RunApprovalPermissionDenied
        if healing_id != self.healing_id:
            raise RunScopeDenied
        if self.expired:
            return {"healing_status": "EXPIRED", "expired": True}
        self.status = "APPROVED" if decision == "APPROVE" else "REJECTED"
        self.approval_status = self.status
        return {"healing_status": self.status, "expired": False}


def _client():
    requester_id, reviewer_id, outsider_id = uuid4(), uuid4(), uuid4()
    healing_id, project_id = uuid4(), uuid4()
    repository = _HealingRepository(
        requester_id=requester_id, reviewer_id=reviewer_id,
        outsider_id=outsider_id, healing_id=healing_id, project_id=project_id,
    )
    app = FastAPI()
    app.include_router(create_run_router(
        repository=repository, case_repository=None,
        auth_sessions=_AuthSessions({
            "requester": {"id": str(requester_id), "session_id": str(uuid4())},
            "reviewer": {"id": str(reviewer_id), "session_id": str(uuid4())},
            "outsider": {"id": str(outsider_id), "session_id": str(uuid4())},
            "read_only_token": {
                "id": str(reviewer_id), "credential_type": "api_token",
                "organization_id": str(repository.organization_id),
                "scopes": ["resource:read"],
            },
        }),
    ))
    return app, repository, healing_id


def _request(app, method, path, *, headers=None, json=None):
    async def send():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        ) as client:
            return await client.request(method, path, headers=headers, json=json)

    return asyncio.run(send())


def test_get_healing_returns_the_scoped_review_dto():
    client, repository, healing_id = _client()

    response = _request(
        client, "GET", f"/api/v1/healing-records/{healing_id}",
        headers={"Authorization": "Bearer reviewer"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(healing_id)
    assert body["old_locator"]["name"] == "Continue"
    assert body["new_locator"]["name"] == "Submit"
    assert body["source"] == "a11y_tree"
    assert body["evidence"]["verification"]["descriptor"]["name"] == "Submit"
    assert body["approval"]["status"] == "PENDING"
    assert repository.get_calls[0][1] == healing_id


def test_get_healing_hides_missing_and_out_of_project_records_with_404():
    client, _repository, healing_id = _client()

    unknown = _request(
        client, "GET", f"/api/v1/healing-records/{uuid4()}",
        headers={"Authorization": "Bearer reviewer"},
    )
    out_of_scope = _request(
        client, "GET", f"/api/v1/healing-records/{healing_id}",
        headers={"Authorization": "Bearer outsider"},
    )

    assert unknown.status_code == 404
    assert out_of_scope.status_code == 404


def test_approve_and_reject_call_the_repository_and_return_terminal_dto():
    for decision in ("APPROVE", "REJECT"):
        client, repository, healing_id = _client()

        response = _request(
            client, "POST", f"/api/v1/healing-records/{healing_id}/decision",
            headers={"Authorization": "Bearer reviewer"},
            json={"decision": decision, "reason": "Reviewed the target evidence"},
        )

        expected = "APPROVED" if decision == "APPROVE" else "REJECTED"
        assert response.status_code == 200, response.text
        assert response.json()["status"] == expected
        assert repository.decision_calls[0]["decision"] == decision
        assert repository.decision_calls[0]["reason"] == "Reviewed the target evidence"


def test_decision_requires_a_nonblank_reason():
    client, _repository, healing_id = _client()

    response = _request(
        client, "POST", f"/api/v1/healing-records/{healing_id}/decision",
        headers={"Authorization": "Bearer reviewer"},
        json={"decision": "APPROVE", "reason": "  "},
    )

    assert response.status_code == 422


def test_requester_cannot_approve_own_healing():
    client, _repository, healing_id = _client()

    response = _request(
        client, "POST", f"/api/v1/healing-records/{healing_id}/decision",
        headers={"Authorization": "Bearer requester"},
        json={"decision": "APPROVE", "reason": "I approve my own proposal"},
    )

    assert response.status_code == 403


def test_expired_and_repeated_decisions_return_conflict():
    client, repository, healing_id = _client()
    repository.expired = True

    expired = _request(
        client, "POST", f"/api/v1/healing-records/{healing_id}/decision",
        headers={"Authorization": "Bearer reviewer"},
        json={"decision": "APPROVE", "reason": "Too late"},
    )
    repository.expired = False
    repository.decision_error = RunApprovalConflict("Healing is no longer pending")
    repeated = _request(
        client, "POST", f"/api/v1/healing-records/{healing_id}/decision",
        headers={"Authorization": "Bearer reviewer"},
        json={"decision": "REJECT", "reason": "Conflicting decision"},
    )

    assert expired.status_code == 409
    assert repeated.status_code == 409


def test_decision_hides_healings_outside_the_actor_project():
    client, _repository, healing_id = _client()

    response = _request(
        client, "POST", f"/api/v1/healing-records/{healing_id}/decision",
        headers={"Authorization": "Bearer outsider"},
        json={"decision": "REJECT", "reason": "Not authorized"},
    )

    assert response.status_code == 404


def test_decision_requires_approval_decide_token_scope():
    client, _repository, healing_id = _client()

    response = _request(
        client, "POST", f"/api/v1/healing-records/{healing_id}/decision",
        headers={"Authorization": "Bearer read_only_token"},
        json={"decision": "REJECT", "reason": "Read-only token"},
    )

    assert response.status_code == 403


def test_runtime_healing_routes_match_the_static_review_contract():
    runtime = create_app().openapi()
    static = yaml.safe_load((ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    expected_ids = {
        "get_api_v1_healing_records_healing_id",
        "post__api_v1_healing_records__id__decision",
    }
    runtime_operations = {
        operation["operationId"]: operation
        for path_item in runtime["paths"].values()
        for method, operation in path_item.items()
        if method.lower() in {"get", "post", "put", "patch", "delete", "options", "head"}
        and isinstance(operation, dict)
        and "operationId" in operation
    }
    assert expected_ids.issubset(runtime_operations)

    review_fields = {
        "id", "run_id", "run_case_id", "step_id", "action_id", "sequence",
        "action_type", "browser", "old_locator", "new_locator", "source",
        "confidence", "evidence", "status", "approval", "expires_at",
        "new_version_id", "applied_at", "created_at",
    }
    runtime_schema = runtime["components"]["schemas"]["HealingRecordView"]
    static_schema = static["components"]["schemas"]["HealingRecord"]
    assert review_fields.issubset(runtime_schema["properties"])
    assert review_fields == set(runtime_schema["required"])
    assert review_fields == set(static_schema["properties"])
    assert review_fields == set(static_schema["required"])

    runtime_decision = runtime["components"]["schemas"]["ApprovalDecision"]
    static_decision = static["components"]["schemas"][
        "V102PostApiV1HealingRecordsIdDecisionInput"
    ]
    assert runtime_decision["properties"]["reason"]["maxLength"] == 2000
    assert static_decision["properties"]["reason"]["maxLength"] == 2000
