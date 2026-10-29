-- v1 schema for MySQL. Booleans are TINYINT 0 and 1. JSON and timestamps
-- are text. InnoDB so foreign keys are enforced. utf8mb4 for keys and names.

CREATE TABLE IF NOT EXISTS schema_migrations (
    id VARCHAR(32) NOT NULL PRIMARY KEY
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE projects (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    `key` VARCHAR(128) NOT NULL,
    name VARCHAR(256) NOT NULL,
    CONSTRAINT uq_projects_key UNIQUE (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE environments (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    project_id BIGINT NOT NULL,
    `key` VARCHAR(128) NOT NULL,
    name VARCHAR(256) NOT NULL,
    CONSTRAINT uq_environments_project_key UNIQUE (project_id, `key`),
    CONSTRAINT fk_environments_project
        FOREIGN KEY (project_id) REFERENCES projects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE flags (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    project_id BIGINT NOT NULL,
    `key` VARCHAR(128) NOT NULL,
    name VARCHAR(256) NOT NULL,
    description TEXT NOT NULL,
    CONSTRAINT uq_flags_project_key UNIQUE (project_id, `key`),
    CONSTRAINT fk_flags_project
        FOREIGN KEY (project_id) REFERENCES projects (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE flag_variations (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    flag_id BIGINT NOT NULL,
    variation_index INTEGER NOT NULL,
    value_json TEXT NOT NULL,
    CONSTRAINT uq_flag_variations_index UNIQUE (flag_id, variation_index),
    CONSTRAINT fk_flag_variations_flag
        FOREIGN KEY (flag_id) REFERENCES flags (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE flag_configs (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    flag_id BIGINT NOT NULL,
    environment_id BIGINT NOT NULL,
    enabled TINYINT NOT NULL,
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
        FOREIGN KEY (environment_id) REFERENCES environments (id),
    CONSTRAINT ck_flag_configs_enabled CHECK (enabled IN (0, 1))
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE api_credentials (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    kind VARCHAR(32) NOT NULL,
    name VARCHAR(256) NULL,
    secret_hash VARCHAR(64) NOT NULL,
    prefix VARCHAR(16) NOT NULL,
    environment_id BIGINT NULL,
    created_at VARCHAR(32) NOT NULL,
    revoked_at VARCHAR(32) NULL,
    CONSTRAINT uq_api_credentials_secret_hash UNIQUE (secret_hash),
    CONSTRAINT fk_api_credentials_environment
        FOREIGN KEY (environment_id) REFERENCES environments (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE audit_events (
    id BIGINT NOT NULL AUTO_INCREMENT PRIMARY KEY,
    at VARCHAR(32) NOT NULL,
    actor_kind VARCHAR(32) NOT NULL,
    actor_id BIGINT NOT NULL,
    action VARCHAR(64) NOT NULL,
    project_key VARCHAR(128) NOT NULL,
    environment_key VARCHAR(128) NULL,
    flag_key VARCHAR(128) NULL,
    before_json TEXT NULL,
    after_json TEXT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
