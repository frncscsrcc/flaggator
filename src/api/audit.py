"""Controller for project and server audit reads."""

import base64

from api.request import Request, Response
from domain.errors import ValidationError
from domain.models.audit import AuditEvent, AuditQuery
from domain.models.principal import Principal
from domain.services import audit as audit_service
from domain.validation import DEFAULT_AUDIT_LIMIT, require_page_limit
from repositories.db import Connection

_CURSOR_PREFIX = "v1:"


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


def list_server_audit(
    principal: Principal,
    connection: Connection,
    request: Request,
) -> Response:
    """List audit rows that are not tied to a project.

    Returns 200 with items newest first and next_cursor when another page
    exists. The cursor is an event id, not a public key. Does not commit.
    """
    limit = _parse_limit(request.query.get("limit"))
    before_id = _parse_id_cursor(request.query.get("cursor"))
    events, next_id = audit_service.list_server_audit(
        connection,
        principal,
        limit,
        before_id,
    )
    body: dict[str, object] = {"items": [_document(event) for event in events]}
    if next_id is not None:
        body["next_cursor"] = _encode_id_cursor(next_id)
    return Response(status=200, body=body)


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


def _parse_id_cursor(raw: str | None) -> int | None:
    """Return the event id inside raw, or None when raw is absent.

    Raises ValidationError when the cursor is not the v1 id form. Does not
    read the database.
    """
    if raw is None:
        return None
    padding = "=" * (-len(raw) % 4)
    try:
        text = base64.urlsafe_b64decode(raw + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationError(
            "The list cursor is not valid.",
            details=[{"field": "cursor", "message": "Use a cursor from a previous page."}],
        ) from exc
    if not text.startswith(_CURSOR_PREFIX) or not text.removeprefix(_CURSOR_PREFIX).isdigit():
        raise ValidationError(
            "The list cursor is not valid.",
            details=[{"field": "cursor", "message": "Use a cursor from a previous page."}],
        )
    return int(text.removeprefix(_CURSOR_PREFIX))


def _encode_id_cursor(event_id: int) -> str:
    """Return the opaque cursor that points at event_id.

    The decoded text is `v1:` plus the decimal id. Does not read the database.
    """
    raw = f"{_CURSOR_PREFIX}{event_id}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


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
