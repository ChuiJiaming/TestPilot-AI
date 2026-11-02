from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials

from apps.api.app.main import create_app
from apps.api.app.operations.routes import (
    SystemSettingChangeRequest,
    create_operations_router,
)


ACTOR_ID = uuid4()
ORGANIZATION_ID = uuid4()


class _AuthSessions:
    def authenticate(self, token):
        if token != "admin":
            return None
        return {"id": str(ACTOR_ID), "credential_type": "session"}


class _Audit:
    def __init__(self):
        self.calls = []

    def list(self, actor_id, organization_id, *, filters, limit, cursor):
        self.calls.append((actor_id, organization_id, filters, limit, cursor))
        return {"items": [], "next_cursor": None}

    def export(self, *args, **kwargs):
        return {"filename": "audit.csv", "media_type": "text/csv", "content": "", "next_cursor": None}

    def verify_chain(self, organization_id, *, actor_id):
        return type("Verification", (), {"__dict__": {
            "valid": True, "entries_checked": 0, "first_invalid_entry_id": None,
            "expected_hash": None, "actual_hash": None,
        }})()


class _QuotaPolicy:
    def get_policy(self, actor_id, organization_id, *, now=None):
        return {"organization_id": organization_id, "max_concurrent_runs": 3,
                "monthly_token_limit": 1000, "monthly_tokens_committed": 0,
                "monthly_tokens_reserved": 0, "monthly_tokens_remaining": 1000,
                "quota_exhausted": False, "period_month": "2026-11-01", "updated_at": None}

    def update_policy(self, actor_id, organization_id, **kwargs):
        return {"organization_id": organization_id,
                "max_concurrent_runs": kwargs["max_concurrent_runs"],
                "monthly_token_limit": kwargs["monthly_token_limit"], "updated_at": kwargs["now"]}

    def list_usage(self, actor_id, organization_id, **kwargs):
        return {"items": []}


class _Settings:
    def __init__(self):
        self.requests = []

    def list_settings(self, actor_id):
        return {"items": [], "pending_changes": []}

    def request_change(self, actor_id, key, *, value, reason, now):
        self.requests.append((actor_id, key, value, reason))
        return {
            "id": uuid4(), "key": key, "status": "PENDING",
            "previous_value_hash": "0" * 64, "proposed_value_hash": "1" * 64,
            "requested_by": actor_id, "approved_by": None, "reason": reason,
            "created_at": now, "decided_at": None,
        }

    def decide_change(self, *args, **kwargs):
        from apps.api.app.operations.settings import SettingForbidden
        raise SettingForbidden


def _router(audit=None, settings=None):
    return create_operations_router(
        notification_service=object(), auth_sessions=_AuthSessions(),
        audit_repository=audit or _Audit(), quota_policy_service=_QuotaPolicy(),
        system_setting_service=settings or _Settings(),
    )


def _endpoint(router, operation_id):
    return next(route for route in router.routes if isinstance(route, APIRoute)
                and route.operation_id == operation_id)


def _credentials(token="admin"):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_m15_baseline_and_additive_operations_are_registered_at_runtime():
    runtime = create_app().openapi()
    operation_ids = {
        operation["operationId"]
        for path_item in runtime["paths"].values()
        for method, operation in path_item.items()
        if method.lower() in {"get", "post", "put", "patch", "delete"}
        and isinstance(operation, dict)
        and "operationId" in operation
    }
    expected = {
        "get__api_v1_admin_system_settings",
        "put__api_v1_admin_system_settings__key",
        "get__api_v1_organizations__id__audit_logs",
        "get__api_v1_organizations__id__quotas",
        "get__api_v1_organizations__id__usage",
        "post__api_v1_audit_logs_export",
        "put__api_v1_organizations__id__quotas",
        "get__api_v1_audit_logs",
        "get__api_v1_audit_logs_verify",
        "get__api_v1_quota_policies",
        "put__api_v1_quota_policies",
        "get__api_v1_usage",
        "get__api_v1_system_settings",
        "put__api_v1_system_settings",
        "post__api_v1_admin_system_setting_changes__id__decision",
    }

    assert expected <= operation_ids


def test_system_setting_route_requires_authentication():
    route = _endpoint(_router(), "get__api_v1_system_settings")

    with pytest.raises(HTTPException) as caught:
        route.endpoint(credentials=None)

    assert caught.value.status_code == 401


def test_system_setting_route_creates_a_pending_change_without_approval():
    settings = _Settings()
    router = _router(settings=settings)
    route = _endpoint(router, "put__api_v1_system_settings")
    payload = SystemSettingChangeRequest(
        key="maintenance_mode", value=True, reason="Scheduled maintenance",
    )

    result = route.endpoint(payload=payload, credentials=_credentials())

    assert result["status"] == "PENDING"
    assert settings.requests == [(ACTOR_ID, "maintenance_mode", True, "Scheduled maintenance")]


def test_audit_and_quota_routes_pass_organization_scope_to_services():
    audit = _Audit()
    router = _router(audit=audit)
    audit_route = _endpoint(router, "get__api_v1_audit_logs")
    quota_route = _endpoint(router, "get__api_v1_quota_policies")

    audit_result = audit_route.endpoint(
        organization_id=ORGANIZATION_ID, action="run.failed", resource_type=None,
        actor_user_id=None, resource_id=None, created_at_from=None, created_at_to=None,
        limit=20, cursor=None, credentials=_credentials(),
    )
    quota_result = quota_route.endpoint(
        organization_id=ORGANIZATION_ID, credentials=_credentials(),
    )

    assert audit_result["items"] == []
    assert audit.calls[0][0:2] == (ACTOR_ID, ORGANIZATION_ID)
    assert audit.calls[0][2]["action"] == "run.failed"
    assert quota_result["organization_id"] == ORGANIZATION_ID
