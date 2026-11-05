"""Database engine construction with explicit pool and timeout settings.

Why this exists: SQLAlchemy's defaults (pool_size=5, max_overflow=10) are chosen
for a laptop, not for an API process serving concurrent tenants. Worse, the
default has no statement timeout at all, so one missing index can hold a
connection until the client gives up and the pool starves.

Every value is read from the environment so a deployment can tune it without a
code change, and every value has a documented production-safe default.
"""

from __future__ import annotations

import os

from sqlalchemy import create_engine


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if value <= 0:
        raise ValueError(f"{name} must be positive")
    return value


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def build_engine(url: str, *, statement_timeout_ms: int | None = None):
    """Return a SQLAlchemy engine sized for a server process.

    SQLite is only used by the unit-test suite; it accepts none of the pool
    arguments below, so it is handled separately and deliberately left alone.
    """
    if is_sqlite(url):
        return create_engine(
            url,
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )

    pool_size = _int_env("TESTPILOT_DB_POOL_SIZE", 10)
    max_overflow = _int_env("TESTPILOT_DB_MAX_OVERFLOW", 20)
    recycle = _int_env("TESTPILOT_DB_POOL_RECYCLE_SECONDS", 1800)
    timeout_ms = statement_timeout_ms or _int_env("TESTPILOT_DB_STATEMENT_TIMEOUT_MS", 15000)

    connect_args: dict[str, object] = {}
    if url.startswith("postgresql"):
        # psycopg3 passes server options through libpq.
        connect_args["options"] = f"-c statement_timeout={timeout_ms}"
        connect_args["connect_timeout"] = _int_env("TESTPILOT_DB_CONNECT_TIMEOUT_SECONDS", 5)

    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=pool_size,
        max_overflow=max_overflow,
        pool_recycle=recycle,
        pool_timeout=_int_env("TESTPILOT_DB_POOL_TIMEOUT_SECONDS", 10),
        connect_args=connect_args,
    )
