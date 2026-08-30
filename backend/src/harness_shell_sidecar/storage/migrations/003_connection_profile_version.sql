BEGIN IMMEDIATE;

ALTER TABLE connection_profiles
ADD COLUMN version INTEGER NOT NULL DEFAULT 1
CHECK (version BETWEEN 1 AND 9007199254740991);

INSERT INTO schema_migrations(version, applied_at)
VALUES (3, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
