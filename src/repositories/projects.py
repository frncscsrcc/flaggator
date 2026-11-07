"""Project rows.

SQL for the projects table lives here. Functions do not commit and do not
check the principal.
"""

from dataclasses import dataclass

from domain.models.catalog import Project
from repositories.db import Connection


@dataclass(frozen=True)
class ProjectRow:
    """A project plus the id the next statement in this use case needs.

    The id is not an API field. key and name match the projects columns.
    """

    id: int
    key: str
    name: str


def insert_project(connection: Connection, key: str, name: str) -> int:
    """Insert one project and return its new id.

    Raises ConstraintViolation when key already exists. Does not commit and
    does not write an audit row.
    """
    cursor = connection.execute(
        """
        INSERT INTO projects (`key`, name)
        VALUES (?, ?)
        """,
        (key, name),
    )
    return cursor.last_insert_id


def find_project(connection: Connection, key: str) -> ProjectRow | None:
    """Load one project by its public key.

    Returns None when no row matches. Does not commit.
    """
    # `key` is reserved in MySQL. Backticks keep this statement valid there,
    # and SQLite accepts the same quotes.
    row = connection.execute(
        """
        SELECT id, `key`, name
          FROM projects
         WHERE `key` = ?
        """,
        (key,),
    ).fetchone()
    if row is None:
        return None
    return ProjectRow(id=int(row["id"]), key=str(row["key"]), name=str(row["name"]))


def list_projects(
    connection: Connection,
    after_key: str | None,
    limit: int,
    search: str | None,
) -> list[Project]:
    """Return projects ordered by key, at most limit rows.

    after_key excludes that key and every key before it. None starts at the
    first key. search None returns every row in that window. A string keeps
    rows whose name or key contains it. An empty table returns an empty
    list. limit is the caller's row cap, including the extra row a service
    uses to see a further page. Does not commit.
    """
    pattern = None if search is None else _like_contains(search)
    if pattern is None and after_key is None:
        rows = connection.execute(
            """
            SELECT `key`, name
              FROM projects
             ORDER BY `key`
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    elif pattern is None:
        rows = connection.execute(
            """
            SELECT `key`, name
              FROM projects
             WHERE `key` > ?
             ORDER BY `key`
             LIMIT ?
            """,
            (after_key, limit),
        ).fetchall()
    elif after_key is None:
        rows = connection.execute(
            """
            SELECT `key`, name
              FROM projects
             WHERE `key` LIKE ? ESCAPE '!' OR name LIKE ? ESCAPE '!'
             ORDER BY `key`
             LIMIT ?
            """,
            (pattern, pattern, limit),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT `key`, name
              FROM projects
             WHERE `key` > ?
               AND (`key` LIKE ? ESCAPE '!' OR name LIKE ? ESCAPE '!')
             ORDER BY `key`
             LIMIT ?
            """,
            (after_key, pattern, pattern, limit),
        ).fetchall()
    return [Project(key=str(row["key"]), name=str(row["name"])) for row in rows]


def _like_contains(value: str) -> str:
    """Return a LIKE pattern that matches value as a substring.

    ! is the escape character on both engines. % and _ in value match those
    characters rather than wildcards.
    """
    escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return "%" + escaped + "%"
