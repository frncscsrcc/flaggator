"""MySQL sessions.

This module is the only one that imports PyMySQL, and it does so when a
session is opened. Importing the module does not require the extra to be
installed, so the placeholder rewriter can be tested without a server.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING

from domain.errors import ConstraintViolation, StorageError
from repositories.db import Connection, Transactional

if TYPE_CHECKING:
    import pymysql


class MysqlCursor:
    """A PyMySQL cursor whose rows are dictionaries."""

    def __init__(self, raw: pymysql.cursors.DictCursor) -> None:
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
        """The integer id MySQL assigned to the last insert on this cursor."""
        return int(self._raw.lastrowid)

    @property
    def rowcount(self) -> int:
        """How many rows the statement changed, as MySQL reports it."""
        return int(self._raw.rowcount)


class MysqlConnection(Transactional):
    """One MySQL session.

    Statements written with `?` are rewritten to `%s` before the driver sees
    them. One request owns this object and does not share it.
    """

    def __init__(self, raw: pymysql.Connection) -> None:
        """Adopt an open PyMySQL connection. autocommit is already off."""
        super().__init__()
        self._raw = raw

    def execute(self, sql: str, params: tuple = ()) -> MysqlCursor:
        """Run one statement.

        Rewrites placeholders, then binds params. Duplicate-key error 1062
        becomes ConstraintViolation. Other driver errors become StorageError.
        """
        pymysql_module = _import_pymysql()
        cursor = self._raw.cursor()
        try:
            cursor.execute(rewrite_placeholders(sql), params)
        except pymysql_module.err.IntegrityError as exc:
            raise _translate_mysql_integrity(exc) from exc
        except pymysql_module.err.Error as exc:
            raise StorageError() from exc
        return MysqlCursor(cursor)

    def executemany(self, sql: str, params: Sequence[tuple]) -> MysqlCursor:
        """Run one statement for each parameter tuple.

        The placeholder rewrite and the error translation match execute.
        """
        pymysql_module = _import_pymysql()
        cursor = self._raw.cursor()
        try:
            cursor.executemany(rewrite_placeholders(sql), list(params))
        except pymysql_module.err.IntegrityError as exc:
            raise _translate_mysql_integrity(exc) from exc
        except pymysql_module.err.Error as exc:
            raise StorageError() from exc
        return MysqlCursor(cursor)

    def close(self) -> None:
        """Close the session. Does not commit."""
        self._raw.close()

    def _begin(self) -> None:
        """Start a transaction. MySQL DDL will still implicit-commit later."""
        pymysql_module = _import_pymysql()
        try:
            self._raw.begin()
        except pymysql_module.err.Error as exc:
            raise StorageError() from exc

    def _commit(self) -> None:
        """Commit the open transaction."""
        pymysql_module = _import_pymysql()
        try:
            self._raw.commit()
        except pymysql_module.err.Error as exc:
            raise StorageError() from exc

    def _rollback(self) -> None:
        """Roll back the open transaction."""
        pymysql_module = _import_pymysql()
        try:
            self._raw.rollback()
        except pymysql_module.err.Error as exc:
            raise StorageError() from exc


class MysqlDatabase:
    """Opens MySQL sessions.

    engine is "mysql". Migration DDL is not wrapped in one transaction,
    because MySQL commits each CREATE TABLE on its own.
    """

    engine = "mysql"
    transactional_ddl = False

    def __init__(self, host: str, port: int, database: str, user: str, password: str) -> None:
        """Remember the coordinates. Does not connect yet.

        password is kept for connect(). Do not log this object.
        """
        self._host = host
        self._port = port
        self._database = database
        self._user = user
        self._password = password

    def __repr__(self) -> str:
        """Describe the target without the password."""
        return (
            f"MysqlDatabase(host={self._host!r}, port={self._port!r}, "
            f"database={self._database!r}, user={self._user!r})"
        )

    def connect(self) -> MysqlConnection:
        """Open a utf8mb4 session with autocommit off.

        Raises StorageError when PyMySQL is not installed or the server
        rejects the connection. The driver message stays on the chain.
        """
        pymysql_module = _import_pymysql()
        try:
            raw = pymysql_module.connect(
                host=self._host,
                port=self._port,
                user=self._user,
                password=self._password,
                database=self._database,
                charset="utf8mb4",
                autocommit=False,
                cursorclass=pymysql_module.cursors.DictCursor,
            )
        except pymysql_module.err.Error as exc:
            raise StorageError() from exc
        return MysqlConnection(raw)

    def ensure_schema_migrations(self, connection: Connection) -> None:
        """Create the migrations table if this schema does not have it yet.

        The statement matches the first statement of 0001_init.mysql.sql.
        InnoDB and utf8mb4 are set here because the runner may create the
        table before that file runs.
        """
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                id VARCHAR(32) NOT NULL PRIMARY KEY
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
            """
        )


def rewrite_placeholders(sql: str) -> str:
    """Rewrite `?` placeholders into the form PyMySQL binds.

    Outside quotes and comments, `?` becomes `%s` and a literal `%` becomes
    `%%`. PyMySQL formats the statement with Python's percent operator, so an
    unescaped `%` would be read as a format marker. Characters inside quotes
    or comments are copied unchanged, which is where a `?` is data rather
    than a placeholder. Does not execute SQL and does not take parameters.
    """
    parts: list[str] = []
    index = 0
    length = len(sql)
    while index < length:
        if sql.startswith("--", index):
            line_end = sql.find("\n", index)
            if line_end == -1:
                parts.append(sql[index:])
                break
            parts.append(sql[index:line_end])
            index = line_end
            continue
        if sql.startswith("/*", index):
            comment_end = sql.find("*/", index + 2)
            if comment_end == -1:
                parts.append(sql[index:])
                break
            parts.append(sql[index : comment_end + 2])
            index = comment_end + 2
            continue
        character = sql[index]
        if character == "'" or character == '"':
            quote_end = _end_of_quote(sql, index, character)
            parts.append(sql[index:quote_end])
            index = quote_end
            continue
        if character == "?":
            parts.append("%s")
            index += 1
            continue
        if character == "%":
            parts.append("%%")
            index += 1
            continue
        parts.append(character)
        index += 1
    return "".join(parts)


def _end_of_quote(sql: str, start: int, quote: str) -> int:
    """Return the index just after the closing quote that begins at start.

    A doubled single quote is an escaped quote and does not close the
    string. An unclosed quote runs to the end of sql. Does not execute SQL.
    """
    index = start + 1
    length = len(sql)
    while index < length:
        if sql[index] == quote:
            if quote == "'" and index + 1 < length and sql[index + 1] == "'":
                index += 2
                continue
            return index + 1
        index += 1
    return length


def _import_pymysql() -> pymysql:
    """Import PyMySQL, or raise StorageError when the extra is missing.

    The message does not include a filesystem path. The import is local so
    that loading this module does not require the package.
    """
    try:
        import pymysql as pymysql_module
    except ImportError as exc:
        raise StorageError("MySQL support requires the optional PyMySQL extra.") from exc
    return pymysql_module


def _translate_mysql_integrity(exc: Exception) -> StorageError:
    """Turn a MySQL integrity error into ConstraintViolation or StorageError.

    Error 1062 is a duplicate key. Other integrity failures stay generic
    storage errors. Does not copy the driver message.
    """
    code = exc.args[0] if exc.args else None
    if code == 1062:
        return ConstraintViolation()
    return StorageError()
