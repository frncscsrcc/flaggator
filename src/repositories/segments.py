"""Segment rows.

SQL for the segments table lives here. Functions do not commit and do not
check the principal. subjects_json is parsed into a tuple before it leaves
this module.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from domain.errors import StorageError
from repositories.db import Connection
from repositories.json_text import dump_json_text, load_json_text


@dataclass(frozen=True)
class SegmentRow:
    """One segment, with the subject list already parsed.

    key and name match the columns. subjects is empty when the stored list
    is empty. The database id is not included.
    """

    key: str
    name: str
    subjects: tuple[str, ...]


def insert_segment(
    connection: Connection,
    environment_id: int,
    key: str,
    name: str,
    subjects: tuple[str, ...],
) -> None:
    """Insert one segment in environment_id.

    Raises ConstraintViolation when that environment already has key. Does
    not commit and does not check that environment_id exists.
    """
    connection.execute(
        """
        INSERT INTO segments (environment_id, `key`, name, subjects_json)
        VALUES (?, ?, ?, ?)
        """,
        (environment_id, key, name, _dump_subjects(subjects)),
    )


def find_segment(
    connection: Connection,
    environment_id: int,
    key: str,
) -> SegmentRow | None:
    """Load one segment by its public key inside an environment.

    Returns None when no row matches. Raises StorageError when the stored
    list is not JSON strings. Does not commit.
    """
    row = connection.execute(
        """
        SELECT `key`, name, subjects_json
          FROM segments
         WHERE environment_id = ? AND `key` = ?
        """,
        (environment_id, key),
    ).fetchone()
    if row is None:
        return None
    return _row(row)


def update_segment(
    connection: Connection,
    environment_id: int,
    key: str,
    name: str,
    subjects: tuple[str, ...],
) -> int:
    """Replace the name and subjects of one segment.

    Returns the number of rows changed. Zero means no segment has that key
    in the environment. Does not commit and does not change the key.
    """
    cursor = connection.execute(
        """
        UPDATE segments
           SET name = ?,
               subjects_json = ?
         WHERE environment_id = ?
           AND `key` = ?
        """,
        (name, _dump_subjects(subjects), environment_id, key),
    )
    return cursor.rowcount


def delete_segment(connection: Connection, environment_id: int, key: str) -> int:
    """Delete one segment.

    Returns the number of rows deleted. Zero means no segment has that key.
    Does not commit and does not look at flag configurations.
    """
    cursor = connection.execute(
        """
        DELETE FROM segments
         WHERE environment_id = ?
           AND `key` = ?
        """,
        (environment_id, key),
    )
    return cursor.rowcount


def list_segments(
    connection: Connection,
    environment_id: int,
    after_key: str | None,
    limit: int,
) -> list[SegmentRow]:
    """Return segments in environment_id ordered by key, at most limit rows.

    after_key excludes that key and every key before it. None starts at the
    first key. An environment with no segments returns an empty list. Does
    not commit.
    """
    if after_key is None:
        rows = connection.execute(
            """
            SELECT `key`, name, subjects_json
              FROM segments
             WHERE environment_id = ?
             ORDER BY `key`
             LIMIT ?
            """,
            (environment_id, limit),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT `key`, name, subjects_json
              FROM segments
             WHERE environment_id = ?
               AND `key` > ?
             ORDER BY `key`
             LIMIT ?
            """,
            (environment_id, after_key, limit),
        ).fetchall()
    return [_row(row) for row in rows]


def list_environment_segments(connection: Connection, environment_id: int) -> list[SegmentRow]:
    """Return every segment in environment_id, ordered by key.

    An environment with no segments returns an empty list. The evaluation
    bundle uses this list. Does not commit.
    """
    rows = connection.execute(
        """
        SELECT `key`, name, subjects_json
          FROM segments
         WHERE environment_id = ?
         ORDER BY `key`
        """,
        (environment_id,),
    ).fetchall()
    return [_row(row) for row in rows]


def list_segment_keys(connection: Connection, environment_id: int) -> list[str]:
    """Return every segment key in environment_id, ordered by key.

    An environment with no segments returns an empty list. Does not commit.
    """
    rows = connection.execute(
        """
        SELECT `key`
          FROM segments
         WHERE environment_id = ?
         ORDER BY `key`
        """,
        (environment_id,),
    ).fetchall()
    return [str(row["key"]) for row in rows]


def _dump_subjects(subjects: tuple[str, ...]) -> str:
    """Return the JSON text stored for a subject list.

    Order is the tuple order. Does not read the database.
    """
    return dump_json_text(list(subjects))


def _row(row: Mapping[str, object]) -> SegmentRow:
    """Copy one query row into a segment.

    Raises StorageError when subjects_json is not a list of strings. Does
    not commit.
    """
    return SegmentRow(
        key=str(row["key"]),
        name=str(row["name"]),
        subjects=_subjects(row["subjects_json"]),
    )


def _subjects(value: object) -> tuple[str, ...]:
    """Return the subject tuple stored in a JSON column.

    Raises StorageError when the column is not a JSON list of strings.
    Does not commit.
    """
    parsed = load_json_text(value)
    if not isinstance(parsed, list):
        raise StorageError()
    subjects: list[str] = []
    for item in parsed:
        if not isinstance(item, str):
            raise StorageError()
        subjects.append(item)
    return tuple(subjects)
