-- Email comparison is already binary on SQLite. This file only adds the generation.
ALTER TABLE users ADD COLUMN token_generation INTEGER NOT NULL DEFAULT 0;
