"""Process wiring.

This module is the only place that chooses the database engine and the
cache. Other modules receive the objects it builds.
"""

import os
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from api.http import Application
from cache.memory import MemoryCache
from cache.null import NullCache
from cache.protocol import Cache
from domain.errors import ValidationError
from domain.services.authentication import JwtSettings
from domain.validation import DEFAULT_EVAL_TTL_SECONDS, MIN_JWT_SECRET_BYTES
from repositories.db import Database
from repositories.eval_cache import EvaluationCache
from repositories.migrate import apply_migrations, default_migrations_directory


@dataclass(frozen=True, repr=False)
class Settings:
    """Configuration for one process.

    mysql_password and jwt_secret are secrets. __repr__ leaves them out so a
    log of this object does not record them. Building Settings does not open
    a connection. JWT fields are loaded here; issue and verify land later.
    """

    engine: str
    sqlite_path: str
    mysql_host: str
    mysql_port: int
    mysql_database: str
    mysql_user: str
    mysql_password: str
    cache: str
    bind_host: str
    bind_port: int
    eval_ttl_seconds: int
    jwt_secret: str
    jwt_issuer: str
    jwt_audience: str
    jwt_ttl_seconds: int
    jwt_jwks_url: str
    client_eval_limit: int
    client_eval_window_seconds: int
    migrations_directory: Path

    def __repr__(self) -> str:
        """Describe the process settings without secret values."""
        return (
            "Settings("
            f"engine={self.engine!r}, sqlite_path={self.sqlite_path!r}, "
            f"mysql_host={self.mysql_host!r}, mysql_port={self.mysql_port!r}, "
            f"mysql_database={self.mysql_database!r}, mysql_user={self.mysql_user!r}, "
            f"cache={self.cache!r}, bind_host={self.bind_host!r}, "
            f"bind_port={self.bind_port!r}, eval_ttl_seconds={self.eval_ttl_seconds!r}, "
            f"jwt_issuer={self.jwt_issuer!r}, jwt_audience={self.jwt_audience!r}, "
            f"jwt_ttl_seconds={self.jwt_ttl_seconds!r}, jwt_jwks_url={self.jwt_jwks_url!r}, "
            f"client_eval_limit={self.client_eval_limit!r}, "
            f"client_eval_window_seconds={self.client_eval_window_seconds!r})"
        )


@dataclass(frozen=True)
class Runtime:
    """What prepare() builds for one process.

    database opens a connection per request. cache is shared by the process.
    application answers HTTP but does not listen until serve() is called.
    """

    settings: Settings
    database: Database
    cache: Cache
    application: Application


def load_settings(environment: Mapping[str, str] | None = None) -> Settings:
    """Read configuration from environment.

    When environment is omitted, os.environ is used. Names start with
    FLAGGATOR_, for example FLAGGATOR_ENGINE and FLAGGATOR_SQLITE_PATH.
    SQLite, the memory cache, and 127.0.0.1 are the defaults. The MySQL
    password and JWT signing secret are copied onto Settings and are not
    logged here. Raises ValidationError when a port or TTL is not an
    integer. Does not open a connection.
    """
    source = os.environ if environment is None else environment
    jwt_secret = _jwt_secret(source.get("FLAGGATOR_JWT_SECRET", ""))
    return Settings(
        engine=source.get("FLAGGATOR_ENGINE", "sqlite"),
        sqlite_path=source.get("FLAGGATOR_SQLITE_PATH", "flaggator.db"),
        mysql_host=source.get("FLAGGATOR_MYSQL_HOST", "localhost"),
        mysql_port=_port(source.get("FLAGGATOR_MYSQL_PORT", "3306"), "mysql_port"),
        mysql_database=source.get("FLAGGATOR_MYSQL_DATABASE", "flaggator"),
        mysql_user=source.get("FLAGGATOR_MYSQL_USER", ""),
        mysql_password=source.get("FLAGGATOR_MYSQL_PASSWORD", ""),
        cache=source.get("FLAGGATOR_CACHE", "memory"),
        bind_host=source.get("FLAGGATOR_BIND_HOST", "127.0.0.1"),
        bind_port=_port(source.get("FLAGGATOR_BIND_PORT", "8080"), "bind_port"),
        eval_ttl_seconds=_positive_int(
            source.get("FLAGGATOR_EVAL_TTL_SECONDS", str(DEFAULT_EVAL_TTL_SECONDS)),
            "eval_ttl_seconds",
        ),
        jwt_secret=jwt_secret,
        jwt_issuer=source.get("FLAGGATOR_JWT_ISSUER", "flaggator"),
        jwt_audience=source.get("FLAGGATOR_JWT_AUDIENCE", "flaggator"),
        jwt_ttl_seconds=_positive_int(
            source.get("FLAGGATOR_JWT_TTL_SECONDS", "3600"),
            "jwt_ttl_seconds",
        ),
        jwt_jwks_url=source.get("FLAGGATOR_JWT_JWKS_URL", ""),
        client_eval_limit=_positive_int(
            source.get("FLAGGATOR_CLIENT_EVAL_LIMIT", "60"),
            "client_eval_limit",
        ),
        client_eval_window_seconds=_positive_int(
            source.get("FLAGGATOR_CLIENT_EVAL_WINDOW_SECONDS", "60"),
            "client_eval_window_seconds",
        ),
        migrations_directory=default_migrations_directory(),
    )


