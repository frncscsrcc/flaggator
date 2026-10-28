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
) -> list[Project]:
    """Return projects ordered by key, at most limit rows.

    after_key excludes that key and every key before it. None starts at the
    first key. An empty table returns an empty list. limit is the caller's
    row cap, including the extra row a service uses to see a further page.
    Does not commit.
    """
    if after_key is None:
        rows = connection.execute(
            """
            SELECT `key`, name
              FROM projects
             ORDER BY `key`
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    else:
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
    return [Project(key=str(row["key"]), name=str(row["name"])) for row in rows]
