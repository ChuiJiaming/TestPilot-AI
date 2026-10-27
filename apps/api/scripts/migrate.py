"""Apply versioned PostgreSQL migrations with an advisory lock and SHA-256 ledger.

The default mode is read-only validation. Use ``--apply`` only for a database
whose migration history and backup have already been reviewed.
"""

from argparse import ArgumentParser
import hashlib
import os
from pathlib import Path
import re
import sys

import psycopg


MIGRATION_NAME = re.compile(r"^\d{3}_[a-z0-9_]+\.sql$")
TRANSACTION_LINE = re.compile(r"^\s*(BEGIN|COMMIT)\s*;\s*(?:--.*)?$", re.IGNORECASE)
LEDGER_TABLE = "public.schema_migrations"
LOCK_NAME = "testpilot-schema-migrations"


class MigrationError(RuntimeError):
    pass


def repository_migrations(directory: Path) -> list[Path]:
    return sorted(
        path for path in directory.glob("*.sql")
        if MIGRATION_NAME.fullmatch(path.name) and "preflight" not in path.name
    )


def migration_source(path: Path) -> tuple[str, str]:
    source = path.read_text(encoding="utf-8")
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    statements = []
    begins = 0
    commits = 0
    for line in source.splitlines():
        match = TRANSACTION_LINE.fullmatch(line)
        if match:
            if match.group(1).upper() == "BEGIN":
                begins += 1
            else:
                commits += 1
            continue
        statements.append(line)
    if begins == 0 or begins != commits:
        raise MigrationError(f"{path.name}: expected balanced BEGIN/COMMIT blocks")
    return "\n".join(statements) + "\n", digest


def database_dsn(value: str) -> str:
    if value.startswith("postgresql+psycopg://"):
        return value.replace("postgresql+psycopg://", "postgresql://", 1)
    return value


def validate_ledger_order(migration_names: list[str], recorded_names: set[str]) -> None:
    missing_seen = False
    for name in migration_names:
        if name not in recorded_names:
            missing_seen = True
        elif missing_seen:
            raise MigrationError(f"Migration history has a gap before already-applied {name}")


def run_migrations(*, dsn: str, directory: Path, apply: bool) -> list[str]:
    migrations = repository_migrations(directory)
    if not migrations:
        raise MigrationError(f"No numbered migration files found in {directory}")
    sources = {path.name: migration_source(path) for path in migrations}
    applied_now: list[str] = []

    with psycopg.connect(database_dsn(dsn), autocommit=True) as connection:
        if apply:
            connection.execute(f"""
                CREATE TABLE IF NOT EXISTS {LEDGER_TABLE} (
                    version text PRIMARY KEY,
                    sha256 char(64) NOT NULL,
                    applied_at timestamptz NOT NULL DEFAULT now()
                )
            """)
        ledger_exists = connection.execute(
            "SELECT to_regclass(%s)", (LEDGER_TABLE,)
        ).fetchone()[0]
        if ledger_exists is None:
            raise MigrationError("public.schema_migrations is missing; run with --apply to initialize it")

        connection.execute("SELECT pg_advisory_lock(hashtext(%s))", (LOCK_NAME,))
        try:
            recorded_rows = connection.execute(
                f"SELECT version, sha256 FROM {LEDGER_TABLE} ORDER BY version"
            ).fetchall()
            recorded = {version: digest.strip() for version, digest in recorded_rows}
            unknown_versions = sorted(set(recorded) - set(sources))
            if unknown_versions:
                raise MigrationError(
                    "Ledger contains migrations absent from this source tree: "
                    + ", ".join(unknown_versions)
                )

            validate_ledger_order([path.name for path in migrations], set(recorded))
            for path in migrations:
                name = path.name
                source, digest = sources[name]
                previous = recorded.get(name)
                if previous is not None:
                    if previous != digest:
                        raise MigrationError(
                            f"{name} checksum differs from the applied ledger; refusing to continue"
                        )
                    print(f"VERIFIED {name} {digest}")
                    continue

                if not apply:
                    print(f"PENDING  {name} {digest}")
                    continue

                # SQL transaction wrappers are normalized so the ledger row and
                # the complete source file commit atomically, including 002's
                # adjacent V5/V6 blocks.
                with connection.transaction():
                    connection.execute(source, prepare=False)
                    connection.execute(
                        f"INSERT INTO {LEDGER_TABLE}(version, sha256) VALUES (%s, %s)",
                        (name, digest),
                    )
                applied_now.append(name)
                print(f"APPLIED  {name} {digest}")
        finally:
            connection.execute("SELECT pg_advisory_unlock(hashtext(%s))", (LOCK_NAME,))
    return applied_now


def main(argv: list[str] | None = None) -> int:
    parser = ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="verify ledger and source hashes (default)")
    mode.add_argument("--apply", action="store_true", help="apply unapplied migrations")
    parser.add_argument(
        "--directory", type=Path,
        default=Path(__file__).resolve().parents[3] / "db" / "migrations",
    )
    args = parser.parse_args(argv)
    dsn = os.getenv("TESTPILOT_DATABASE_URL")
    if not dsn:
        print("TESTPILOT_DATABASE_URL is required", file=sys.stderr)
        return 2
    try:
        run_migrations(dsn=dsn, directory=args.directory, apply=args.apply)
    except (MigrationError, psycopg.Error, OSError) as exc:
        print(f"migration error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
