"""The connection surface repository functions call.

SQLite and MySQL each implement Database and Connection. Transaction
bookkeeping lives here so both engines commit, roll back, and run on_commit
callbacks the same way.
"""

from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Protocol

from domain.errors import StorageError


class Cursor(Protocol):
    """Rows from one statement, addressed by column name."""

    def fetchone(self) -> Mapping[str, object] | None:
        """Return the next row, or None when the result is exhausted."""

    def fetchall(self) -> list[Mapping[str, object]]:
        """Return the remaining rows. An empty result is an empty list."""

    @property
    def last_insert_id(self) -> int:
        """The integer id of the row this cursor just inserted."""

    @property
    def rowcount(self) -> int:
        """How many rows the statement changed.

        A versioned update uses zero to mean the caller's version lost.
        """


class Connection(Protocol):
    """One database session.

    Not shared across requests. Repository functions execute SQL here and
    do not commit.
    """

    def execute(self, sql: str, params: tuple = ()) -> Cursor:
        """Run one statement with bound parameters.

        Returns a cursor of named rows. Raises ConstraintViolation when a
        unique constraint fails, and StorageError for other driver failures.
        """

    def executemany(self, sql: str, params: Sequence[tuple]) -> Cursor:
        """Run one statement for each parameter tuple.

        Binding rules match execute. An empty sequence still prepares the
        statement and changes no rows.
        """

    def transaction(self) -> Iterator[None]:
        """Commit if the block returns, roll back if it raises.

        A nested call joins the open transaction and does not commit it.
        """

    def on_commit(self, callback: Callable[[], None]) -> None:
        """Run callback after the outermost transaction commits.

        The callback is dropped when that transaction rolls back. Raises
        StorageError when no transaction is open.
        """

    def close(self) -> None:
        """Release the session.

        Does not commit. An uncommitted transaction is rolled back by the
        driver.
        """


class Database(Protocol):
    """Opens sessions and describes how this engine treats DDL."""

    engine: str
    transactional_ddl: bool

    def connect(self) -> Connection:
        """Open a session. The caller closes it."""

    def ensure_schema_migrations(self, connection: Connection) -> None:
        """Create schema_migrations if it is not there yet.

        The migration files create the same table. This exists so the runner
        can read applied ids before the first file runs.
        """


class Transactional:
    """Commit, rollback, and on_commit for a connection that implements _begin.

    Subclasses run statements on their own driver. This class does not import
    sqlite3 or PyMySQL.
    """

    def __init__(self) -> None:
        """Start with no transaction and no callbacks."""
        self._depth = 0
        self._commit_callbacks: list[Callable[[], None]] = []

    def on_commit(self, callback: Callable[[], None]) -> None:
        """Register callback for the commit of the current transaction.

        Raises StorageError outside a transaction, because the callback
        would otherwise never run. Does not run the callback now.
        """
        if self._depth == 0:
            raise StorageError("on_commit requires an open transaction.")
        self._commit_callbacks.append(callback)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Commit on success. Roll back and drop callbacks on any exception.

        Nested use joins the open transaction. Only the outermost call
        commits, rolls back, and runs callbacks. Callbacks run after commit,
        so a rollback cannot leave them believing the write was stored.
        """
        outermost = self._depth == 0
        if outermost:
            self._begin()
        self._depth += 1
        committed = False
        try:
            yield
            if outermost:
                self._commit()
                committed = True
        except BaseException:
            if outermost:
                self._rollback()
                self._commit_callbacks.clear()
            raise
        finally:
            self._depth -= 1
        if committed:
            callbacks = self._commit_callbacks
            self._commit_callbacks = []
            for callback in callbacks:
                callback()

    def _begin(self) -> None:
        """Start the driver transaction. Subclasses issue the SQL."""
        raise NotImplementedError

    def _commit(self) -> None:
        """Commit the driver transaction. Subclasses issue the SQL."""
        raise NotImplementedError

    def _rollback(self) -> None:
        """Roll back the driver transaction. Subclasses issue the SQL."""
        raise NotImplementedError
