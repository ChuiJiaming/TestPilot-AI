"""Detect ORM <-> migration drift that unit tests cannot see.

Tests build schema with ``AuthBase.metadata.create_all()`` on SQLite, i.e. from the
ORM models, never from ``db/migrations``. Anything that exists only in the SQL
migrations is therefore invisible to the suite. Three concrete failure classes have
already shipped this way:

1. A column the ORM declares but migrations never create
   (``approvals.payload``) -> UndefinedColumn on real PostgreSQL.
2. A NOT NULL / CHECK(col IS NOT NULL) column that migrations require but the ORM
   does not declare or populate (``suite_cases.project_id``, ``run_cases.project_id``)
   -> CheckViolation on insert, 500 at runtime.
3. A model module never imported in a given process, leaving its table out of the
   shared MetaData registry (``test_artifacts`` missing in the Celery worker)
   -> NoReferencedTableError while configuring an unrelated mapper.

Usage:
    TESTPILOT_DATABASE_URL=postgresql://... python -m apps.api.scripts.check_schema_drift
Exit code 0 = no fatal drift, 1 = fatal drift found.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

FATAL = "FATAL"
WARN = "WARN"

# Columns PostgreSQL adds itself; the ORM never needs to declare them.
_IGNORED_COLUMNS = {"schema_migrations"}


def _model_modules() -> tuple[str, ...]:
    from apps.api.app import orm_models

    return orm_models._MODEL_MODULES


def check_registry_completeness() -> list[tuple[str, str]]:
    """Every module declaring __tablename__ must be registered in orm_models."""
    findings: list[tuple[str, str]] = []
    registered = set(_model_modules())
    root = Path(__file__).resolve().parents[2] / "app"
    for path in sorted(root.rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "__tablename__" not in text:
            continue
        module = ".".join(
            ["apps", "api", *path.relative_to(root.parent.parent).with_suffix("").parts]
        )
        # normalise to apps.api.app.<rel>
        rel = path.relative_to(root.parent.parent)
        module = "apps.api." + ".".join(rel.with_suffix("").parts)
        if module not in registered:
            findings.append((FATAL, f"{module} declares __tablename__ but is missing from orm_models._MODEL_MODULES"))
    return findings


def check_schema(database_url: str) -> list[tuple[str, str]]:
    from sqlalchemy import create_engine, inspect, text

    from apps.api.app import orm_models
    from apps.api.app.auth.persistence import AuthBase

    orm_models.load_all_models()

    findings: list[tuple[str, str]] = []
    engine = create_engine(database_url)
    insp = inspect(engine)
    db_tables = set(insp.get_table_names(schema="public"))
    orm_tables = set(AuthBase.metadata.tables)

    for table in sorted(orm_tables - db_tables):
        findings.append((FATAL, f"{table}: ORM declares table but migrations never create it"))
    for table in sorted(db_tables - orm_tables - _IGNORED_COLUMNS):
        findings.append((WARN, f"{table}: table exists in DB but no ORM model maps it"))

    # Columns PostgreSQL requires on INSERT: NOT NULL without default, or a
    # CHECK (col IS NOT NULL) constraint.
    required_by_check: dict[str, set[str]] = {}
    with engine.connect() as conn:
        rows = conn.execute(text("""
            SELECT c.relname, pg_get_constraintdef(k.oid)
            FROM pg_constraint k
            JOIN pg_class c ON c.oid = k.conrelid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'public' AND k.contype = 'c'
        """)).all()
    for table, definition in rows:
        for column in re.findall(r"\(?([a-z_][a-z0-9_]*)\s+IS NOT NULL\)?", definition or ""):
            required_by_check.setdefault(table, set()).add(column)

    for table in sorted(orm_tables & db_tables):
        db_columns = {c["name"]: c for c in insp.get_columns(table, schema="public")}
        orm_columns = set(AuthBase.metadata.tables[table].columns.keys())

        for column in sorted(orm_columns - set(db_columns)):
            findings.append((FATAL, f"{table}.{column}: ORM column missing in DB (SELECT/INSERT will fail)"))

        for column in sorted(set(db_columns) - orm_columns):
            info = db_columns[column]
            must_fill = (not info["nullable"]) and info.get("default") is None
            if column in required_by_check.get(table, set()):
                must_fill = True
            level = FATAL if must_fill else WARN
            why = "NOT NULL / CHECK requires a value the ORM never writes" if must_fill else "unused by ORM"
            findings.append((level, f"{table}.{column}: column absent from ORM; {why}"))

    engine.dispose()
    return findings


def main() -> int:
    database_url = os.getenv("TESTPILOT_DATABASE_URL")
    if not database_url:
        print("TESTPILOT_DATABASE_URL is required", file=sys.stderr)
        return 2

    findings = check_registry_completeness() + check_schema(database_url)
    findings.sort(key=lambda item: (item[0] != FATAL, item[1]))

    fatal = sum(1 for level, _ in findings if level == FATAL)
    warn = len(findings) - fatal
    for level, message in findings:
        print(f"[{level:5}] {message}")
    print(f"\n{fatal} fatal, {warn} warning")
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
