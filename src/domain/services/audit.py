"""Record and list mutations in the audit log.

The use-case service calls record_change inside its transaction, after
authorization. list_project_audit is a read for readers.
"""

import json

from domain.clock import utc_timestamp
from domain.errors import NotFound, StorageError, ValidationError
from domain.models.audit import AuditActor, AuditEvent, AuditQuery
from domain.models.principal import Principal
from domain.services.access import require_reader
from domain.validation import is_public_key
from repositories.audit import AuditEventRow, append_event, list_project_events
from repositories.db import Connection
from repositories.projects import find_project


def record_change(
    connection: Connection,
    principal: Principal,
    action: str,
    project_key: str,
    environment_key: str | None,
    flag_key: str | None,
    before_json: str | None,
    after_json: str | None,
) -> None:
    """Append one audit row for a change principal is making.

    Does not commit. before_json and after_json are stored unchanged. The
    caller must not include a credential secret in either document. This
    function does not check that principal may perform action.
    """
    append_event(
        connection,
        principal,
        utc_timestamp(),
        action,
        project_key,
        environment_key,
        flag_key,
        before_json,
        after_json,
    )


def list_project_audit(
    connection: Connection,
    principal: Principal,
    project_key: str,
    query: AuditQuery,
) -> list[AuditEvent]:
    """Return audit events for project_key, newest first.

    Raises Forbidden without reader (or writer), NotFound when the project
    is absent, and ValidationError for a bad filter key. Does not commit.
    """
    require_reader(principal)
    if not is_public_key(project_key):
        raise NotFound("Project does not exist.")
    if query.flag_key is not None and not is_public_key(query.flag_key):
        raise ValidationError(
            "The flag filter is not valid.",
            details=[{"field": "flag", "message": "Use a public key."}],
        )
    if query.environment_key is not None and not is_public_key(query.environment_key):
        raise ValidationError(
            "The environment filter is not valid.",
            details=[{"field": "environment", "message": "Use a public key."}],
        )
    project = find_project(connection, project_key)
    if project is None:
        raise NotFound("Project does not exist.")
    rows = list_project_events(
        connection,
        project.key,
        query.flag_key,
        query.environment_key,
        query.limit,
    )
    return [_event(row) for row in rows]


def _event(row: AuditEventRow) -> AuditEvent:
    """Map a repository row to AuditEvent."""
    return AuditEvent(
        id=row.id,
        at=row.at,
        actor=AuditActor(id=row.actor_id, kind=row.actor_kind),
        action=row.action,
        flag_key=row.flag_key,
        environment_key=row.environment_key,
        before=_parse_document(row.before_json),
        after=_parse_document(row.after_json),
    )


def _parse_document(raw: str | None) -> object | None:
    """Parse a stored before/after JSON document.

    None stays None. Raises StorageError when the text is not JSON.
    """
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StorageError() from exc
