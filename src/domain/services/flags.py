"""Create, fetch, list, and patch flags.

A new flag starts disabled in every environment the project already has.
Patch may append variations. It does not retarget those environments.
"""

import json

from domain.errors import Conflict, ConstraintViolation, NotFound, StorageError, ValidationError
from domain.models.flag import (
    CreateFlag,
    Flag,
    FlagConfig,
    FlagConfigSummary,
    FlagPage,
    FlagSummary,
    PatchFlag,
)
from domain.models.catalog import KeysetQuery
from domain.models.principal import Principal
from domain.services.access import require_management
from domain.services.audit import record_change
from domain.validation import (
    is_public_key,
    require_description,
    require_display_name,
    require_page_limit,
    require_public_key,
    require_variation_count,
    variation_text,
)
from repositories.db import Connection
from repositories.environments import find_environment, list_environment_ids
from repositories.flags import (
    FlagRow,
    find_flag,
    insert_flag,
    insert_variation,
    list_flags,
    list_variations,
    update_flag_text,
)
from repositories.flag_configs import (
    FlagConfigRow,
    find_config_summary,
    insert_disabled_config,
    list_configs,
)
from repositories.projects import ProjectRow, find_project

_CREATE_MINIMUM = 2


def create_flag(
    connection: Connection,
    principal: Principal,
    project_key: str,
    command: CreateFlag,
) -> Flag:
    """Insert a flag, its variations, a disabled config per environment, and an audit row.

    Raises Forbidden for an SDK key, NotFound when the project is absent,
    ValidationError for a bad key, name, description, or variation list, and
    Conflict when the flag key already exists. Existing environments keep
    off variation 0 and fallthrough variation 0. The audit document has no
    credential secret and no environment rule tree.
    """
    require_management(principal)
    key = require_public_key(command.key, "key")
    name = require_display_name(command.name, "name")
    description = require_description(command.description)
    texts = _variation_texts(command.variations, _CREATE_MINIMUM)
    with connection.transaction():
        project = _require_project(connection, project_key)
        try:
            flag_id = insert_flag(connection, project.id, key, name, description)
        except ConstraintViolation as exc:
            raise Conflict("Flag key already exists.") from exc
        for index, text in enumerate(texts):
            insert_variation(connection, flag_id, index, text)
        for environment_id in list_environment_ids(connection, project.id):
            insert_disabled_config(connection, flag_id, environment_id)
        flag = _load_flag(connection, project.id, key)
        record_change(
            connection,
            principal,
            "flag.create",
            project.key,
            None,
            flag.key,
            None,
            _audit_document(flag),
        )
    return flag


def get_flag(
    connection: Connection,
    principal: Principal,
    project_key: str,
    flag_key: str,
) -> Flag:
    """Return one flag, its variations, and every environment configuration.

    Raises Forbidden for an SDK key. Raises NotFound when the project or the
    flag is absent, including when the key cannot exist. Does not open a
    transaction.
    """
    require_management(principal)
    project = _require_project(connection, project_key)
    return _load_flag(connection, project.id, flag_key)


def list_flags_page(
    connection: Connection,
    principal: Principal,
    project_key: str,
    query: KeysetQuery,
    environment_key: str | None,
) -> FlagPage:
    """Return one page of flags ordered by key.

    environment_key, when set, adds that environment's summary to each item
    and raises NotFound when the environment is absent. Raises Forbidden for
    an SDK key and ValidationError for a bad limit or cursor. next_key is
    None when no further page exists. Does not open a transaction.
    """
    require_management(principal)
    project = _require_project(connection, project_key)
    environment_id = _environment_id(connection, project.id, environment_key)
    checked = _checked_query(query)
    rows = list_flags(connection, project.id, checked.after_key, checked.limit + 1)
    visible = rows[: checked.limit]
    if len(rows) <= checked.limit:
        next_key = None
    else:
        next_key = visible[-1].key
    items: list[FlagSummary] = []
    for row in visible:
        items.append(_summary(connection, row, environment_id))
    return FlagPage(items=items, next_key=next_key)


def patch_flag(
    connection: Connection,
    principal: Principal,
    project_key: str,
    flag_key: str,
    command: PatchFlag,
) -> Flag:
    """Update the name and description, and append variations.

    variations, when sent, must start with the stored list. Values after
    that prefix are appended. A shorter list or a different prefix raises
    ValidationError. Existing environment configs keep their indexes.
    Raises Forbidden, NotFound, and ValidationError as the other flag
    methods do. Writes one audit row in the same transaction.
    """
    require_management(principal)
    name = None if command.name is None else require_display_name(command.name, "name")
    description = None if command.description is None else require_description(command.description)
    with connection.transaction():
        project = _require_project(connection, project_key)
        current = _load_flag(connection, project.id, flag_key)
        stored_name = current.name if name is None else name
        stored_description = current.description if description is None else description
        tail = _appended_variations(current.variations, command.variations)
        row = find_flag(connection, project.id, current.key)
        if row is None:
            raise NotFound("Flag does not exist.")
        update_flag_text(connection, row.id, stored_name, stored_description)
        for offset, text in enumerate(tail):
            insert_variation(connection, row.id, len(current.variations) + offset, text)
        updated = _load_flag(connection, project.id, current.key)
        record_change(
            connection,
            principal,
            "flag.patch",
            project.key,
            None,
            updated.key,
            _audit_document(current),
            _audit_document(updated),
        )
    return updated


