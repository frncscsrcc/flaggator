"""Flag configuration rows.

Creating a flag or an environment writes a disabled configuration here.
Reads return the stored documents. This module does not evaluate flags
and does not commit.
"""

from collections.abc import Mapping
from dataclasses import dataclass

from repositories.db import Connection
from repositories.json_text import load_json_text

# A variation serve is {"variation": <index>}. Version starts at 1 so the
# first targeting write sends the version a later read would return.
DISABLED_FALLTHROUGH_JSON = '{"variation":0}'
DISABLED_RULES_JSON = "[]"
DISABLED_TARGETS_JSON = "{}"
DISABLED_PREREQUISITES_JSON = "[]"
DISABLED_VERSION = 1


@dataclass(frozen=True)
class FlagConfigRow:
    """One stored configuration, with JSON columns already parsed.

    environment_key is the public key. enabled is true only when the column
    is 1. Does not include the flag id.
    """

    environment_key: str
    enabled: bool
    off_variation: int
    fallthrough: object
    rules: object
    targets: object
    prerequisites: object
    version: int


@dataclass(frozen=True)
class FlagConfigSummaryRow:
    """The list columns of one configuration.

    The rule tree is not loaded. environment_key is the public key.
    """

    environment_key: str
    enabled: bool
    off_variation: int
    version: int


def list_configs(connection: Connection, flag_id: int) -> list[FlagConfigRow]:
    """Return every environment configuration for flag_id, ordered by environment key.

    A flag with no configs returns an empty list. Raises StorageError when
    a JSON column is not valid JSON. Does not commit.
    """
    rows = connection.execute(
        """
        SELECT environments.`key` AS environment_key,
               flag_configs.enabled,
               flag_configs.off_variation,
               flag_configs.fallthrough_json,
               flag_configs.rules_json,
               flag_configs.targets_json,
               flag_configs.prerequisites_json,
               flag_configs.version
          FROM flag_configs
          JOIN environments ON environments.id = flag_configs.environment_id
         WHERE flag_configs.flag_id = ?
         ORDER BY environments.`key`
        """,
        (flag_id,),
    ).fetchall()
    return [_config_row(row) for row in rows]


def find_config_summary(
    connection: Connection,
    flag_id: int,
    environment_id: int,
) -> FlagConfigSummaryRow | None:
    """Return the list summary for one flag in one environment.

    Returns None when that pair has no config row. Does not commit and does
    not load the rule tree.
    """
    row = connection.execute(
        """
        SELECT environments.`key` AS environment_key,
               flag_configs.enabled,
               flag_configs.off_variation,
               flag_configs.version
          FROM flag_configs
          JOIN environments ON environments.id = flag_configs.environment_id
         WHERE flag_configs.flag_id = ?
           AND flag_configs.environment_id = ?
        """,
        (flag_id, environment_id),
    ).fetchone()
    if row is None:
        return None
    return FlagConfigSummaryRow(
        environment_key=str(row["environment_key"]),
        enabled=int(row["enabled"]) == 1,
        off_variation=int(row["off_variation"]),
        version=int(row["version"]),
    )


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


def _config_row(row: Mapping[str, object]) -> FlagConfigRow:
    """Map one joined config row into a FlagConfigRow.

    Raises StorageError when a JSON column is not valid JSON.
    """
    return FlagConfigRow(
        environment_key=str(row["environment_key"]),
        enabled=int(row["enabled"]) == 1,
        off_variation=int(row["off_variation"]),
        fallthrough=load_json_text(row["fallthrough_json"]),
        rules=load_json_text(row["rules_json"]),
        targets=load_json_text(row["targets_json"]),
        prerequisites=load_json_text(row["prerequisites_json"]),
        version=int(row["version"]),
    )
