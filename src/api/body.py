"""JSON object bodies for write controllers.

Shape checks live here: content type, size, and field types. Whether a key
matches the public-key pattern is a domain check in the service.
"""

import json

from api.request import Request
from domain.errors import ValidationError
from domain.validation import MAX_REQUEST_BODY_BYTES


def read_json_object(request: Request) -> dict:
    """Parse request.body as one JSON object.

    Raises ValidationError when the content type is not JSON, the body is
    larger than the request limit, the bytes are not UTF-8 JSON, or the
    value is not an object. Does not read the database.
    """
    content_type = request.headers.get("content-type")
    if not _is_json_content_type(content_type):
        raise ValidationError(
            "The request body must be JSON.",
            details=[{"field": "content-type", "message": "Content-Type must be application/json."}],
        )
    if len(request.body) > MAX_REQUEST_BODY_BYTES:
        raise ValidationError(
            "The request body is too large.",
            details=[{"field": "body", "message": "The body must be at most 256 KiB."}],
        )
    try:
        text = request.body.decode("utf-8")
    except UnicodeError as exc:
        raise ValidationError(
            "The request body is not JSON.",
            details=[{"field": "body", "message": "The body must be UTF-8 JSON."}],
        ) from exc
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValidationError(
            "The request body is not JSON.",
            details=[{"field": "body", "message": "The body must be a JSON object."}],
        ) from exc
    if not isinstance(parsed, dict):
        raise ValidationError(
            "The request body must be a JSON object.",
            details=[{"field": "body", "message": "The body must be a JSON object."}],
        )
    return parsed


def require_string_fields(body: dict, fields: tuple[str, ...]) -> dict[str, str]:
    """Return the named string fields from body.

    Raises ValidationError when a field is missing, is not a string, or when
    body contains any other field. Does not read the database.
    """
    allowed = set(fields)
    details: list[dict[str, str]] = []
    for field in fields:
        if field not in body:
            details.append({"field": field, "message": "This field is required."})
        elif not isinstance(body[field], str):
            details.append({"field": field, "message": "This field must be a string."})
    for field in sorted(set(body) - allowed):
        details.append({"field": field, "message": "This field is not accepted."})
    if details:
        raise ValidationError("The request body has the wrong fields.", details=details)
    values: dict[str, str] = {}
    for field in fields:
        values[field] = body[field]
    return values


def _is_json_content_type(content_type: str | None) -> bool:
    """Return True when content_type is application/json, with or without parameters.

    A missing header is False. Does not read the body.
    """
    if content_type is None:
        return False
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "application/json"
