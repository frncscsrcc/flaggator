"""SQLite connection behavior that does not depend on a MySQL server."""

import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from domain.errors import ConstraintViolation, StorageError
from repositories.migrate import apply_migrations, default_migrations_directory
from repositories.sqlite import SqliteDatabase


class SqliteConnectionTests(unittest.TestCase):
    def setUp(self) -> None:
        """Create a migrated database in a temporary directory.

        The directory is removed in tearDown. Each test gets its own file.
        """
        self._temporary = tempfile.TemporaryDirectory()
        self.database = SqliteDatabase(str(Path(self._temporary.name) / "test.db"))
        apply_migrations(self.database, default_migrations_directory())

    def tearDown(self) -> None:
        """Delete the temporary database file."""
        self._temporary.cleanup()

    def test_rows_are_addressed_by_column_name(self) -> None:
        """A select returns a mapping, and the inserted id is on the cursor."""
        connection = self.database.connect()
        try:
            with connection.transaction():
                cursor = connection.execute(
                    "INSERT INTO projects (`key`, name) VALUES (?, ?)",
                    ("shop", "Shop"),
                )
                inserted_id = cursor.last_insert_id
            row = connection.execute(
                "SELECT `key`, name FROM projects WHERE `key` = ?",
                ("shop",),
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row["key"], "shop")
        self.assertEqual(row["name"], "Shop")
        self.assertGreater(inserted_id, 0)

    def test_unique_violation_is_a_constraint_violation(self) -> None:
        """A second project with the same key raises ConstraintViolation.

        The driver error is not the exception the caller sees.
        """
        connection = self.database.connect()
        try:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO projects (`key`, name) VALUES (?, ?)",
                    ("shop", "Shop"),
                )
            with self.assertRaises(ConstraintViolation) as caught:
                with connection.transaction():
                    connection.execute(
                        "INSERT INTO projects (`key`, name) VALUES (?, ?)",
                        ("shop", "Other"),
                    )
        finally:
            connection.close()
        self.assertEqual(caught.exception.message, "The database failed to complete the request.")
        self.assertNotIn("UNIQUE", caught.exception.message)

    def test_foreign_key_failure_is_storage_error(self) -> None:
        """A dangling environment is not reported as a unique violation."""
        connection = self.database.connect()
        try:
            with self.assertRaises(StorageError) as caught:
                with connection.transaction():
                    connection.execute(
                        """
                        INSERT INTO environments (project_id, `key`, name)
                        VALUES (?, ?, ?)
                        """,
                        (999, "prod", "Production"),
                    )
            self.assertNotIsInstance(caught.exception, ConstraintViolation)
        finally:
            connection.close()

    def test_on_commit_runs_after_commit_and_not_after_rollback(self) -> None:
        """Callbacks see a committed write and are dropped when it rolls back."""
        connection = self.database.connect()
        ran: list[str] = []

        def remember_commit() -> None:
            """Record that the commit callback ran. Does not touch the database."""
            ran.append("committed")

        try:
            with connection.transaction():
                connection.execute(
                    "INSERT INTO projects (`key`, name) VALUES (?, ?)",
                    ("shop", "Shop"),
                )
                connection.on_commit(remember_commit)
            self.assertEqual(ran, ["committed"])
            with self.assertRaises(ConstraintViolation):
                with connection.transaction():
                    connection.on_commit(remember_commit)
                    connection.execute(
                        "INSERT INTO projects (`key`, name) VALUES (?, ?)",
                        ("shop", "Other"),
                    )
        finally:
            connection.close()
        self.assertEqual(ran, ["committed"])

    def test_on_commit_outside_a_transaction_fails(self) -> None:
        """A callback registered with nothing to commit is a storage error."""
        connection = self.database.connect()

        def do_nothing() -> None:
            """A callback that must not run. Does not touch the database."""
            return None

        try:
            with self.assertRaises(StorageError):
                connection.on_commit(do_nothing)
        finally:
            connection.close()
