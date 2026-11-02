from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials

from apps.api.app.quality.routes import create_quality_router


class _Sessions:
    def __init__(self, user_id):
        self.user_id = user_id

    def authenticate(self, _token):
        return {"id": str(self.user_id), "credential_type": "user"}


class _Repository:
    def __init__(self, user_id, project_id, artifact_id):
        self.user_id = user_id
        self.project_id = project_id
        self.artifact_id = artifact_id
        self.report_artifact_id = None
        self.download_calls = []

    def organization_id_for_project(self, project_id):
        return uuid4() if project_id == self.project_id else None

    def project_id_for_artifact(self, artifact_id):
        return self.project_id if artifact_id in {self.artifact_id, self.report_artifact_id} else None

    def download_artifact(self, actor_id, artifact_id):
        self.download_calls.append((actor_id, artifact_id))
        if actor_id != self.user_id or artifact_id != self.artifact_id:
            if actor_id != self.user_id or artifact_id != self.report_artifact_id:
                return None
            return ({
                "id": self.report_artifact_id,
                "project_id": self.project_id,
                "artifact_type": "report",
                "content_type": "application/pdf",
                "size_bytes": 128,
                "redacted": True,
            }, b"%PDF-private-report")
        return ({
            "id": self.artifact_id,
            "project_id": self.project_id,
            "artifact_type": "screenshot",
            "content_type": "image/png",
            "size_bytes": 70,
            "redacted": True,
        }, b"private-png")

    def list_artifacts(self, actor_id, project_id, *, limit, cursor=None, artifact_type=None):
        if actor_id != self.user_id or project_id != self.project_id:
            return {"items": [], "next_cursor": None}
        return {"items": [], "next_cursor": None}


def test_artifact_download_is_authenticated_project_scoped_and_never_redirects():
    user_id, project_id, artifact_id = uuid4(), uuid4(), uuid4()
    repository = _Repository(user_id, project_id, artifact_id)
    router = create_quality_router(
        repository=repository, auth_sessions=_Sessions(user_id),
    )
    route = next(item for item in router.routes if isinstance(item, APIRoute)
                 and item.path == "/api/v1/artifacts/{id}/download")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="session-token")

    response = route.endpoint(id=artifact_id, credentials=credentials)
    hidden_id = uuid4()
    with pytest.raises(HTTPException) as caught:
        route.endpoint(id=hidden_id, credentials=credentials)

    assert response.body == b"private-png"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "private, no-store"
    assert caught.value.status_code == 404
    assert repository.download_calls == [
        (user_id, artifact_id),
    ]


def test_report_artifact_download_returns_private_pdf_bytes_and_rechecks_scope():
    user_id, project_id, screenshot_id, report_id = uuid4(), uuid4(), uuid4(), uuid4()
    repository = _Repository(user_id, project_id, screenshot_id)
    repository.report_artifact_id = report_id
    router = create_quality_router(
        repository=repository, auth_sessions=_Sessions(user_id),
    )
    route = next(item for item in router.routes if isinstance(item, APIRoute)
                 and item.path == "/api/v1/artifacts/{id}/download")
    credentials = HTTPAuthorizationCredentials(scheme="Bearer", credentials="session-token")

    response = route.endpoint(id=report_id, credentials=credentials)

    assert response.body == b"%PDF-private-report"
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-disposition"] == f'attachment; filename="{report_id}.pdf"'
    assert response.headers["cache-control"] == "private, no-store"
    assert "storage_key" not in response.headers
    assert repository.download_calls == [(user_id, report_id)]