def build_database(settings: Settings) -> Database:
    """Return the database selected by settings.engine.

    Does not connect. Raises ValidationError for an unknown engine. The
    MySQL class is imported only when that engine is selected.
    """
    if settings.engine == "sqlite":
        from repositories.sqlite import SqliteDatabase

        return SqliteDatabase(settings.sqlite_path)
    if settings.engine == "mysql":
        from repositories.mysql import MysqlDatabase

        return MysqlDatabase(
            host=settings.mysql_host,
            port=settings.mysql_port,
            database=settings.mysql_database,
            user=settings.mysql_user,
            password=settings.mysql_password,
        )
    raise ValidationError(
        "The database engine is not supported.",
        details=[{"field": "engine", "message": "Use sqlite or mysql."}],
    )


def build_cache(settings: Settings) -> Cache:
    """Return the cache selected by settings.cache.

    memory is the process-local map. null stores nothing. Raises
    ValidationError for an unknown name. Does not connect to Redis.
    """
    if settings.cache == "memory":
        return MemoryCache()
    if settings.cache == "null":
        return NullCache()
    raise ValidationError(
        "The cache is not supported.",
        details=[{"field": "cache", "message": "Use memory or null."}],
    )


def prepare(settings: Settings) -> Runtime:
    """Build the database and the cache, and apply pending migrations.

    Raises StorageError when a migration fails. Does not bind a port.
    """
    database = build_database(settings)
    apply_migrations(database, settings.migrations_directory)
    cache = build_cache(settings)
    return Runtime(
        settings=settings,
        database=database,
        cache=cache,
        application=Application(
            database,
            EvaluationCache(cache, settings.eval_ttl_seconds),
            JwtSettings(
                secret=settings.jwt_secret,
                issuer=settings.jwt_issuer,
                audience=settings.jwt_audience,
                ttl_seconds=settings.jwt_ttl_seconds,
                jwks_url=settings.jwt_jwks_url,
            ),
            client_eval_limit=settings.client_eval_limit,
            client_eval_window_seconds=settings.client_eval_window_seconds,
        ),
    )


def bootstrap_management_token(
    settings: Settings | None = None,
    secret_out: TextIO | None = None,
    notice_out: TextIO | None = None,
) -> None:
    """Create one management token and write the secret once.

    Applies migrations first. The secret goes to secret_out. A short notice
    goes to notice_out and does not contain the secret. Nothing is written
    to the audit log. Does not bind a port. When settings is omitted,
    load_settings() is used.
    """
    from domain.services.credentials import issue_management_token

    resolved = load_settings() if settings is None else settings
    secret_stream = sys.stdout if secret_out is None else secret_out
    notice_stream = sys.stderr if notice_out is None else notice_out
    runtime = prepare(resolved)
    connection = runtime.database.connect()
    try:
        with connection.transaction():
            token = issue_management_token(connection)
    finally:
        connection.close()
    notice_stream.write("Save this token. It will not be shown again.\n")
    secret_stream.write(token + "\n")
    secret_stream.flush()


def _jwt_secret(raw: str) -> str:
    """Return raw when it is empty or at least 32 UTF-8 bytes.

    Empty means local login is off. A shorter non-empty secret is rejected
    so HS256 is not used with a weak key. Does not log raw.
    """
    if raw == "":
        return raw
    if len(raw.encode("utf-8")) < MIN_JWT_SECRET_BYTES:
        raise ValidationError(
            "The JWT signing secret is not valid.",
            details=[
                {
                    "field": "jwt_secret",
                    "message": "FLAGGATOR_JWT_SECRET must be at least 32 bytes.",
                }
            ],
        )
    return raw


def _positive_int(raw: str, field: str) -> int:
    """Parse raw as an integer greater than zero.

    field is the settings name used in the validation detail. Raises
    ValidationError when raw is not a positive integer. Does not connect.
    """
    if not raw.isdigit() or raw == "0" or (len(raw) > 1 and raw[0] == "0"):
        raise ValidationError(
            "The value is not a positive integer.",
            details=[{"field": field, "message": "Use a whole number greater than zero."}],
        )
    return int(raw)


def _port(raw: str, field: str) -> int:
    """Parse raw as a TCP port.

    field is the settings name used in the validation detail. Raises
    ValidationError when raw is not an integer. Does not check that the
    port is free.
    """
    if not raw.isdigit():
        raise ValidationError(
            "The port is not an integer.",
            details=[{"field": field, "message": "The port must be a number."}],
        )
    return int(raw)
