"""Create, patch, and list environments.

Creating an environment writes one disabled configuration for every flag
already in the project. With no flags, that loop inserts nothing.
"""

import json
import secrets

from domain.errors import Conflict, ConstraintViolation, NotFound, StorageError, ValidationError
from domain.models.catalog import (
    CreateEnvironment,
    Environment,
    EnvironmentPage,
    KeysetQuery,
    PatchEnvironment,
)
from domain.models.principal import Principal
from domain.services.access import require_protected_write, require_reader
from domain.services.audit import record_change
from domain.validation import is_public_key, require_display_name, require_page_limit, require_public_key
from repositories.db import Connection
from repositories.environments import (
    find_environment,
    find_environment_by_client_id,
    insert_environment,
    list_environments,
    set_client_id,
    update_environment,
)
from repositories.flag_configs import insert_disabled_config, list_flag_ids
from repositories.projects import find_project


def create_environment(
    connection: Connection,
    principal: Principal,
    project_key: str,
    command: CreateEnvironment,
) -> Environment:
    """Insert an environment, disabled flag configs, and one audit row.

    Raises Forbidden for an SDK token, NotFound when the project is absent,
    ValidationError for a bad key or name, and Conflict when this project
    already has the environment key. Configs are disabled with off variation
    0 and fallthrough variation 0. The audit document does not contain a
    credential secret.
    """
    require_protected_write(principal, command.protected)
    if not is_public_key(project_key):
        raise NotFound("Project does not exist.")
    key = require_public_key(command.key, "key")
    name = require_display_name(command.name, "name")
    protected = command.protected
    with connection.transaction():
        project = find_project(connection, project_key)
        if project is None:
            raise NotFound("Project does not exist.")
        try:
            environment_id = insert_environment(
                connection,
                project.id,
                key,
                name,
                protected,
            )
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
            _environment_document(key, name, protected),
        )
    return Environment(key=key, name=name, protected=protected, client_id=None)


def patch_environment(
    connection: Connection,
    principal: Principal,
    project_key: str,
    environment_key: str,
    command: PatchEnvironment,
) -> Environment:
    """Update name and/or protected for one environment.

    Raises Forbidden when the principal lacks writer or, for a protected
    environment, protected_writer. Raises NotFound when the project or
    environment is absent, and ValidationError for a bad name. Omitted
    fields stay unchanged.
    """
    if not is_public_key(project_key) or not is_public_key(environment_key):
        raise NotFound("Environment does not exist.")
    name = None
    if command.name is not None:
        name = require_display_name(command.name, "name")
    with connection.transaction():
        project = find_project(connection, project_key)
        if project is None:
            raise NotFound("Environment does not exist.")
        current = find_environment(connection, project.id, environment_key)
        if current is None:
            raise NotFound("Environment does not exist.")
        next_name = current.name if name is None else name
        next_protected = current.protected if command.protected is None else command.protected
        require_protected_write(principal, current.protected or next_protected)
        if next_name == current.name and next_protected == current.protected:
            return Environment(
                key=current.key,
                name=current.name,
                protected=current.protected,
                client_id=current.client_id,
            )
        before = _environment_document(current.key, current.name, current.protected)
        update_environment(connection, current.id, next_name, next_protected)
        after = _environment_document(current.key, next_name, next_protected)
        record_change(
            connection,
            principal,
            "environment.patch",
            project.key,
            current.key,
            None,
            before,
            after,
        )
    return Environment(
        key=current.key,
        name=next_name,
        protected=next_protected,
        client_id=current.client_id,
    )


def issue_client_id(
    connection: Connection,
    principal: Principal,
    project_key: str,
    environment_key: str,
) -> Environment:
    """Issue or rotate the opaque client id for one environment.

    A protected environment needs protected_writer. The previous id stops
    working. The new id is returned in clear text. Raises NotFound when
    the project or environment is absent, and Forbidden when the principal
    may not write it.
    """
    if not is_public_key(project_key) or not is_public_key(environment_key):
        raise NotFound("Environment does not exist.")
    with connection.transaction():
        project = find_project(connection, project_key)
        if project is None:
            raise NotFound("Environment does not exist.")
        current = find_environment(connection, project.id, environment_key)
        if current is None:
            raise NotFound("Environment does not exist.")
        require_protected_write(principal, current.protected)
        client_id = _unused_client_id(connection)
        try:
            set_client_id(connection, current.id, client_id)
        except ConstraintViolation as exc:
            raise StorageError() from exc
        record_change(
            connection,
            principal,
            "environment.client_id",
            project.key,
            current.key,
            None,
            _client_id_document(current.client_id),
            _client_id_document(client_id),
        )
    return Environment(
        key=current.key,
        name=current.name,
        protected=current.protected,
        client_id=client_id,
    )


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
    require_reader(principal)
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


def _unused_client_id(connection: Connection) -> str:
    """Return a client id that no environment currently stores.

    A collision on insert is still possible and becomes StorageError.
    Does not write.
    """
    for _attempt in range(3):
        candidate = secrets.token_urlsafe(32)
        if find_environment_by_client_id(connection, candidate) is None:
            return candidate
    raise StorageError()


def _client_id_document(client_id: str | None) -> str:
    """Return the audit JSON for a client id.

    The id is not a secret. Does not read the database.
    """
    return json.dumps(
        {"client_id": client_id},
        sort_keys=True,
        separators=(",", ":"),
    )


def _environment_document(key: str, name: str, protected: bool) -> str:
    """Return the audit JSON for an environment.

    The document has the public fields only. Does not include a secret.
    """
    return json.dumps(
        {"key": key, "name": name, "protected": protected},
        sort_keys=True,
        separators=(",", ":"),
    )
