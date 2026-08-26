BEGIN IMMEDIATE;

CREATE TABLE connection_profiles (
    connection_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    group_name TEXT CHECK (group_name IS NULL OR length(group_name) BETWEEN 1 AND 80),
    host TEXT NOT NULL CHECK (length(host) BETWEEN 1 AND 255),
    port INTEGER NOT NULL CHECK (port BETWEEN 1 AND 65535),
    username TEXT NOT NULL CHECK (length(username) BETWEEN 1 AND 128),
    auth_kind TEXT NOT NULL CHECK (auth_kind IN ('password', 'private_key')),
    credential_id TEXT NOT NULL,
    passphrase_credential_id TEXT,
    proxy_jump_id TEXT REFERENCES connection_profiles(connection_id) ON DELETE RESTRICT,
    favorite INTEGER NOT NULL CHECK (favorite IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK (proxy_jump_id IS NULL OR proxy_jump_id <> connection_id),
    CHECK (auth_kind = 'private_key' OR passphrase_credential_id IS NULL)
) STRICT;

CREATE TABLE host_keys (
    host_key_id TEXT PRIMARY KEY,
    connection_id TEXT NOT NULL REFERENCES connection_profiles(connection_id) ON DELETE CASCADE,
    key_algorithm TEXT NOT NULL,
    fingerprint_sha256 TEXT NOT NULL,
    public_key_openssh BLOB NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'replaced')),
    confirmed_at TEXT NOT NULL,
    replaced_at TEXT,
    UNIQUE(connection_id, fingerprint_sha256)
) STRICT;

CREATE UNIQUE INDEX one_active_host_key_per_connection
ON host_keys(connection_id) WHERE status = 'active';

CREATE TABLE artifact_metadata (
    artifact_id TEXT PRIMARY KEY,
    sha256 TEXT NOT NULL,
    byte_count INTEGER NOT NULL CHECK (byte_count >= 0),
    media_type TEXT NOT NULL,
    sensitivity TEXT NOT NULL CHECK (sensitivity IN ('normal', 'sensitive')),
    complete INTEGER NOT NULL CHECK (complete IN (0, 1)),
    created_at TEXT NOT NULL
) STRICT;

INSERT INTO schema_migrations(version, applied_at)
VALUES (2, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
