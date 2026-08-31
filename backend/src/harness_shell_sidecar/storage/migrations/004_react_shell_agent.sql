BEGIN IMMEDIATE;

CREATE TABLE model_api_configs (
    api_config_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL CHECK(length(display_name) BETWEEN 1 AND 80),
    api_type TEXT NOT NULL CHECK(api_type IN ('CHAT_COMPLETIONS', 'RESPONSES')),
    base_url TEXT NOT NULL CHECK(length(base_url) BETWEEN 1 AND 2048),
    model TEXT NOT NULL CHECK(length(model) BETWEEN 1 AND 255),
    api_key_secret_ref TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
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
    status TEXT NOT NULL CHECK(status IN ('RUNNING','COMPLETED','FAILED','LIMIT_REACHED','CANCELLED')),
    react_iteration INTEGER NOT NULL CHECK(react_iteration BETWEEN 0 AND 128),
    error_code TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT
) STRICT;

CREATE TABLE agent_messages (
    message_id TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES agent_conversations(conversation_id),
    sequence INTEGER NOT NULL CHECK(sequence > 0),
    message_type TEXT NOT NULL CHECK(message_type IN ('SYSTEM','HUMAN','AI','TOOL')),
    encrypted_record_id TEXT NOT NULL UNIQUE,
    tool_call_id TEXT,
    agent_run_id TEXT NOT NULL REFERENCES agent_runs(agent_run_id),
    created_at TEXT NOT NULL,
    UNIQUE(conversation_id, sequence)
) STRICT;

INSERT INTO schema_migrations(version, applied_at)
VALUES (4, strftime('%Y-%m-%dT%H:%M:%fZ', 'now'));

COMMIT;
