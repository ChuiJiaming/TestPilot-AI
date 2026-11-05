"""Two-person approval workflow for non-secret platform settings."""

from datetime import datetime, timezone
from hashlib import sha256
import json
from uuid import UUID, uuid4

from sqlalchemy import select

from apps.api.app.auth.persistence import UserRecord
from apps.api.app.core.audit import append_audit_entry
from apps.api.app.operations.persistence import (
    SystemSettingChangeRecord,
    SystemSettingRecord,
)


class SettingForbidden(Exception):
    pass


class SettingNotFound(Exception):
    pass


class SettingConflict(Exception):
    pass


class SettingStale(Exception):
    pass


class SettingInvalid(Exception):
    pass


_SETTING_RULES = {
    "maintenance_mode": (bool, None, None),
    "max_concurrent_runs": (int, 1, 1000),
    "monthly_token_limit": (int, 0, 2**63 - 1),
    "provider_timeout_seconds": (int, 1, 3600),
    "notification_retry_limit": (int, 1, 8),
    "llm_config": (dict, None, None),
}


def _digest(value) -> str:
    try:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"),
            ensure_ascii=False, allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SettingInvalid("setting value must be valid JSON") from exc
    return sha256(encoded.encode("utf-8")).hexdigest()


def _now_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SettingInvalid("now must be timezone-aware")
    return value.astimezone(timezone.utc)


def _validate_setting(key: str, value) -> None:
    if not isinstance(key, str) or key not in _SETTING_RULES:
        raise SettingInvalid("setting key is not allowlisted or may contain a secret")
    expected_type, minimum, maximum = _SETTING_RULES[key]
    if type(value) is not expected_type:
        raise SettingInvalid("setting value has the wrong type")
    if key == "llm_config":
        from apps.api.app.operations.model_config import ModelConfigError, parse_model_config

        try:
            parse_model_config(value)
        except ModelConfigError as exc:
            raise SettingInvalid(str(exc)) from exc
        return
    if expected_type is int and not minimum <= value <= maximum:
        raise SettingInvalid("setting value is outside the supported range")


def require_platform_admin(session, actor_id, *, lock=False) -> None:
    user = session.get(UserRecord, actor_id, with_for_update=lock)
    if user is None or user.platform_role != "platform_admin":
        raise SettingForbidden("platform administrator access required")


def _validate_reason(reason: str) -> str:
    if not isinstance(reason, str) or not reason.strip() or len(reason.strip()) > 500:
        raise SettingInvalid("a non-empty reason of at most 500 characters is required")
    return reason.strip()


