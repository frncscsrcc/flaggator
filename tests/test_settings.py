"""Composition root: settings, engine choice, and prepare()."""

import sys
import tempfile
import unittest
from pathlib import Path

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from composition import build_database, load_settings, prepare
from domain.errors import ValidationError
from repositories.migrate import default_migrations_directory
from repositories.mysql import MysqlDatabase


class SettingsTests(unittest.TestCase):
    def test_defaults_are_sqlite_on_loopback(self) -> None:
        """An empty environment selects SQLite, the memory cache, and 127.0.0.1.

        The password is empty and is not part of repr.
        """
        settings = load_settings({})
        self.assertEqual(settings.engine, "sqlite")
        self.assertEqual(settings.sqlite_path, "flaggator.db")
        self.assertEqual(settings.mysql_database, "flaggator")
        self.assertEqual(settings.cache, "memory")
        self.assertEqual(settings.bind_host, "127.0.0.1")
        self.assertEqual(settings.bind_port, 8080)
        self.assertEqual(settings.mysql_password, "")
        self.assertNotIn("mysql_password", repr(settings))

    def test_repr_hides_the_mysql_password(self) -> None:
        """A logged Settings value must not contain the database password."""
        settings = load_settings({"FLAGGATOR_MYSQL_PASSWORD": "secret-password"})
        self.assertNotIn("secret-password", repr(settings))

    def test_bad_port_is_a_validation_error(self) -> None:
        """A non-numeric bind port fails before a socket is opened."""
        with self.assertRaises(ValidationError) as caught:
            load_settings({"FLAGGATOR_BIND_PORT": "nope"})
        self.assertEqual(caught.exception.details[0]["field"], "bind_port")

    def test_mysql_database_is_built_without_connecting(self) -> None:
        """Selecting MySQL does not open a socket during construction."""
        settings = load_settings(
            {
                "FLAGGATOR_ENGINE": "mysql",
                "FLAGGATOR_MYSQL_PASSWORD": "secret-password",
            }
        )
        database = build_database(settings)
        self.assertIsInstance(database, MysqlDatabase)
        self.assertNotIn("secret-password", repr(database))

    def test_prepare_migrates_a_fresh_sqlite_file(self) -> None:
        """prepare records migration 0001 and builds the null cache.

        Nothing listens. Health is covered by the HTTP tests.
        """
        with tempfile.TemporaryDirectory() as temporary:
            settings = load_settings(
                {
                    "FLAGGATOR_SQLITE_PATH": str(Path(temporary) / "test.db"),
                    "FLAGGATOR_CACHE": "null",
                }
            )
            # load_settings points at the real migrations directory.
            self.assertEqual(settings.migrations_directory, default_migrations_directory())
            runtime = prepare(settings)
            connection = runtime.database.connect()
            try:
                rows = connection.execute("SELECT id FROM schema_migrations").fetchall()
            finally:
                connection.close()
        self.assertEqual([row["id"] for row in rows], ["0001"])
        self.assertEqual(runtime.cache.get("anything"), None)
