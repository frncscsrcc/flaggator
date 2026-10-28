"""Flag rows and their ordered variations.

SQL for the flags and flag_variations tables lives here. Functions do not
commit and do not check the principal.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from repositories.db import Connection
from repositories.json_text import load_json_text


@dataclass(frozen=True)
class FlagRow:
    """A flag plus the id the next statement in this use case needs.

    The id is not an API field. variations are loaded by a separate query.
    """

    id: int
    key: str
    name: str
    description: str


def insert_flag(
    connection: Connection,
    project_id: int,
    key: str,
    name: str,
    description: str,
) -> int:
    """Insert one flag and return its new id.

    Raises ConstraintViolation when that project already has key. Does not
    commit, does not insert variations, and does not write an audit row.
    """
    cursor = connection.execute(
        """
        INSERT INTO flags (project_id, `key`, name, description)
        VALUES (?, ?, ?, ?)
        """,
        (project_id, key, name, description),
    )
    return cursor.last_insert_id


def update_flag_text(
    connection: Connection,
    flag_id: int,
    name: str,
    description: str,
) -> None:
    """Replace the name and description of one flag.

    Does not change the key, the variations, or any environment config.
    Does not commit.
    """
    connection.execute(
        """
        UPDATE flags
           SET name = ?,
               description = ?
         WHERE id = ?
        """,
        (name, description, flag_id),
    )


def find_flag(connection: Connection, project_id: int, key: str) -> FlagRow | None:
    """Load one flag by its public key inside a project.

    Returns None when no row matches. Does not commit.
    """
    row = connection.execute(
        """
        SELECT id, `key`, name, description
          FROM flags
         WHERE project_id = ? AND `key` = ?
        """,
        (project_id, key),
    ).fetchone()
    if row is None:
        return None
    return _flag_row(row)


def list_flags(
    connection: Connection,
    project_id: int,
    after_key: str | None,
    limit: int,
) -> list[FlagRow]:
    """Return flags in project_id ordered by key, at most limit rows.

    after_key excludes that key and every key before it. None starts at the
    first key. A project with no flags returns an empty list. Does not commit.
    """
    if after_key is None:
        rows = connection.execute(
            """
            SELECT id, `key`, name, description
              FROM flags
             WHERE project_id = ?
             ORDER BY `key`
             LIMIT ?
            """,
            (project_id, limit),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT id, `key`, name, description
              FROM flags
             WHERE project_id = ?
               AND `key` > ?
             ORDER BY `key`
             LIMIT ?
            """,
            (project_id, after_key, limit),
        ).fetchall()
    return [_flag_row(row) for row in rows]


def insert_variation(
    connection: Connection,
    flag_id: int,
    variation_index: int,
    value_json: str,
) -> None:
    """Insert one variation at variation_index.

    value_json is the JSON text of the value. Does not commit and does not
    change environment configs that already point at other indexes.
    """
    connection.execute(
        """
        INSERT INTO flag_variations (flag_id, variation_index, value_json)
        VALUES (?, ?, ?)
        """,
        (flag_id, variation_index, value_json),
    )


def list_variations(connection: Connection, flag_id: int) -> tuple[object, ...]:
    """Return the variation values for flag_id, ordered by index.

    A flag with no variation rows returns an empty tuple. Raises
    StorageError when a stored value is not JSON. Does not commit.
    """
    rows = connection.execute(
        """
        SELECT variation_index, value_json
          FROM flag_variations
         WHERE flag_id = ?
         ORDER BY variation_index
        """,
        (flag_id,),
    ).fetchall()
    return tuple(load_json_text(row["value_json"]) for row in rows)


def _flag_row(row: Mapping[str, object]) -> FlagRow:
    """Map one flags row into a FlagRow.

    Does not read another table.
    """
    return FlagRow(
        id=int(row["id"]),
        key=str(row["key"]),
        name=str(row["name"]),
        description=str(row["description"]),
    )
