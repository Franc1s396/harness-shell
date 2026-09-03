BEGIN IMMEDIATE;

CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
) STRICT;

CREATE TABLE runtime_records (
    record_type TEXT NOT NULL,
    record_id TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    payload BLOB NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (record_type, record_id)
) STRICT;

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
    version INTEGER NOT NULL DEFAULT 1 CHECK (version BETWEEN 1 AND 9007199254740991),
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
    UNIQUE (connection_id, fingerprint_sha256)
) STRICT;

CREATE UNIQUE INDEX one_active_host_key_per_connection
ON host_keys(connection_id) WHERE status = 'active';

CREATE TABLE model_api_configs (
    api_config_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    api_type TEXT NOT NULL CHECK (api_type IN ('CHAT_COMPLETIONS', 'RESPONSES')),
    base_url TEXT NOT NULL CHECK (length(base_url) BETWEEN 1 AND 2048),
    model TEXT NOT NULL CHECK (length(model) BETWEEN 1 AND 255),
    api_key_credential_id TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE agent_conversations (
    conversation_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
) STRICT;

CREATE TABLE agent_runs (
    agent_run_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES agent_conversations(conversation_id),
    ssh_session_id TEXT NOT NULL,
    api_config_id TEXT NOT NULL REFERENCES model_api_configs(api_config_id),
    status TEXT NOT NULL CHECK (status IN ('RUNNING','COMPLETED','FAILED','LIMIT_REACHED','CANCELLED')),
    react_iteration INTEGER NOT NULL CHECK (react_iteration BETWEEN 0 AND 128),
    error_code TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
) STRICT;

CREATE TABLE agent_messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES agent_conversations(conversation_id),
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    message_type TEXT NOT NULL CHECK (message_type IN ('SYSTEM','HUMAN','AI','TOOL')),
    record_id TEXT NOT NULL UNIQUE,
    tool_call_id TEXT,
    agent_run_id TEXT NOT NULL REFERENCES agent_runs(agent_run_id),
    created_at TEXT NOT NULL,
    UNIQUE (conversation_id, sequence)
) STRICT;

INSERT INTO schema_migrations(version, applied_at)
VALUES (6, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
