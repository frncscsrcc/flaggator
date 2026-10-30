"""Create and list environments.

Creating an environment writes one disabled configuration for every flag
already in the project. With no flags, that loop inserts nothing.
"""

import json

from domain.errors import Conflict, ConstraintViolation, NotFound, ValidationError
from domain.models.catalog import CreateNamed, Environment, EnvironmentPage, KeysetQuery
from domain.models.principal import Principal
from domain.services.access import require_management
from domain.services.audit import record_change
from domain.validation import is_public_key, require_display_name, require_page_limit, require_public_key
from repositories.db import Connection
from repositories.environments import insert_environment, list_environments
from repositories.flag_configs import insert_disabled_config, list_flag_ids
from repositories.projects import find_project


def create_environment(
    connection: Connection,
    principal: Principal,
    project_key: str,
    command: CreateNamed,
) -> Environment:
    """Insert an environment, disabled flag configs, and one audit row.

    Raises Forbidden for an SDK token, NotFound when the project is absent,
    ValidationError for a bad key or name, and Conflict when this project
    already has the environment key. Configs are disabled with off variation
    0 and fallthrough variation 0. The audit document does not contain a
    credential secret.
    """
    require_management(principal)
    if not is_public_key(project_key):
        raise NotFound("Project does not exist.")
    key = require_public_key(command.key, "key")
    name = require_display_name(command.name, "name")
    with connection.transaction():
        project = find_project(connection, project_key)
        if project is None:
            raise NotFound("Project does not exist.")
        try:
            environment_id = insert_environment(connection, project.id, key, name)
        except ConstraintViolation as exc:
            raise Conflict("Environment key already exists.") from exc
        for flag_id in list_flag_ids(connection, project.id):
            insert_disabled_config(connection, flag_id, environment_id)
        record_change(
            connection,
            principal,
            "environment.create",
            project.key,
            key,
            None,
            None,
            _after_document(key, name),
        )
    return Environment(key=key, name=name)


def list_environments_page(
    connection: Connection,
    principal: Principal,
    project_key: str,
    query: KeysetQuery,
) -> EnvironmentPage:
    """Return one page of environments in project_key.

    Raises Forbidden for an SDK token, NotFound when the project is absent,
    and ValidationError for a bad limit or cursor key. The existence check
    and the page share one transaction. next_key is None when no further
    page exists.
    """
    require_management(principal)
    if not is_public_key(project_key):
        raise NotFound("Project does not exist.")
    checked = _checked_query(query)
    with connection.transaction():
        project = find_project(connection, project_key)
        if project is None:
            raise NotFound("Project does not exist.")
        rows = list_environments(connection, project.id, checked.after_key, checked.limit + 1)
    visible = rows[: checked.limit]
    if len(rows) <= checked.limit:
        next_key = None
    else:
        next_key = visible[-1].key
    return EnvironmentPage(items=visible, next_key=next_key)


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


def _after_document(key: str, name: str) -> str:
    """Return the audit JSON for a created environment.

    The document has the public fields only. Does not include a secret.
    """
    return json.dumps({"key": key, "name": name}, sort_keys=True, separators=(",", ":"))
