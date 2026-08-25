BEGIN IMMEDIATE;

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE encrypted_records (
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    nonce BLOB NOT NULL CHECK (length(nonce) = 12),
    ciphertext BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (record_type, record_id)
);

CREATE TABLE audit_entries (
    sequence INTEGER PRIMARY KEY,
    event_id TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor TEXT NOT NULL,
    task_id TEXT,
    workflow_run_id TEXT,
    correlation_id TEXT,
    body_json TEXT NOT NULL,
    body_sha256 BLOB NOT NULL CHECK (length(body_sha256) = 32),
    previous_hmac BLOB NOT NULL CHECK (length(previous_hmac) = 32),
    entry_hmac BLOB NOT NULL CHECK (length(entry_hmac) = 32)
);

CREATE TABLE trace_spans (
    span_id TEXT PRIMARY KEY,
    trace_id TEXT NOT NULL,
    parent_span_id TEXT,
    name TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    attributes_json TEXT NOT NULL
);

INSERT INTO schema_migrations(version, applied_at)
VALUES (1, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;

