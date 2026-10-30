-- v1 schema for SQLite. Booleans are integers 0 and 1. JSON and timestamps
-- are text. The runner may already have created schema_migrations.

CREATE TABLE IF NOT EXISTS schema_migrations (
    id VARCHAR(32) NOT NULL PRIMARY KEY
);

CREATE TABLE projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    `key` TEXT NOT NULL,
    name TEXT NOT NULL,
    CONSTRAINT uq_projects_key UNIQUE (`key`)
);

CREATE TABLE environments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    `key` TEXT NOT NULL,
    name TEXT NOT NULL,
    CONSTRAINT uq_environments_project_key UNIQUE (project_id, `key`),
    CONSTRAINT fk_environments_project
        FOREIGN KEY (project_id) REFERENCES projects (id)
);

CREATE TABLE segments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment_id INTEGER NOT NULL,
    `key` TEXT NOT NULL,
    name TEXT NOT NULL,
    subjects_json TEXT NOT NULL,
    CONSTRAINT uq_segments_environment_key UNIQUE (environment_id, `key`),
    CONSTRAINT fk_segments_environment
        FOREIGN KEY (environment_id) REFERENCES environments (id)
);

CREATE TABLE flags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL,
    `key` TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT NOT NULL,
    CONSTRAINT uq_flags_project_key UNIQUE (project_id, `key`),
    CONSTRAINT fk_flags_project
        FOREIGN KEY (project_id) REFERENCES projects (id)
);

CREATE TABLE flag_variations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_id INTEGER NOT NULL,
    variation_index INTEGER NOT NULL,
    value_json TEXT NOT NULL,
    CONSTRAINT uq_flag_variations_index UNIQUE (flag_id, variation_index),
    CONSTRAINT fk_flag_variations_flag
        FOREIGN KEY (flag_id) REFERENCES flags (id)
);

CREATE TABLE flag_configs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    flag_id INTEGER NOT NULL,
    environment_id INTEGER NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    off_variation INTEGER NOT NULL,
    fallthrough_json TEXT NOT NULL,
    rules_json TEXT NOT NULL,
    targets_json TEXT NOT NULL,
    prerequisites_json TEXT NOT NULL,
    version INTEGER NOT NULL,
    CONSTRAINT uq_flag_configs_flag_environment UNIQUE (flag_id, environment_id),
    CONSTRAINT fk_flag_configs_flag
        FOREIGN KEY (flag_id) REFERENCES flags (id),
    CONSTRAINT fk_flag_configs_environment
        FOREIGN KEY (environment_id) REFERENCES environments (id)
);

CREATE TABLE api_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    name TEXT,
    secret_hash TEXT NOT NULL,
    prefix TEXT NOT NULL,
    environment_id INTEGER,
    created_at TEXT NOT NULL,
    revoked_at TEXT,
    CONSTRAINT uq_api_credentials_secret_hash UNIQUE (secret_hash),
    CONSTRAINT fk_api_credentials_environment
        FOREIGN KEY (environment_id) REFERENCES environments (id)
);

CREATE TABLE audit_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    at TEXT NOT NULL,
    actor_kind TEXT NOT NULL,
    actor_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    project_key TEXT NOT NULL,
    environment_key TEXT,
    flag_key TEXT,
    before_json TEXT,
    after_json TEXT
);
