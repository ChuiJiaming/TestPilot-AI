from uuid import uuid4

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials

from apps.api.app.main import create_app
from apps.api.app.operations.notifications import NotificationForbidden
from apps.api.app.operations.routes import (
    NotificationPreferenceUpdate,
    create_operations_router,
)


ACTOR_ID = uuid4()
OTHER_ID = uuid4()
NOTIFICATION_ID = uuid4()


class _AuthSessions:
    def authenticate(self, token):
        actor_id = {"owner": ACTOR_ID, "other": OTHER_ID}.get(token)
        return {"id": str(actor_id), "credential_type": "session"} if actor_id else None


class _NotificationService:
    def __init__(self):
        self.preferences = [{"channel": "email", "event_type": "run.failed", "enabled": True}]

    def list_notifications(self, actor_id, *, limit=50, cursor=None):
        return {"items": [{"id": NOTIFICATION_ID, "kind": "run.failed", "payload": {},
                            "read_at": None, "created_at": "2026-11-02T08:00:00Z"}],
                "next_cursor": None}

    def mark_read(self, actor_id, notification_id, *, now):
        if actor_id != ACTOR_ID or notification_id != NOTIFICATION_ID:
            raise NotificationForbidden
        return True

    def list_preferences(self, actor_id):
        return {"items": list(self.preferences)}

    def update_preferences(self, actor_id, values):
        self.preferences = values
        return {"items": list(values)}

    def list_dead_deliveries(self, actor_id, *, organization_id=None, limit=100):
        return {"items": [], "next_cursor": None}


def _router():
    return create_operations_router(
        notification_service=_NotificationService(),
        auth_sessions=_AuthSessions(),
    )


def _endpoint(router, operation_id):
    return next(route for route in router.routes if isinstance(route, APIRoute)
                and route.operation_id == operation_id)


def _credentials(token="owner"):
    return HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)


def test_notification_routes_require_authentication():
    route = _endpoint(_router(), "get__api_v1_notifications")

    with pytest.raises(HTTPException) as caught:
        route.endpoint(limit=50, cursor=None, credentials=None)

    assert caught.value.status_code == 401


def test_user_can_read_own_notification_and_preferences():
    router = _router()
    credentials = _credentials()
    notifications = _endpoint(router, "get__api_v1_notifications").endpoint(
        limit=50, cursor=None, credentials=credentials,
    )
    preferences = _endpoint(router, "get__api_v1_notifications_preferences").endpoint(
        credentials=credentials,
    )
    update = _endpoint(router, "put__api_v1_notifications_preferences").endpoint(
        payload=NotificationPreferenceUpdate(items=[{
            "channel": "email", "event_type": "run.failed", "enabled": False,
        }]),
        credentials=credentials,
    )
    dead_deliveries = _endpoint(router, "get__api_v1_notification_deliveries").endpoint(
        status="DEAD", organization_id=None, limit=100, credentials=credentials,
    )

    assert notifications["items"][0]["kind"] == "run.failed"
    assert preferences["items"][0]["enabled"] is True
    assert update["items"][0]["enabled"] is False
    assert dead_deliveries["items"] == []


def test_user_cannot_mark_another_users_notification_read():
    route = _endpoint(_router(), "post__api_v1_notifications__id__read")

    with pytest.raises(HTTPException) as caught:
        route.endpoint(id=NOTIFICATION_ID, credentials=_credentials("other"))

    assert caught.value.status_code == 403


def test_notification_operation_ids_are_registered_in_runtime_api():
    operation_ids = {
        operation["operationId"]
        for path in create_app().openapi()["paths"].values()
        for method, operation in path.items()
        if method.lower() in {"get", "post", "put", "patch", "delete"}
        and "operationId" in operation
    }

    assert {
        "get__api_v1_notifications",
        "post__api_v1_notifications__id__read",
        "get__api_v1_notifications_preferences",
        "put__api_v1_notifications_preferences",
        "get__api_v1_notification_deliveries",
    } <= operation_ids
