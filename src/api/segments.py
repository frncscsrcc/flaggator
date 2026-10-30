"""Controllers for segments nested under an environment.

Each function checks the request shape, calls one service, and maps the
result. The segment key in a path is not repeated in a replace body.
"""

from api.body import read_json_object
from api.paging import page_body, page_request
from api.request import Request, Response
from domain.errors import ValidationError
from domain.models.principal import Principal
from domain.models.segment import CreateSegment, ReplaceSegment, Segment
from domain.services import segments as segment_service
from repositories.db import Connection
from repositories.eval_cache import EvaluationCache


def create_segment(
    principal: Principal,
    connection: Connection,
    request: Request,
    project_key: str,
    environment_key: str,
) -> Response:
    """Create a segment from the JSON body.

    The body requires key, name, and subjects. Returns 201 and the stored
    document. Domain failures propagate. Does not commit; the service does.
    """
    segment = segment_service.create_segment(
        connection,
        principal,
        project_key,
        environment_key,
        _create_command(request),
    )
    return Response(status=201, body=_document(segment))


def list_segments(
    principal: Principal,
    connection: Connection,
    request: Request,
    project_key: str,
    environment_key: str,
) -> Response:
    """List segments in one environment.

    Returns 200. items is empty when the environment has no segments on
    this page. next_cursor is null on the last page. Does not commit.
    """
    page = segment_service.list_segments_page(
        connection,
        principal,
        project_key,
        environment_key,
        page_request(request),
    )
    items = [_document(item) for item in page.items]
    return Response(status=200, body=page_body(items, page.next_key))


def get_segment(
    principal: Principal,
    connection: Connection,
    project_key: str,
    environment_key: str,
    segment_key: str,
) -> Response:
    """Fetch one segment.

    Returns 200 and the stored document. NotFound propagates when the
    project, environment, or segment is absent. Does not commit.
    """
    segment = segment_service.get_segment(
        connection,
        principal,
        project_key,
        environment_key,
        segment_key,
    )
    return Response(status=200, body=_document(segment))


def replace_segment(
    principal: Principal,
    connection: Connection,
    evaluation_cache: EvaluationCache,
    request: Request,
    project_key: str,
    environment_key: str,
    segment_key: str,
) -> Response:
    """Replace one segment's name and subjects from the JSON body.

    Returns 200 and the stored document. The key in the path does not
    change. Domain failures propagate. Does not commit; the service does.
    """
    segment = segment_service.replace_segment(
        connection,
        principal,
        project_key,
        environment_key,
        segment_key,
        _replace_command(request),
        evaluation_cache,
    )
    return Response(status=200, body=_document(segment))


def delete_segment(
    principal: Principal,
    connection: Connection,
    project_key: str,
    environment_key: str,
    segment_key: str,
) -> Response:
    """Delete one segment that no target cites.

    Returns 204 with an empty object. A segment a target still cites is
    409. Does not commit; the service does.
    """
    segment_service.delete_segment_now(
        connection,
        principal,
        project_key,
        environment_key,
        segment_key,
    )
    return Response(status=204, body={})


def _create_command(request: Request) -> CreateSegment:
    """Build a create command from the JSON body.

    Raises ValidationError when a field is missing, has the wrong type, or
    is not part of the create document. Does not read the database.
    """
    body = read_json_object(request)
    _reject_unknown(body, {"key", "name", "subjects"})
    key = _require_string(body, "key")
    name = _require_string(body, "name")
    return CreateSegment(key=key, name=name, subjects=_subjects(body))


def _replace_command(request: Request) -> ReplaceSegment:
    """Build a replace command from the JSON body.

    Raises ValidationError when name or subjects is missing or has the
    wrong type, or when the body contains another field. Does not read
    the database.
    """
    body = read_json_object(request)
    _reject_unknown(body, {"name", "subjects"})
    name = _require_string(body, "name")
    return ReplaceSegment(name=name, subjects=_subjects(body))


def _subjects(body: dict) -> tuple[str, ...]:
    """Return the subject tuple from body.

    Raises ValidationError when subjects is missing, is not a list, or
    contains a non-string. Does not read the database.
    """
    if "subjects" not in body:
        raise ValidationError(
            "The request body has the wrong fields.",
            details=[{"field": "subjects", "message": "This field is required."}],
        )
    subjects = body["subjects"]
    if not isinstance(subjects, list):
        raise ValidationError(
            "The request body has the wrong fields.",
            details=[{"field": "subjects", "message": "This field must be a list."}],
        )
    cleaned: list[str] = []
    for subject in subjects:
        if not isinstance(subject, str):
            raise ValidationError(
                "The request body has the wrong fields.",
                details=[{"field": "subjects", "message": "A segment subject must be a string."}],
            )
        cleaned.append(subject)
    return tuple(cleaned)


def _require_string(body: dict, field: str) -> str:
    """Return one string field from body.

    Raises ValidationError when the field is missing or is not a string.
    Does not read the database.
    """
    if field not in body:
        raise ValidationError(
            "The request body has the wrong fields.",
            details=[{"field": field, "message": "This field is required."}],
        )
    value = body[field]
    if not isinstance(value, str):
        raise ValidationError(
            "The request body has the wrong fields.",
            details=[{"field": field, "message": "This field must be a string."}],
        )
    return value


def _reject_unknown(body: dict, allowed: set[str]) -> None:
    """Raise ValidationError when body contains a field that is not allowed.

    Does not read the database.
    """
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise ValidationError(
            "The request body has the wrong fields.",
            details=[{"field": unknown[0], "message": "This field is not accepted."}],
        )


def _document(segment: Segment) -> dict:
    """Return the JSON object for one segment.

    subjects is a list, in stored order. Does not read the database.
    """
    return {
        "key": segment.key,
        "name": segment.name,
        "subjects": list(segment.subjects),
    }
