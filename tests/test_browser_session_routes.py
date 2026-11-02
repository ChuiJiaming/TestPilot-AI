from contextlib import contextmanager
from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials

from apps.api.app.runs.repository import RunScopeDenied
from apps.api.app.runs.routes import create_run_router


class _AuthSessions:
    def __init__(self, principals):
        self.principals = principals

    def authenticate(self, token):
        return self.principals.get(token)


class _Repository:
    def __init__(self, *, user_id, outsider_id, session_id, project_id, organization_id):
        self.user_id = user_id
        self.outsider_id = outsider_id
        self.session_id = session_id
        self.project_id = project_id
        self.organization_id = organization_id
        self.get_calls = []

    @contextmanager
    def session_factory(self):
        class _Session:
            def scalar(_self, _statement):
                return self.organization_id

        yield _Session()

    def project_id_for_browser_session(self, session_id):
        return self.project_id if session_id == self.session_id else None

    def get_browser_session(self, actor_id, session_id):
        self.get_calls.append((actor_id, session_id))
        if session_id != self.session_id or actor_id == self.outsider_id:
            raise RunScopeDenied
        return {
            "id": self.session_id,
            "run_case_id": uuid4(),
            "worker_instance": "browser-worker-a",
            "browser": "chromium",
            "status": "READY",
        }


@pytest.fixture
def browser_session_route():
    user_id, outsider_id = uuid4(), uuid4()
    session_id, project_id, organization_id = uuid4(), uuid4(), uuid4()
    repository = _Repository(
        user_id=user_id, outsider_id=outsider_id, session_id=session_id,
        project_id=project_id, organization_id=organization_id,
    )
    router = create_run_router(
        repository=repository, case_repository=None,
        auth_sessions=_AuthSessions({
            "member": {"id": str(user_id), "credential_type": "user"},
            "outsider": {"id": str(outsider_id), "credential_type": "user"},
            "read_token": {
                "id": str(user_id), "credential_type": "api_token",
                "organization_id": str(organization_id),
                "scopes": ["resource:read"],
            },
            "write_token": {
                "id": str(user_id), "credential_type": "api_token",
                "organization_id": str(organization_id),
                "scopes": ["resource:write"],
            },
        }),
    )
    route = next(item for item in router.routes if isinstance(item, APIRoute)
                 and item.path == "/api/v1/browser-sessions/{id}")
    return route, repository, session_id


def _credentials(token):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_get_browser_session_returns_scoped_safe_metadata(browser_session_route):
    route, repository, session_id = browser_session_route

    response = route.endpoint(id=session_id, credentials=_credentials("member"))

    assert response["id"] == session_id
    assert response["browser"] == "chromium"
    assert "attempt_token" not in response
    assert "lease_expires_at" not in response
    assert repository.get_calls[0][1] == session_id
    assert route.operation_id == "get__api_v1_browser_sessions__id"


def test_get_browser_session_hides_missing_and_out_of_project_records(browser_session_route):
    route, _repository, session_id = browser_session_route

    for hidden_id, token in ((uuid4(), "member"), (session_id, "outsider")):
        with pytest.raises(HTTPException) as caught:
            route.endpoint(id=hidden_id, credentials=_credentials(token))
        assert caught.value.status_code == 404


def test_get_browser_session_requires_auth_and_read_scope(browser_session_route):
    route, _repository, session_id = browser_session_route

    with pytest.raises(HTTPException) as unauthenticated:
        route.endpoint(id=session_id, credentials=None)
    assert unauthenticated.value.status_code == 401

    with pytest.raises(HTTPException) as insufficient_scope:
        route.endpoint(id=session_id, credentials=_credentials("write_token"))
    assert insufficient_scope.value.status_code == 403
