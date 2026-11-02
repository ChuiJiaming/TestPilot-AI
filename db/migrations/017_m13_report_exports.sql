BEGIN;

ALTER TABLE reports
    ADD COLUMN IF NOT EXISTS snapshot_hash char(64);

UPDATE reports
SET snapshot_hash = summary->>'snapshot_hash'
WHERE snapshot_hash IS NULL
  AND jsonb_typeof(summary) = 'object'
  AND summary->>'snapshot_hash' ~ '^[0-9a-f]{64}$';

ALTER TABLE reports
    ADD CONSTRAINT ck_reports_snapshot_hash_format
    CHECK (snapshot_hash IS NULL OR snapshot_hash ~ '^[0-9a-f]{64}$');

ALTER TABLE report_exports
    ADD COLUMN IF NOT EXISTS requester_id uuid REFERENCES users(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS idempotency_key varchar(160),
    ADD COLUMN IF NOT EXISTS attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    ADD COLUMN IF NOT EXISTS available_at timestamptz,
    ADD COLUMN IF NOT EXISTS lease_owner text,
    ADD COLUMN IF NOT EXISTS lease_expires_at timestamptz,
    ADD COLUMN IF NOT EXISTS last_error_code varchar(80);

UPDATE report_exports SET available_at = created_at WHERE available_at IS NULL;

ALTER TABLE report_exports
    ALTER COLUMN available_at SET DEFAULT now(),
    ALTER COLUMN available_at SET NOT NULL,
    ADD CONSTRAINT ck_report_exports_idempotency_key_length
        CHECK (idempotency_key IS NULL OR length(idempotency_key) BETWEEN 8 AND 160),
    ADD CONSTRAINT ck_report_exports_lease_pair
        CHECK ((lease_owner IS NULL) = (lease_expires_at IS NULL)),
    ADD CONSTRAINT ck_report_exports_error_code
        CHECK (last_error_code IS NULL OR last_error_code ~ '^[A-Z][A-Z0-9_.:-]{0,79}$');

CREATE UNIQUE INDEX IF NOT EXISTS uq_report_exports_requester_idempotency
    ON report_exports(requester_id, idempotency_key)
    WHERE requester_id IS NOT NULL AND idempotency_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS ix_report_exports_due
    ON report_exports(status, available_at, lease_expires_at, created_at)
    WHERE status IN ('QUEUED', 'RUNNING');

INSERT INTO outbox_events (
    aggregate_type, aggregate_id, event_type, payload, status, attempts, available_at, created_at
)
SELECT
    'report_export', export.id, 'REPORT_EXPORT_REQUESTED',
    jsonb_build_object(
        'export_id', export.id,
        'report_id', report.id,
        'run_id', export.run_id
    ),
    'PENDING', 0, now(), now()
FROM report_exports AS export
JOIN reports AS report ON report.run_id = export.run_id
WHERE export.status = 'QUEUED'
  AND NOT EXISTS (
      SELECT 1 FROM outbox_events AS event
      WHERE event.aggregate_type = 'report_export'
        AND event.aggregate_id = export.id
        AND event.event_type = 'REPORT_EXPORT_REQUESTED'
  );

COMMIT;
