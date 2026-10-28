"""Flag configuration rows.

This module writes the disabled configuration a new environment needs. It
does not evaluate flags and does not commit.
"""

from repositories.db import Connection

# A variation serve is {"variation": <index>}. Version starts at 1 so the
# first targeting write sends the version a later read would return.
DISABLED_FALLTHROUGH_JSON = '{"variation":0}'
DISABLED_RULES_JSON = "[]"
DISABLED_TARGETS_JSON = "{}"
DISABLED_PREREQUISITES_JSON = "[]"
DISABLED_VERSION = 1


def list_flag_ids(connection: Connection, project_id: int) -> list[int]:
    """Return the flag ids in project_id, ordered by id.

    An empty project returns an empty list. Does not commit.
    """
    rows = connection.execute(
        """
        SELECT id
          FROM flags
         WHERE project_id = ?
         ORDER BY id
        """,
        (project_id,),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def insert_disabled_config(
    connection: Connection,
    flag_id: int,
    environment_id: int,
) -> None:
    """Insert a disabled configuration for one flag in one environment.

    The flag is off, the off variation is 0, and the fallthrough variation
    is 0. Rules, targets, and prerequisites are empty. Does not commit and
    does not copy another environment's targeting.
    """
    connection.execute(
        """
        INSERT INTO flag_configs (
            flag_id,
            environment_id,
            enabled,
            off_variation,
            fallthrough_json,
            rules_json,
            targets_json,
            prerequisites_json,
            version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            flag_id,
            environment_id,
            0,
            0,
            DISABLED_FALLTHROUGH_JSON,
            DISABLED_RULES_JSON,
            DISABLED_TARGETS_JSON,
            DISABLED_PREREQUISITES_JSON,
            DISABLED_VERSION,
        ),
    )
