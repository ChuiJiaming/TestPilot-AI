"""Import every module that declares an ORM table mapping.

SQLAlchemy resolves ``ForeignKey("some_table.id")`` strings through a single
shared ``MetaData`` registry. If a model module is never imported in a process,
its table is absent from that registry and configuring *any* mapper that
references it raises ``NoReferencedTableError``.

The API process imports most modules transitively through ``main.py``, so this
failure mode hides in tests. Worker processes (Celery) import a much smaller
graph, which is why ``report_exports.artifact_id -> test_artifacts.id`` only
exploded inside ``render_report_export`` at runtime.

Import this module early in every process that touches the ORM (API, Celery
worker, Celery beat, one-off scripts) so the registry is always complete.

Add any new table-declaring module to ``_MODEL_MODULES`` below. The companion
check in ``apps/api/scripts/check_schema_drift.py`` fails if a module declares
``__tablename__`` but is missing here.
"""
from __future__ import annotations

_MODEL_MODULES = (
    "apps.api.app.auth.lifecycle",
    "apps.api.app.auth.persistence",
    "apps.api.app.cases.persistence",
    "apps.api.app.cases.planning",
    "apps.api.app.core.audit",
    "apps.api.app.environments.persistence",
    "apps.api.app.integrations.delivery",
    "apps.api.app.integrations.persistence",
    "apps.api.app.operations.persistence",
    "apps.api.app.quality.persistence",
    "apps.api.app.reports.persistence",
    "apps.api.app.runs.persistence",
    "apps.api.app.tenancy.persistence",
)

__all__ = ["_MODEL_MODULES", "load_all_models"]


def load_all_models() -> None:
    """Import every ORM model module so foreign-key targets resolve."""
    import importlib

    for module in _MODEL_MODULES:
        importlib.import_module(module)
