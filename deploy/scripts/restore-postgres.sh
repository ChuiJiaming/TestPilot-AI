#!/usr/bin/env bash
# Sureshot — restore a dump into a *named* database. Never restores into a DSN.
#
# The destructive step is deliberately explicit: you pass a target database name
# and a superuser DSN for the server, not an application DSN. That prevents the
# classic mistake of restoring over production because an env var was still set.
#
# Usage:
#   ./restore-postgres.sh <dump> <target-db> [admin-dsn]
#
# Example:
#   ./restore-postgres.sh /var/backups/sureshot/sureshot-20261105T030000Z.dump sureshot_restore_check
set -euo pipefail

dump="${1:?usage: restore-postgres.sh <dump> <target-db> [admin-dsn]}"
target="${2:?target database name is required}"
admin_dsn="${3:-${TESTPILOT_ADMIN_DATABASE_URL:-}}"

if [ -z "$admin_dsn" ]; then
  echo "[restore] FATAL: pass an admin DSN or set TESTPILOT_ADMIN_DATABASE_URL" >&2
  exit 1
fi

case "$target" in
  *[!A-Za-z0-9_]*|"")
    echo "[restore] FATAL: target database name must be [A-Za-z0-9_]+" >&2
    exit 1
    ;;
esac

echo "[restore] verifying archive first"
"$(dirname "$0")/verify-backup.sh" "$dump"

echo "[restore] recreating database $target"
psql "$admin_dsn" -v ON_ERROR_STOP=1 -c "DROP DATABASE IF EXISTS \"$target\" WITH (FORCE)"
psql "$admin_dsn" -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$target\""

echo "[restore] restoring into $target"
pg_restore --dbname="${admin_dsn%/*}/$target" --no-owner --no-privileges --exit-on-error "$dump"

echo "[restore] verifying migration ledger in the restored copy"
TESTPILOT_DATABASE_URL="${admin_dsn%/*}/$target" python -m apps.api.scripts.migrate --check

echo "[restore] OK: $target is restored and its ledger verifies"
