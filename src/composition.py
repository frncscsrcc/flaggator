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
from repositories.db import Database
from repositories.migrate import apply_migrations, default_migrations_directory


@dataclass(frozen=True, repr=False)
class Settings:
    """Configuration for one process.

    mysql_password is a secret. __repr__ leaves it out so a log of this
    object does not record it. Building Settings does not open a connection.
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
    migrations_directory: Path

    def __repr__(self) -> str:
        """Describe the process settings without the database password."""
        return (
            "Settings("
            f"engine={self.engine!r}, sqlite_path={self.sqlite_path!r}, "
            f"mysql_host={self.mysql_host!r}, mysql_port={self.mysql_port!r}, "
            f"mysql_database={self.mysql_database!r}, mysql_user={self.mysql_user!r}, "
            f"cache={self.cache!r}, bind_host={self.bind_host!r}, "
            f"bind_port={self.bind_port!r})"
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
    password is copied onto Settings and is not logged here. Raises
    ValidationError when the port is not an integer. Does not open a
    connection.
    """
    source = os.environ if environment is None else environment
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
    return Runtime(
        settings=settings,
        database=database,
        cache=build_cache(settings),
        application=Application(database),
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