def _require_project(connection: Connection, project_key: str) -> ProjectRow:
    """Return the project row for project_key.

    Raises NotFound when the key is not a public key or no row matches,
    with the same message for both. Does not commit.
    """
    if not is_public_key(project_key):
        raise NotFound("Project does not exist.")
    project = find_project(connection, project_key)
    if project is None:
        raise NotFound("Project does not exist.")
    return project


def _load_flag(connection: Connection, project_id: int, flag_key: str) -> Flag:
    """Return the flag and its configurations.

    Raises NotFound when flag_key is not a public key or no row matches.
    Does not commit.
    """
    if not is_public_key(flag_key):
        raise NotFound("Flag does not exist.")
    row = find_flag(connection, project_id, flag_key)
    if row is None:
        raise NotFound("Flag does not exist.")
    variations = list_variations(connection, row.id)
    environments = tuple(_config(item) for item in list_configs(connection, row.id))
    return Flag(
        key=row.key,
        name=row.name,
        description=row.description,
        variations=variations,
        environments=environments,
    )


def _environment_id(
    connection: Connection,
    project_id: int,
    environment_key: str | None,
) -> int | None:
    """Return the environment id for a list filter.

    None means the caller did not ask for an environment. Raises NotFound
    when the key cannot exist or no row matches. Does not commit.
    """
    if environment_key is None:
        return None
    if not is_public_key(environment_key):
        raise NotFound("Environment does not exist.")
    environment = find_environment(connection, project_id, environment_key)
    if environment is None:
        raise NotFound("Environment does not exist.")
    return environment.id


def _summary(connection: Connection, row: FlagRow, environment_id: int | None) -> FlagSummary:
    """Build one list item, including the environment summary when requested.

    Raises StorageError when the environment exists and the flag has no
    config for it. Does not commit.
    """
    variations = list_variations(connection, row.id)
    environment = None
    if environment_id is not None:
        found = find_config_summary(connection, row.id, environment_id)
        if found is None:
            raise StorageError()
        environment = FlagConfigSummary(
            key=found.environment_key,
            enabled=found.enabled,
            off_variation=found.off_variation,
            version=found.version,
        )
    return FlagSummary(
        key=row.key,
        name=row.name,
        description=row.description,
        variations=variations,
        environment=environment,
    )


def _config(row: FlagConfigRow) -> FlagConfig:
    """Copy a repository config into the domain document.

    Does not read the database.
    """
    return FlagConfig(
        key=row.environment_key,
        enabled=row.enabled,
        off_variation=row.off_variation,
        fallthrough=row.fallthrough,
        rules=row.rules,
        targets=row.targets,
        prerequisites=row.prerequisites,
        version=row.version,
    )


def _variation_texts(variations: tuple[object, ...], minimum: int) -> list[str]:
    """Return the JSON text of each variation, in order.

    Raises ValidationError when the count or a value breaks a variation
    rule. Does not read the database.
    """
    require_variation_count(len(variations), minimum)
    return [variation_text(value, index) for index, value in enumerate(variations)]


def _appended_variations(
    stored: tuple[object, ...],
    proposed: tuple[object, ...] | None,
) -> list[str]:
    """Return the JSON text of variations that come after the stored prefix.

    An omitted proposal returns an empty list. Raises ValidationError when
    the proposal removes, reorders, or exceeds the variation cap. Does not
    read the database.
    """
    if proposed is None:
        return []
    if list(proposed[: len(stored)]) != list(stored):
        raise ValidationError(
            "Variations cannot be removed or reordered.",
            details=[
                {
                    "field": "variations",
                    "message": "Send the stored variations, then any new values.",
                }
            ],
        )
    require_variation_count(len(proposed), len(stored))
    start = len(stored)
    return [variation_text(value, start + offset) for offset, value in enumerate(proposed[start:])]


def _checked_query(query: KeysetQuery) -> KeysetQuery:
    """Return query after checking the limit and the cursor key.

    Raises ValidationError when the cursor key is not a public key. Does
    not read the database.
    """
    limit = require_page_limit(query.limit)
    if query.after_key is not None and not is_public_key(query.after_key):
        raise ValidationError(
            "The cursor is not valid.",
            details=[{"field": "cursor", "message": "Send the cursor from a previous page."}],
        )
    return KeysetQuery(limit=limit, after_key=query.after_key)


def _audit_document(flag: Flag) -> str:
    """Return the audit JSON for a flag.

    The document has the key, name, description, and variations. It does
    not include a secret or the environment rule trees.
    """
    return json.dumps(
        {
            "description": flag.description,
            "key": flag.key,
            "name": flag.name,
            "variations": list(flag.variations),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
