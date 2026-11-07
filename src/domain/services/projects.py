"""Create, fetch, and list projects.

The service checks the management principal, validates the key and name, and
owns the transaction for the create.
"""

import json

from domain.errors import Conflict, ConstraintViolation, NotFound, ValidationError
from domain.models.catalog import CreateNamed, Project, ProjectListQuery, ProjectPage
from domain.models.principal import Principal
from domain.services.access import require_reader, require_writer
from domain.services.audit import record_change
from domain.validation import is_public_key, require_display_name, require_page_limit, require_public_key
from repositories.db import Connection
from repositories.projects import find_project, insert_project, list_projects

# A stored name is at most 256 characters and a key at most 128, so a
# longer search cannot match either column.
_MAX_SEARCH_LENGTH = 256


def create_project(
    connection: Connection,
    principal: Principal,
    command: CreateNamed,
) -> Project:
    """Insert a project and an audit row, then return the stored project.

    Raises Forbidden for an SDK token, ValidationError for a bad key or name,
    and Conflict when the key already exists. The audit document is the key
    and the name. It does not contain a credential secret.
    """
    require_writer(principal)
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

    Raises Forbidden for an SDK token. Raises NotFound when the key is not a
    public key or no row matches, with the same message for both. Does not
    open a transaction.
    """
    require_reader(principal)
    if not is_public_key(project_key):
        raise NotFound("Project does not exist.")
    row = find_project(connection, project_key)
    if row is None:
        raise NotFound("Project does not exist.")
    return Project(key=row.key, name=row.name)


def list_projects_page(
    connection: Connection,
    principal: Principal,
    query: ProjectListQuery,
) -> ProjectPage:
    """Return one page of projects ordered by key.

    search keeps projects whose name or key contains that text. Raises
    Forbidden for an SDK token and ValidationError for a bad limit, cursor
    key, or search. Reads one extra row to decide next_key. next_key is
    None when no further page exists. Does not open a transaction.
    """
    require_reader(principal)
    checked = _checked_query(query)
    rows = list_projects(connection, checked.after_key, checked.limit + 1, checked.search)
    return ProjectPage(items=_visible(rows, checked.limit), next_key=_next_key(rows, checked.limit))


def _checked_query(query: ProjectListQuery) -> ProjectListQuery:
    """Return query after checking the limit, the cursor key, and search.

    Raises ValidationError when the cursor key is not a public key or the
    search is too long. Does not read the database.
    """
    limit = require_page_limit(query.limit)
    if query.after_key is not None and not is_public_key(query.after_key):
        raise ValidationError(
            "The cursor is not valid.",
            details=[{"field": "cursor", "message": "Send the cursor from a previous page."}],
        )
    return ProjectListQuery(limit=limit, after_key=query.after_key, search=_require_search(query.search))


def _require_search(search: str | None) -> str | None:
    """Return search when it can be a substring filter.

    None means the list is not filtered. Raises ValidationError when the
    text is longer than _MAX_SEARCH_LENGTH. Does not read the database.
    """
    if search is None:
        return None
    if len(search) > _MAX_SEARCH_LENGTH:
        raise ValidationError(
            "The search is not valid.",
            details=[
                {
                    "field": "search",
                    "message": f"Search must be at most {_MAX_SEARCH_LENGTH} characters.",
                }
            ],
        )
    return search


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
