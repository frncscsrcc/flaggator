"""Migration runner against SQLite. MySQL files are present but not executed."""

import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from domain.errors import StorageError
from repositories.migrate import apply_migrations, default_migrations_directory, split_sql
from repositories.sqlite import SqliteDatabase


class MigrationTests(unittest.TestCase):
    def test_fresh_file_applies_once(self) -> None:
        """The first run records 0001. The second run does not insert it again."""
        with tempfile.TemporaryDirectory() as temporary:
            database = SqliteDatabase(str(Path(temporary) / "test.db"))
            directory = default_migrations_directory()
            apply_migrations(database, directory)
            apply_migrations(database, directory)
            connection = database.connect()
            try:
                rows = connection.execute(
                    "SELECT id FROM schema_migrations"
                ).fetchall()
                projects = connection.execute(
                    "SELECT `key` FROM projects"
                ).fetchall()
            finally:
                connection.close()
        self.assertEqual([row["id"] for row in rows], ["0001"])
        self.assertEqual(projects, [])

    def test_a_failing_script_is_not_recorded(self) -> None:
        """Startup stops on a bad file and leaves the id out of schema_migrations."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            migrations = root / "migrations"
            migrations.mkdir()
            (migrations / "0001_bad.sqlite.sql").write_text("THIS IS NOT SQL;\n", encoding="utf-8")
            (migrations / "0001_bad.mysql.sql").write_text("THIS IS NOT SQL;\n", encoding="utf-8")
            database = SqliteDatabase(str(root / "test.db"))
            with self.assertRaises(StorageError):
                apply_migrations(database, migrations)
            connection = database.connect()
            try:
                rows = connection.execute(
                    "SELECT id FROM schema_migrations"
                ).fetchall()
            finally:
                connection.close()
        self.assertEqual(rows, [])

    def test_split_sql_keeps_a_semicolon_inside_a_string(self) -> None:
        """A semicolon that is data does not end the statement."""
        script = "INSERT INTO projects (`key`, name) VALUES ('a;b', 'Shop');\n"
        self.assertEqual(
            split_sql(script),
            ["INSERT INTO projects (`key`, name) VALUES ('a;b', 'Shop')"],
        )
