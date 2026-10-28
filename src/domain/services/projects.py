"""Create, fetch, and list projects.

The service checks the management principal, validates the key and name, and
owns the transaction for the create.
"""

import json

from domain.errors import Conflict, ConstraintViolation, NotFound, ValidationError
from domain.models.catalog import CreateNamed, KeysetQuery, Project, ProjectPage
from domain.models.principal import Principal
from domain.services.access import require_management
from domain.services.audit import record_change
from domain.validation import is_public_key, require_display_name, require_page_limit, require_public_key
from repositories.db import Connection
from repositories.projects import find_project, insert_project, list_projects


def create_project(
    connection: Connection,
    principal: Principal,
    command: CreateNamed,
) -> Project:
    """Insert a project and an audit row, then return the stored project.

    Raises Forbidden for an SDK key, ValidationError for a bad key or name,
    and Conflict when the key already exists. The audit document is the key
    and the name. It does not contain a credential secret.
    """
    require_management(principal)
    key = require_public_key(command.key, "key")
    name = require_display_name(command.name, "name")
    with connection.transaction():
        try:
            insert_project(connection, key, name)
        except ConstraintViolation as exc:
            raise Conflict("Project key already exists.") from exc
        record_change(
            connection,
            principal,
            "project.create",
            key,
            None,
            None,
            None,
            _after_document(key, name),
        )
    return Project(key=key, name=name)


def get_project(connection: Connection, principal: Principal, project_key: str) -> Project:
    """Return the project named by project_key.

    Raises Forbidden for an SDK key. Raises NotFound when the key is not a
    public key or no row matches, with the same message for both. Does not
    open a transaction.
    """
    require_management(principal)
    if not is_public_key(project_key):
        raise NotFound("Project does not exist.")
    row = find_project(connection, project_key)
    if row is None:
        raise NotFound("Project does not exist.")
    return Project(key=row.key, name=row.name)


def list_projects_page(
    connection: Connection,
    principal: Principal,
    query: KeysetQuery,
) -> ProjectPage:
    """Return one page of projects ordered by key.

    Raises Forbidden for an SDK key and ValidationError for a bad limit or
    cursor key. Reads one extra row to decide next_key. next_key is None
    when no further page exists. Does not open a transaction.
    """
    require_management(principal)
    checked = _checked_query(query)
    rows = list_projects(connection, checked.after_key, checked.limit + 1)
    return ProjectPage(items=_visible(rows, checked.limit), next_key=_next_key(rows, checked.limit))


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


def _visible(rows: list[Project], limit: int) -> list[Project]:
    """Return at most limit rows.

    The extra row, when present, is dropped. An empty list stays empty.
    Does not read the database.
    """
    return rows[:limit]


def _next_key(rows: list[Project], limit: int) -> str | None:
    """Return the last visible key when another row was fetched.

    Returns None when this page is the last. Does not read the database.
    """
    if len(rows) <= limit:
        return None
    return rows[limit - 1].key


def _after_document(key: str, name: str) -> str:
    """Return the audit JSON for a created project.

    The document has the public fields only. Does not include a secret.
    """
    return json.dumps({"key": key, "name": name}, sort_keys=True, separators=(",", ":"))
