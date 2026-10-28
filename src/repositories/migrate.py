"""Apply numbered SQL migration files.

The files live in db/migrations. Each id has a SQLite script and a MySQL
script. The runner records an id only after that engine's script succeeds.
"""

from dataclasses import dataclass
from pathlib import Path

from domain.errors import StorageError
from repositories.db import Connection, Database


@dataclass(frozen=True)
class MigrationPair:
    """The two scripts for one migration id.

    migration_id is the numeric prefix, including leading zeros, so it
    matches the filename. The paths point at the SQL files. This object
    does not execute them.
    """

    migration_id: str
    sqlite_path: Path
    mysql_path: Path


def default_migrations_directory() -> Path:
    """Find db/migrations by walking up from this file.

    Returns the directory. Raises StorageError when it cannot be found,
    without putting a searched path in the message.
    """
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "db" / "migrations"
        if candidate.is_dir():
            return candidate
    raise StorageError("The migrations directory was not found.")


def apply_migrations(database: Database, directory: Path) -> None:
    """Apply every migration id this database has not recorded.

    Opens one session and closes it. SQLite runs each file inside a
    transaction. MySQL does not, because CREATE TABLE commits on its own;
    the id is inserted only after the file's statements succeed. Raises
    StorageError and stops on the first failure.
    """
    pairs = list_migrations(directory)
    connection = database.connect()
    try:
        database.ensure_schema_migrations(connection)
        applied = _applied_ids(connection)
        for pair in pairs:
            if pair.migration_id in applied:
                continue
            script_path = _script_for_engine(pair, database.engine)
            if database.transactional_ddl:
                with connection.transaction():
                    _run_script(connection, script_path)
                    _record(connection, pair.migration_id)
            else:
                _run_script(connection, script_path)
                with connection.transaction():
                    _record(connection, pair.migration_id)
    finally:
        connection.close()


def list_migrations(directory: Path) -> list[MigrationPair]:
    """Return migration pairs in numeric order.

    An id with only one engine file raises StorageError. Does not execute
    SQL. An empty directory returns an empty list.
    """
    if not directory.is_dir():
        raise StorageError("The migrations directory was not found.")
    sqlite_by_id: dict[str, Path] = {}
    mysql_by_id: dict[str, Path] = {}
    for path in directory.iterdir():
        if not path.is_file():
            continue
        parsed = _parse_migration_name(path.name)
        if parsed is None:
            continue
        migration_id, engine = parsed
        if engine == "sqlite":
            sqlite_by_id[migration_id] = path
        else:
            mysql_by_id[migration_id] = path
    ids = sorted(set(sqlite_by_id) | set(mysql_by_id), key=int)
    pairs: list[MigrationPair] = []
    for migration_id in ids:
        sqlite_path = sqlite_by_id.get(migration_id)
        mysql_path = mysql_by_id.get(migration_id)
        if sqlite_path is None or mysql_path is None:
            raise StorageError(
                f"Migration {migration_id} does not have both engine files."
            )
        pairs.append(
            MigrationPair(
                migration_id=migration_id,
                sqlite_path=sqlite_path,
                mysql_path=mysql_path,
            )
        )
    return pairs


def split_sql(script: str) -> list[str]:
    """Split a script on semicolons that are outside quotes and comments.

    Returns stripped statements. A semicolon inside a string stays in the
    statement. An empty script returns an empty list. Does not execute SQL.
    """
    statements: list[str] = []
    current: list[str] = []
    index = 0
    length = len(script)
    while index < length:
        if script.startswith("--", index):
            line_end = script.find("\n", index)
            if line_end == -1:
                current.append(script[index:])
                break
            current.append(script[index:line_end])
            index = line_end
            continue
        if script.startswith("/*", index):
            comment_end = script.find("*/", index + 2)
            if comment_end == -1:
                current.append(script[index:])
                break
            current.append(script[index : comment_end + 2])
            index = comment_end + 2
            continue
        character = script[index]
        if character == "'" or character == '"':
            quote_end = _end_of_quote(script, index, character)
            current.append(script[index:quote_end])
            index = quote_end
            continue
        if character == ";":
            statement = "".join(current).strip()
            if statement:
                statements.append(statement)
            current = []
            index += 1
            continue
        current.append(character)
        index += 1
    tail = "".join(current).strip()
    if tail:
        statements.append(tail)
    return statements


def _end_of_quote(script: str, start: int, quote: str) -> int:
    """Return the index just after the closing quote that begins at start.

    A doubled single quote does not close the string. An unclosed quote
    runs to the end of the script.
    """
    index = start + 1
    length = len(script)
    while index < length:
        if script[index] == quote:
            if quote == "'" and index + 1 < length and script[index + 1] == "'":
                index += 2
                continue
            return index + 1
        index += 1
    return length


def _parse_migration_name(name: str) -> tuple[str, str] | None:
    """Return (id, engine) for a migration filename, or None when it is not one.

    The id keeps its leading zeros. engine is "sqlite" or "mysql".
    """
    suffix_sqlite = ".sqlite.sql"
    suffix_mysql = ".mysql.sql"
    if name.endswith(suffix_sqlite):
        engine = "sqlite"
        stem = name[: -len(suffix_sqlite)]
    elif name.endswith(suffix_mysql):
        engine = "mysql"
        stem = name[: -len(suffix_mysql)]
    else:
        return None
    prefix, separator, _rest = stem.partition("_")
    if separator != "_" or not prefix.isdigit():
        return None
    return prefix, engine


def _script_for_engine(pair: MigrationPair, engine: str) -> Path:
    """Pick the SQL file for engine.

    Raises StorageError for an engine this runner does not know. The message
    does not include a path.
    """
    if engine == "sqlite":
        return pair.sqlite_path
    if engine == "mysql":
        return pair.mysql_path
    raise StorageError("The database engine is not supported.")


def _applied_ids(connection: Connection) -> set[str]:
    """Read the migration ids already stored.

    Returns a set, empty when no migration has been recorded.
    """
    rows = connection.execute("SELECT id FROM schema_migrations").fetchall()
    return {str(row["id"]) for row in rows}


def _record(connection: Connection, migration_id: str) -> None:
    """Insert migration_id into schema_migrations.

    The caller holds the transaction when the engine uses one. Does not commit.
    """
    connection.execute(
        "INSERT INTO schema_migrations (id) VALUES (?)",
        (migration_id,),
    )


def _run_script(connection: Connection, path: Path) -> None:
    """Execute each statement in path.

    Does not commit. A driver failure becomes StorageError from the
    connection and stops the script.
    """
    script = path.read_text(encoding="utf-8")
    for statement in split_sql(script):
        connection.execute(statement)