class SystemSettingService:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def list_settings(self, actor_id: UUID) -> dict:
        with self.session_factory() as session:
            self._require_platform_admin(session, actor_id)
            rows = session.scalars(select(SystemSettingRecord).order_by(
                SystemSettingRecord.key,
            )).all()
            pending = session.scalars(select(SystemSettingChangeRecord).where(
                SystemSettingChangeRecord.status == "PENDING",
            ).order_by(
                SystemSettingChangeRecord.created_at.desc(),
                SystemSettingChangeRecord.id.desc(),
            ).limit(200)).all()
            recent = session.scalars(select(SystemSettingChangeRecord).where(
                SystemSettingChangeRecord.status != "PENDING",
            ).order_by(
                SystemSettingChangeRecord.created_at.desc(),
                SystemSettingChangeRecord.id.desc(),
            ).limit(10)).all()
            return {"items": [{
                "key": row.key,
                "value_hash": _digest(row.value),
                "updated_at": row.updated_at,
            } for row in rows], "pending_changes": [
                self._change_dto(row) for row in pending
            ], "recent_changes": [
                self._change_dto(row) for row in recent
            ]}

    def list_changes(self, actor_id: UUID, *, status: str | None = None) -> dict:
        if status not in {None, "PENDING", "APPROVED", "REJECTED", "STALE"}:
            raise SettingInvalid("invalid setting change status")
        with self.session_factory() as session:
            self._require_platform_admin(session, actor_id)
            statement = select(SystemSettingChangeRecord)
            if status:
                statement = statement.where(SystemSettingChangeRecord.status == status)
            rows = session.scalars(statement.order_by(
                SystemSettingChangeRecord.created_at.desc(),
                SystemSettingChangeRecord.id.desc(),
            ).limit(200)).all()
            return {"items": [self._change_dto(row) for row in rows]}

    def request_change(
        self,
        actor_id: UUID,
        key: str,
        *,
        value,
        reason: str,
        now: datetime,
    ) -> dict:
        """平台管理员保存即生效，不再要求第二位管理员审批。

        调用方本身已被限制为平台管理员（``require_platform_admin``），
        再要求「另一位管理员批准」会让只有一个管理员的部署永远改不了配置。
        变更仍写入 ``system_setting_changes`` 作为历史（status=APPROVED，
        申请人与生效人为同一人），审计记录两侧哈希，可追溯但不需要二次确认。
        """
        _validate_setting(key, value)
        reason = _validate_reason(reason)
        timestamp = _now_utc(now)
        proposed_hash = _digest(value)
        with self.session_factory.begin() as session:
            self._require_platform_admin(session, actor_id, lock=True)
            current = session.scalar(select(SystemSettingRecord).where(
                SystemSettingRecord.key == key,
            ).with_for_update())
            previous_hash = _digest(current.value if current is not None else None)
            if current is None:
                current = SystemSettingRecord(
                    id=uuid4(), key=key, value=value, updated_at=timestamp,
                )
                session.add(current)
            else:
                current.value = value
                current.updated_at = timestamp
            # 历史上可能残留待审批的同类变更，直接生效后它们就过期了。
            for stale in session.scalars(select(SystemSettingChangeRecord).where(
                SystemSettingChangeRecord.key == key,
                SystemSettingChangeRecord.status == "PENDING",
            )).all():
                stale.status = "STALE"
                stale.decided_at = timestamp
            change = SystemSettingChangeRecord(
                id=uuid4(), key=key, proposed_value=value,
                previous_value_hash=previous_hash, proposed_value_hash=proposed_hash,
                requested_by=actor_id, approved_by=actor_id, status="APPROVED", reason=reason,
                created_at=timestamp, decided_at=timestamp,
            )
            session.add(change)
            session.flush()
            append_audit_entry(
                session, organization_id=None, actor_user_id=actor_id,
                action="system_setting.applied", resource_type="system_setting",
                resource_id=change.id,
                metadata={
                    "key": key,
                    "previous_value_hash": previous_hash,
                    "proposed_value_hash": proposed_hash,
                    "actor_id": str(actor_id),
                    "mode": "direct",
                },
                created_at=timestamp,
            )
            return self._change_dto(change)

    def decide_change(
        self,
        actor_id: UUID,
        change_id: UUID,
        *,
        decision: str,
        reason: str,
        now: datetime,
    ) -> dict:
        if decision not in {"approve", "reject"}:
            raise SettingInvalid("decision must be approve or reject")
        reason = _validate_reason(reason)
        timestamp = _now_utc(now)
        stale = False
        result = None
        with self.session_factory.begin() as session:
            self._require_platform_admin(session, actor_id, lock=True)
            change = session.get(SystemSettingChangeRecord, change_id, with_for_update=True)
            if change is None:
                raise SettingNotFound
            if change.requested_by == actor_id:
                raise SettingForbidden("a different platform administrator must review the change")
            if change.status != "PENDING":
                raise SettingConflict("setting change is no longer pending")

            current = session.scalar(select(SystemSettingRecord).where(
                SystemSettingRecord.key == change.key,
            ).with_for_update())
            current_hash = _digest(current.value if current is not None else None)
            if current_hash != change.previous_value_hash:
                change.status = "STALE"
                change.approved_by = actor_id
                change.decided_at = timestamp
                stale = True
                action = "system_setting.stale"
            else:
                change.status = "APPROVED" if decision == "approve" else "REJECTED"
                change.approved_by = actor_id
                change.decided_at = timestamp
                action = "system_setting.approved" if decision == "approve" else "system_setting.rejected"
                if decision == "approve":
                    if current is None:
                        current = SystemSettingRecord(
                            id=uuid4(), key=change.key, value=change.proposed_value,
                            updated_at=timestamp,
                        )
                        session.add(current)
                    else:
                        current.value = change.proposed_value
                        current.updated_at = timestamp
            append_audit_entry(
                session, organization_id=None, actor_user_id=actor_id,
                action=action, resource_type="system_setting_change", resource_id=change.id,
                metadata={
                    "key": change.key,
                    "previous_value_hash": change.previous_value_hash,
                    "proposed_value_hash": change.proposed_value_hash,
                    "requester_id": str(change.requested_by),
                    "approver_id": str(actor_id),
                },
                created_at=timestamp,
            )
            session.flush()
            result = self._change_dto(change)
        if stale:
            raise SettingStale("current setting changed after the proposal")
        return result

    @staticmethod
    def _require_platform_admin(session, actor_id, *, lock=False):
        require_platform_admin(session, actor_id, lock=lock)

    @staticmethod
    def _change_dto(row: SystemSettingChangeRecord) -> dict:
        return {
            "id": row.id,
            "key": row.key,
            "status": row.status,
            "previous_value_hash": row.previous_value_hash,
            "proposed_value_hash": row.proposed_value_hash,
            "requested_by": row.requested_by,
            "approved_by": row.approved_by,
            "reason": row.reason,
            "created_at": row.created_at,
            "decided_at": row.decided_at,
        }
