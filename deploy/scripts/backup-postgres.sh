#!/usr/bin/env bash
# Sureshot — PostgreSQL logical backup with retention and optional off-host copy.
#
# Why a script instead of a cron one-liner: an unrestored backup is not a backup.
# This script therefore writes a manifest with the dump's SHA-256 and byte size,
# so `verify-backup.sh` can prove the file is intact before anyone needs it.
#
# Required:
#   TESTPILOT_DATABASE_URL      postgresql://user:pass@host:5432/db
# Optional:
#   TESTPILOT_BACKUP_DIR        default /var/backups/sureshot
#   TESTPILOT_BACKUP_RETAIN_DAYS default 14
#   TESTPILOT_BACKUP_S3_URI     e.g. s3://acme-backups/sureshot (requires aws CLI)
#
# Usage: ./backup-postgres.sh
set -euo pipefail

: "${TESTPILOT_DATABASE_URL:?TESTPILOT_DATABASE_URL is required}"
BACKUP_DIR="${TESTPILOT_BACKUP_DIR:-/var/backups/sureshot}"
RETAIN_DAYS="${TESTPILOT_BACKUP_RETAIN_DAYS:-14}"
S3_URI="${TESTPILOT_BACKUP_S3_URI:-}"

umask 077
mkdir -p "$BACKUP_DIR"

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump="$BACKUP_DIR/sureshot-$stamp.dump"
manifest="$dump.sha256"

echo "[backup] dumping to $dump"
pg_dump "$TESTPILOT_DATABASE_URL" --format=custom --no-owner --no-privileges --file="$dump"

# A zero-byte dump means pg_dump "succeeded" against a broken pipe or an empty
# template. Treat it as a failure rather than rotating away good backups.
size="$(wc -c <"$dump" | tr -d ' ')"
if [ "$size" -lt 1024 ]; then
  echo "[backup] FATAL: dump is only ${size} bytes" >&2
  rm -f "$dump"
  exit 1
fi

digest="$(sha256sum "$dump" | awk '{print $1}')"
printf '%s  %s\n%s\n' "$digest" "$(basename "$dump")" "bytes=$size" >"$manifest"
echo "[backup] sha256=$digest bytes=$size"

if [ -n "$S3_URI" ]; then
  echo "[backup] uploading to $S3_URI"
  aws s3 cp "$dump" "$S3_URI/$(basename "$dump")" --only-show-errors
  aws s3 cp "$manifest" "$S3_URI/$(basename "$manifest")" --only-show-errors
fi

# Retention: delete dumps older than the window together with their manifests.
find "$BACKUP_DIR" -maxdepth 1 -name 'sureshot-*.dump' -type f \
  -mtime "+$RETAIN_DAYS" -print -delete
find "$BACKUP_DIR" -maxdepth 1 -name 'sureshot-*.dump.sha256' -type f \
  -mtime "+$RETAIN_DAYS" -print -delete

echo "[backup] done; retained ${RETAIN_DAYS}d in $BACKUP_DIR"
