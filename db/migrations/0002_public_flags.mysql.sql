-- Public flags and the opaque id a browser sends to evaluate them.
-- public defaults to private. client_id stays null until an operator issues one.
-- Several null client ids are allowed; a non-null value is unique.

ALTER TABLE flags
    ADD COLUMN public TINYINT(1) NOT NULL DEFAULT 0,
    ADD CONSTRAINT ck_flags_public CHECK (public IN (0, 1));

ALTER TABLE environments
    ADD COLUMN client_id VARCHAR(64) NULL,
    ADD CONSTRAINT uq_environments_client_id UNIQUE (client_id);
