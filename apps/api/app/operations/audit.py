"""Organization-scoped audit history and hash-chain verification."""

from csv import DictWriter
from dataclasses import dataclass
from datetime import datetime, timezone
from io import StringIO
import json
import re
from uuid import UUID

from sqlalchemy import and_, or_, select

from apps.api.app.auth.persistence import UserRecord
from apps.api.app.core.audit import AuditLogRecord, audit_entry_hash
from apps.api.app.tenancy.persistence import (
    OrganizationMembershipRecord,
    OrganizationRecord,
)


class AuditForbidden(Exception):
    pass


@dataclass(frozen=True)
class AuditVerificationResult:
    valid: bool
    entries_checked: int
    first_invalid_entry_id: int | None = None
    expected_hash: str | None = None
    actual_hash: str | None = None


_SENSITIVE_KEY = re.compile(
    r"secret|password|passwd|token|cookie|authorization|credential|api.?key|access.?key|response.?body",
    re.IGNORECASE,
)


def _safe_metadata(value):
    if isinstance(value, dict):
        return {
            key: _safe_metadata(item)
            for key, item in value.items()
            if not _SENSITIVE_KEY.search(str(key))
        }
    if isinstance(value, list):
        return [_safe_metadata(item) for item in value]
    return value


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


class AuditRepository:
    def __init__(self, session_factory):
        self.session_factory = session_factory

    def authorize_scope(self, actor_id: UUID, organization_id: UUID) -> None:
        if not isinstance(organization_id, UUID):
            raise ValueError("organization_id is required")
        with self.session_factory() as session:
            user = session.get(UserRecord, actor_id)
            organization = session.get(OrganizationRecord, organization_id)
            membership = session.get(
                OrganizationMembershipRecord, (organization_id, actor_id),
            )
            if organization is None or user is None:
                raise AuditForbidden
            if user.platform_role != "platform_admin" and membership is None:
                raise AuditForbidden

    def list(
        self,
        actor_id: UUID,
        organization_id: UUID,
        *,
        filters: dict | None,
        limit: int,
        cursor: str | None,
    ) -> dict:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        self.authorize_scope(actor_id, organization_id)
        normalized_filters = self._normalize_filters(filters or {})
        with self.session_factory() as session:
            statement = select(AuditLogRecord).where(
                AuditLogRecord.organization_id == organization_id,
            )
            for field, value in normalized_filters.items():
                column = getattr(AuditLogRecord, field)
                if field == "created_at_from":
                    statement = statement.where(AuditLogRecord.created_at >= value)
                elif field == "created_at_to":
                    statement = statement.where(AuditLogRecord.created_at <= value)
                else:
                    statement = statement.where(column == value)
            if cursor is not None:
                try:
                    cursor_id = int(cursor)
                except (TypeError, ValueError) as exc:
                    raise ValueError("audit cursor is invalid") from exc
                cursor_row = session.scalar(select(AuditLogRecord).where(
                    AuditLogRecord.id == cursor_id,
                    AuditLogRecord.organization_id == organization_id,
                ))
                if cursor_row is None:
                    raise ValueError("audit cursor is invalid")
                statement = statement.where(or_(
                    AuditLogRecord.created_at < cursor_row.created_at,
                    and_(
                        AuditLogRecord.created_at == cursor_row.created_at,
                        AuditLogRecord.id < cursor_row.id,
                    ),
                ))
            rows = session.scalars(statement.order_by(
                AuditLogRecord.created_at.desc(), AuditLogRecord.id.desc(),
            ).limit(limit + 1)).all()
            has_more = len(rows) > limit
            items = rows[:limit]
            return {
                "items": [self._dto(row) for row in items],
                "next_cursor": str(items[-1].id) if has_more and items else None,
            }

    def export(
        self,
        actor_id: UUID,
        organization_id: UUID,
        *,
        filters: dict | None = None,
        limit: int = 100,
        cursor: str | None = None,
    ) -> dict:
        if type(limit) is not int or not 1 <= limit <= 500:
            raise ValueError("export limit must be between 1 and 500")
        items = []
        next_cursor = cursor
        has_more = False
        while len(items) < limit:
            result = self.list(
                actor_id, organization_id, filters=filters,
                limit=min(100, limit - len(items)), cursor=next_cursor,
            )
            items.extend(result["items"])
            if result["next_cursor"] is None:
                next_cursor = None
                break
            next_cursor = result["next_cursor"]
            has_more = True
        if len(items) < limit:
            next_cursor = None
        output = StringIO(newline="")
        fields = [
            "id", "actor_user_id", "action", "resource_type", "resource_id",
            "metadata", "entry_hash", "created_at",
        ]
        writer = DictWriter(output, fieldnames=fields)
        writer.writeheader()
        for item in items:
            writer.writerow({
                **item,
                "metadata": json.dumps(item["metadata"], sort_keys=True, ensure_ascii=False),
            })
        return {
            "filename": f"audit-{organization_id}.csv",
            "media_type": "text/csv; charset=utf-8",
            "content": output.getvalue(),
            "next_cursor": next_cursor if has_more else None,
        }

    def verify_chain(
        self, organization_id: UUID | None, *, actor_id: UUID | None = None,
    ) -> AuditVerificationResult:
        if not isinstance(organization_id, UUID):
            raise ValueError("organization_id is required for chain verification")
        if actor_id is not None:
            self.authorize_scope(actor_id, organization_id)
        with self.session_factory() as session:
            rows = session.scalars(select(AuditLogRecord).where(
                AuditLogRecord.organization_id == organization_id,
            ).order_by(AuditLogRecord.id.asc())).all()
            previous_hash = None
            for index, row in enumerate(rows, start=1):
                expected = audit_entry_hash(
                    organization_id=row.organization_id,
                    actor_user_id=row.actor_user_id,
                    action=row.action,
                    resource_type=row.resource_type,
                    resource_id=row.resource_id,
                    metadata=row.metadata_json,
                    created_at=_utc(row.created_at),
                    previous_hash=previous_hash,
                )
                if row.previous_hash != previous_hash or row.entry_hash != expected:
                    return AuditVerificationResult(
                        valid=False,
                        entries_checked=index,
                        first_invalid_entry_id=row.id,
                        expected_hash=expected,
                        actual_hash=row.entry_hash,
                    )
                previous_hash = row.entry_hash
            return AuditVerificationResult(valid=True, entries_checked=len(rows))

    @staticmethod
    def _normalize_filters(filters: dict) -> dict:
        if not isinstance(filters, dict):
            raise ValueError("audit filters must be an object")
        allowed = {
            "action": "action",
            "resource_type": "resource_type",
            "actor_user_id": "actor_user_id",
            "resource_id": "resource_id",
            "created_at_from": "created_at_from",
            "created_at_to": "created_at_to",
        }
        unknown = set(filters) - set(allowed)
        if unknown:
            raise ValueError("unsupported audit filter")
        result = {}
        for key, value in filters.items():
            if value is None:
                continue
            if key in {"actor_user_id", "resource_id"}:
                try:
                    value = UUID(str(value))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"{key} must be a UUID") from exc
            elif key in {"created_at_from", "created_at_to"}:
                if not isinstance(value, datetime) or value.tzinfo is None:
                    raise ValueError(f"{key} must be timezone-aware")
                value = _utc(value)
            elif not isinstance(value, str) or not value.strip() or len(value) > 160:
                raise ValueError(f"{key} must be a short non-empty string")
            result[allowed[key]] = value
        if ("created_at_from" in result and "created_at_to" in result
                and result["created_at_from"] > result["created_at_to"]):
            raise ValueError("audit time range is invalid")
        return result

    @staticmethod
    def _dto(row: AuditLogRecord) -> dict:
        return {
            "id": row.id,
            "organization_id": row.organization_id,
            "actor_user_id": row.actor_user_id,
            "action": row.action,
            "resource_type": row.resource_type,
            "resource_id": row.resource_id,
            "metadata": _safe_metadata(dict(row.metadata_json or {})),
            "previous_hash": row.previous_hash,
            "entry_hash": row.entry_hash,
            "created_at": row.created_at,
        }
