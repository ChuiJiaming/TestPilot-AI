#!/usr/bin/env bash
# Sureshot — verify a backup file without touching production.
#
# Two checks, both cheap:
#   1. SHA-256 matches the manifest written by backup-postgres.sh.
#   2. pg_restore can list the archive (proves the custom-format header is intact).
#
# Usage: ./verify-backup.sh /var/backups/sureshot/sureshot-20261105T030000Z.dump
set -euo pipefail

dump="${1:?usage: verify-backup.sh <path-to-dump>}"
manifest="$dump.sha256"

if [ ! -f "$dump" ]; then
  echo "[verify] FATAL: $dump does not exist" >&2
  exit 1
fi
if [ ! -f "$manifest" ]; then
  echo "[verify] FATAL: manifest $manifest is missing; cannot prove integrity" >&2
  exit 1
fi

expected="$(awk 'NR==1{print $1}' "$manifest")"
actual="$(sha256sum "$dump" | awk '{print $1}')"
if [ "$expected" != "$actual" ]; then
  echo "[verify] FATAL: checksum mismatch (expected $expected, got $actual)" >&2
  exit 1
fi

if ! pg_restore --list "$dump" >/dev/null; then
  echo "[verify] FATAL: archive header is unreadable" >&2
  exit 1
fi

echo "[verify] OK: $(basename "$dump") sha256=$actual"
