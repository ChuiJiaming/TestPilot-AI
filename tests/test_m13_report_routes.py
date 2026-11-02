from datetime import datetime, timezone
import importlib
from uuid import UUID

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials

from apps.api.app.main import create_app


RUN_ID = UUID("70000000-0000-4000-8000-000000000001")
REPORT_ID = UUID("71000000-0000-4000-8000-000000000001")
EXPORT_ID = UUID("72000000-0000-4000-8000-000000000001")
ACTOR_ID = UUID("73000000-0000-4000-8000-000000000001")
ORG_ID = UUID("74000000-0000-4000-8000-000000000001")


class _Sessions:
    def authenticate(self, token):
        if token != "session-token":
            return None
        return {"id": str(ACTOR_ID), "credential_type": "user"}


class _Repository:
    def __init__(self, *, visible=True):
        self.visible = visible
        self.exports = []

    def organization_id_for_run(self, run_id):
        return ORG_ID if run_id == RUN_ID else None

    def organization_id_for_report(self, report_id):
        return ORG_ID if report_id == REPORT_ID else None

    def organization_id_for_export(self, export_id):
        return ORG_ID if export_id == EXPORT_ID else None

    def get_report(self, actor_id, run_id, *, limit, cursor=None):
        if cursor is not None and not cursor.startswith("artifacts:"):
            raise ValueError("invalid artifact cursor")
        if not self.visible or actor_id != ACTOR_ID or run_id != RUN_ID:
            return None
        return {
            "id": REPORT_ID, "run_id": RUN_ID,
            "summary": {"snapshot_hash": "fixed-hash"},
            "branch": "release/1.2", "commit_sha": "abc123def456",
        }

    def get_export(self, actor_id, export_id):
        if not self.visible or actor_id != ACTOR_ID or export_id != EXPORT_ID:
            return None
        return {"id": EXPORT_ID, "run_id": RUN_ID, "format": "pdf", "status": "QUEUED"}

    def report_run_id(self, actor_id, report_id):
        if not self.visible or actor_id != ACTOR_ID or report_id != REPORT_ID:
            return None
        return RUN_ID

    def create_export(self, actor_id, *, run_id, format, idempotency_key, now):
        self.exports.append((actor_id, run_id, format, idempotency_key))
        return {"id": EXPORT_ID, "run_id": run_id, "format": format, "status": "QUEUED"}


def _router(repository):
    runtime = create_app().openapi()
    operation_ids = {
        operation["operationId"]
        for path_item in runtime["paths"].values()
        for method, operation in path_item.items()
        if method.lower() in {"get", "post", "put", "patch", "delete"}
        and "operationId" in operation
    }
    expected = {
        "get__api_v1_test_runs__id__report",
        "get__api_v1_test_runs__id__report_export",
        "post__api_v1_report_exports",
        "get__api_v1_report_exports__id",
        "post__api_v1_reports__id__exports",
    }
    assert expected <= operation_ids, "report routes must be registered in the runtime API"
    module = importlib.import_module("apps.api.app.reports.routes")
    return module.create_report_router(repository=repository, auth_sessions=_Sessions())


def _endpoint(router, operation_id):
    route = next(item for item in router.routes if isinstance(item, APIRoute)
                 and item.operation_id == operation_id)
    return route


def test_scoped_out_report_is_hidden_as_not_found():
    router = _router(_Repository(visible=False))
    endpoint = _endpoint(router, "get__api_v1_test_runs__id__report").endpoint
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="session-token")

    with pytest.raises(HTTPException) as caught:
        endpoint(id=RUN_ID, limit=30, cursor=None, credentials=credentials)

    assert caught.value.status_code == 404


def test_report_and_legacy_export_return_report_dto_and_accept_pagination():
    repository = _Repository()
    router = _router(repository)
    report = _endpoint(router, "get__api_v1_test_runs__id__report")
    legacy = _endpoint(router, "get__api_v1_test_runs__id__report_export")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="session-token")

    current_result = report.endpoint(
        id=RUN_ID, limit=25, cursor="artifacts:page-2", credentials=credentials,
    )
    legacy_result = legacy.endpoint(
        id=RUN_ID, limit=10, cursor=None, credentials=credentials,
    )

    assert current_result == legacy_result
    assert current_result["run_id"] == RUN_ID
    assert "storage_key" not in current_result


def test_report_export_routes_return_202_and_keep_storage_keys_private():
    repository = _Repository()
    router = _router(repository)
    baseline = _endpoint(router, "post__api_v1_report_exports")
    nested = _endpoint(router, "post__api_v1_reports__id__exports")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="session-token")
    module = importlib.import_module("apps.api.app.reports.routes")

    baseline_result = baseline.endpoint(
        payload=module.ReportExportRequest(run_id=RUN_ID, format="pdf"),
        idempotency_key="baseline-export-123", credentials=credentials,
    )
    nested_result = nested.endpoint(
        id=REPORT_ID,
        payload=module.NestedReportExportRequest(format="json"),
        idempotency_key="nested-export-123", credentials=credentials,
    )

    assert baseline.status_code == nested.status_code == 202
    assert baseline_result["status"] == nested_result["status"] == "QUEUED"
    assert "storage_key" not in baseline_result and "storage_key" not in nested_result
    assert repository.exports == [
        (ACTOR_ID, RUN_ID, "pdf", "baseline-export-123"),
        (ACTOR_ID, RUN_ID, "json", "nested-export-123"),
    ]


def test_report_route_requires_authentication():
    router = _router(_Repository())
    endpoint = _endpoint(router, "get__api_v1_test_runs__id__report").endpoint

    with pytest.raises(HTTPException) as caught:
        endpoint(id=RUN_ID, limit=30, cursor=None, credentials=None)

    assert caught.value.status_code == 401


def test_report_snapshot_cursor_error_maps_to_validation_response():
    router = _router(_Repository())
    endpoint = _endpoint(router, "get__api_v1_test_runs__id__report").endpoint
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="session-token")

    with pytest.raises(HTTPException) as caught:
        endpoint(id=RUN_ID, limit=1, cursor="invalid", credentials=credentials)

    assert caught.value.status_code == 422
