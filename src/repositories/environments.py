"""Environment rows.

SQL for the environments table lives here. Functions do not commit and do
not check the principal.
"""

from dataclasses import dataclass

from domain.models.catalog import Environment
from repositories.db import Connection


@dataclass(frozen=True)
class EnvironmentRow:
    """An environment plus the id the next statement in this use case needs.

    The id is not an API field. key and name match the environments columns.
    """

    id: int
    key: str
    name: str


def insert_environment(
    connection: Connection,
    project_id: int,
    key: str,
    name: str,
) -> int:
    """Insert one environment in project_id and return its new id.

    Raises ConstraintViolation when that project already has key. Does not
    commit, does not copy another environment, and does not write configs.
    """
    cursor = connection.execute(
        """
        INSERT INTO environments (project_id, `key`, name)
        VALUES (?, ?, ?)
        """,
        (project_id, key, name),
    )
    return cursor.last_insert_id


def find_environment(
    connection: Connection,
    project_id: int,
    key: str,
) -> EnvironmentRow | None:
    """Load one environment by its public key inside a project.

    Returns None when no row matches. Does not commit.
    """
    row = connection.execute(
        """
        SELECT id, `key`, name
          FROM environments
         WHERE project_id = ? AND `key` = ?
        """,
        (project_id, key),
    ).fetchone()
    if row is None:
        return None
    return EnvironmentRow(id=int(row["id"]), key=str(row["key"]), name=str(row["name"]))


def list_environment_ids(connection: Connection, project_id: int) -> list[int]:
    """Return the environment ids in project_id, ordered by id.

    An empty project returns an empty list. Does not commit.
    """
    rows = connection.execute(
        """
        SELECT id
          FROM environments
         WHERE project_id = ?
         ORDER BY id
        """,
        (project_id,),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def list_environments(
    connection: Connection,
    project_id: int,
    after_key: str | None,
    limit: int,
) -> list[Environment]:
    """Return environments in project_id ordered by key, at most limit rows.

    after_key excludes that key and every key before it. None starts at the
    first key. A project with no environments returns an empty list. Does
    not commit and does not check that project_id exists.
    """
    if after_key is None:
        rows = connection.execute(
            """
            SELECT `key`, name
              FROM environments
             WHERE project_id = ?
             ORDER BY `key`
             LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT `key`, name
              FROM environments
             WHERE project_id = ?
               AND `key` > ?
             ORDER BY `key`
             LIMIT ?
            """,
            (project_id, after_key, limit),
        ).fetchall()
    return [Environment(key=str(row["key"]), name=str(row["name"])) for row in rows]
