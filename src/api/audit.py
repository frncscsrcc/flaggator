"""Controller for project audit reads."""

from api.request import Request, Response
from domain.errors import ValidationError
from domain.models.audit import AuditEvent, AuditQuery
from domain.models.principal import Principal
from domain.services import audit as audit_service
from domain.validation import DEFAULT_AUDIT_LIMIT, require_page_limit
from repositories.db import Connection


def list_audit(
    principal: Principal,
    connection: Connection,
    request: Request,
    project_key: str,
) -> Response:
    """List audit events for project_key.

    Returns 200 with items newest first. Does not commit.
    """
    events = audit_service.list_project_audit(
        connection,
        principal,
        project_key,
        _query(request),
    )
    items = [_document(event) for event in events]
    return Response(status=200, body={"items": items})


def _query(request: Request) -> AuditQuery:
    """Read limit, flag, and environment from the query string."""
    return AuditQuery(
        limit=_parse_limit(request.query.get("limit")),
        flag_key=request.query.get("flag"),
        environment_key=request.query.get("environment"),
    )


def _parse_limit(raw: str | None) -> int:
    """Return the audit page size from raw."""
    if raw is None:
        return DEFAULT_AUDIT_LIMIT
    if not raw.isdigit():
        raise ValidationError(
            "The list limit is not valid.",
            details=[{"field": "limit", "message": "Limit must be a whole number from 1 to 200."}],
        )
    return require_page_limit(int(raw))


def _document(event: AuditEvent) -> dict:
    """Return the JSON object for one audit event."""
    return {
        "id": event.id,
        "at": event.at,
        "actor": {"id": event.actor.id, "kind": event.actor.kind},
        "action": event.action,
        "flag_key": event.flag_key,
        "environment_key": event.environment_key,
        "before": event.before,
        "after": event.after,
    }
