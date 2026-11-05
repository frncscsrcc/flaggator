-- Public flags and the opaque id a browser sends to evaluate them.
-- public defaults to private. client_id stays null until an operator issues one.
-- Several null client ids are allowed; a non-null value is unique.

ALTER TABLE flags ADD COLUMN public INTEGER NOT NULL DEFAULT 0 CHECK (public IN (0, 1));

ALTER TABLE environments ADD COLUMN client_id TEXT NULL;

CREATE UNIQUE INDEX uq_environments_client_id ON environments (client_id);
