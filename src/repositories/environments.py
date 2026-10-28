"""Environment rows.

SQL for the environments table lives here. Functions do not commit and do
not check the principal.
"""

from domain.models.catalog import Environment
from repositories.db import Connection


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
