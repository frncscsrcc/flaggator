"""SQLite sessions.

This module is the only one that imports sqlite3. Repository SQL stays in
other modules and uses the connection returned here.
"""

import sqlite3
from collections.abc import Mapping, Sequence

from domain.errors import ConstraintViolation, StorageError
from repositories.db import Connection, Transactional


class SqliteCursor:
    """A sqlite3 cursor whose rows are plain dictionaries."""

    def __init__(self, raw: sqlite3.Cursor) -> None:
        """Keep the driver cursor. Does not fetch."""
        self._raw = raw

    def fetchone(self) -> Mapping[str, object] | None:
        """Return the next row by column name, or None when there are no more."""
        row = self._raw.fetchone()
        if row is None:
            return None
        return dict(row)

    def fetchall(self) -> list[Mapping[str, object]]:
        """Return the remaining rows. An empty result is an empty list."""
        return [dict(row) for row in self._raw.fetchall()]

    @property
    def last_insert_id(self) -> int:
        """The integer id SQLite assigned to the last insert on this cursor."""
        return int(self._raw.lastrowid)

    @property
    def rowcount(self) -> int:
        """How many rows the statement changed, as SQLite reports it."""
        return int(self._raw.rowcount)


class SqliteConnection(Transactional):
    """One SQLite session with explicit transactions.

    isolation_level is None on the driver connection, so Python does not
    open a second transaction behind BEGIN. One request owns this object
    and does not share it.
    """

    def __init__(self, raw: sqlite3.Connection) -> None:
        """Adopt a connection that already has foreign keys and WAL set."""
        super().__init__()
        self._raw = raw

    def execute(self, sql: str, params: tuple = ()) -> SqliteCursor:
        """Run one statement. `?` placeholders are passed through to SQLite.

        A unique violation becomes ConstraintViolation. Any other driver
        error becomes StorageError with a generic message.
        """
        try:
            raw_cursor = self._raw.execute(sql, params)
        except sqlite3.Error as exc:
            raise _translate_sqlite_error(exc) from exc
        return SqliteCursor(raw_cursor)

    def executemany(self, sql: str, params: Sequence[tuple]) -> SqliteCursor:
        """Run one statement for each parameter tuple.

        The error translation matches execute.
        """
        try:
            raw_cursor = self._raw.executemany(sql, params)
        except sqlite3.Error as exc:
            raise _translate_sqlite_error(exc) from exc
        return SqliteCursor(raw_cursor)

    def close(self) -> None:
        """Close the file session. Does not commit."""
        self._raw.close()

    def _begin(self) -> None:
        """Reserve the write lock now.

        BEGIN IMMEDIATE makes a second writer wait here instead of both
        passing a version check and failing at commit.
        """
        try:
            self._raw.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise StorageError() from exc

    def _commit(self) -> None:
        """Commit the open transaction."""
        try:
            self._raw.execute("COMMIT")
        except sqlite3.Error as exc:
            raise StorageError() from exc

    def _rollback(self) -> None:
        """Roll back the open transaction."""
        try:
            self._raw.execute("ROLLBACK")
        except sqlite3.Error as exc:
            raise StorageError() from exc


class SqliteDatabase:
    """Opens SQLite sessions on one file.

    engine is "sqlite". DDL in a migration runs inside a transaction.
    """

    engine = "sqlite"
    transactional_ddl = True

    def __init__(self, path: str) -> None:
        """Remember the file path. Does not create a connection yet."""
        self._path = path

    def connect(self) -> SqliteConnection:
        """Open a session with foreign keys and WAL enabled.

        The caller closes it. Raises StorageError when the file cannot be
        opened. The driver message stays on the exception chain.
        """
        try:
            raw = sqlite3.connect(self._path)
        except sqlite3.Error as exc:
            raise StorageError() from exc
        # None disables the driver's own transaction, so BEGIN below is the
        # only one. Row access is by name, not by column position.
        raw.isolation_level = None
        raw.row_factory = sqlite3.Row
        try:
            raw.execute("PRAGMA foreign_keys = ON")
            raw.execute("PRAGMA journal_mode = WAL")
        except sqlite3.Error as exc:
            raw.close()
            raise StorageError() from exc
        return SqliteConnection(raw)

    def ensure_schema_migrations(self, connection: Connection) -> None:
        """Create the migrations table if this file does not have it yet.

        The statement matches the first statement of 0001_init.sqlite.sql.
        """
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id VARCHAR(32) NOT NULL PRIMARY KEY
            )
            """
        )


def _translate_sqlite_error(exc: sqlite3.Error) -> StorageError:
    """Turn a driver error into ConstraintViolation or StorageError.

    Unique violations are distinguishable so a service can raise Conflict.
    The returned message is generic. Does not execute SQL.
    """
    if isinstance(exc, sqlite3.IntegrityError) and "UNIQUE constraint failed" in str(exc):
        return ConstraintViolation()
    return StorageError()
