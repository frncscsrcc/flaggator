"""The flags and targeting one environment evaluates.

This is a SQL read. It does not use the cache and does not commit.
"""

from dataclasses import dataclass

from domain.errors import StorageError
from repositories.db import Connection
from repositories.flag_configs import find_config
from repositories.flags import list_variations


@dataclass(frozen=True)
class BundleFlag:
    """One flag as evaluation needs it.

    variations are ordered. The JSON fields are the parsed targeting
    documents. key is the public flag key.
    """

    key: str
    variations: tuple[object, ...]
    enabled: bool
    off_variation: int
    fallthrough: object
    rules: object
    targets: object
    prerequisites: object
    version: int


def load_bundle_flags(
    connection: Connection,
    project_id: int,
    environment_id: int,
) -> list[BundleFlag]:
    """Return every flag in the project with its config in this environment.

    Flags are ordered by key. An empty project returns an empty list.
    Raises StorageError when a flag has no config in the environment.
    Does not commit and does not read the cache.
    """
    rows = connection.execute(
        """
        SELECT id, `key`
          FROM flags
         WHERE project_id = ?
         ORDER BY `key`
        """,
        (project_id,),
    ).fetchall()
    loaded: list[BundleFlag] = []
    for row in rows:
        flag_id = int(row["id"])
        config = find_config(connection, flag_id, environment_id)
        if config is None:
            raise StorageError()
        loaded.append(
            BundleFlag(
                key=str(row["key"]),
                variations=list_variations(connection, flag_id),
                enabled=config.enabled,
                off_variation=config.off_variation,
                fallthrough=config.fallthrough,
                rules=config.rules,
                targets=config.targets,
                prerequisites=config.prerequisites,
                version=config.version,
            )
        )
    return loaded
