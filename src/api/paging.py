"""Opaque list cursors and the limit query parameter.

The cursor is base64url of the text `v1:` plus a public key, without
padding. Clients send it back unchanged. The service pages with
`key` greater than that key.
"""

import base64

from api.request import Request
from domain.errors import ValidationError
from domain.models.catalog import KeysetQuery
from domain.validation import DEFAULT_PAGE_LIMIT, is_public_key, require_page_limit

_CURSOR_PREFIX = "v1:"
_CURSOR_MESSAGE = "Send the cursor from a previous page."


def page_request(request: Request) -> KeysetQuery:
    """Read limit and cursor from the query string.

    A missing limit is the default page size. A missing cursor starts at
    the first key. Raises ValidationError for a bad limit or a bad cursor.
    Does not read the database.
    """
    return KeysetQuery(
        limit=_parse_limit(request.query.get("limit")),
        after_key=_parse_cursor(request.query.get("cursor")),
    )


def page_body(items: list[dict], next_key: str | None) -> dict[str, object]:
    """Return the list document.

    next_cursor is null when next_key is None, which includes an empty
    page. Does not read the database.
    """
    if next_key is None:
        cursor = None
    else:
        cursor = encode_cursor(next_key)
    return {"items": items, "next_cursor": cursor}


def encode_cursor(public_key: str) -> str:
    """Return the opaque cursor that points at public_key.

    The decoded text is `v1:` plus public_key. Does not read the database.
    """
    raw = f"{_CURSOR_PREFIX}{public_key}".encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _parse_limit(raw: str | None) -> int:
    """Return the page size from raw.

    None selects the default. Raises ValidationError when raw is not a
    whole number in range. Does not read the database.
    """
    if raw is None:
        return DEFAULT_PAGE_LIMIT
    if not raw.isdigit():
        raise ValidationError(
            "The list limit is not valid.",
            details=[{"field": "limit", "message": "Limit must be a whole number from 1 to 200."}],
        )
    return require_page_limit(int(raw))


def _parse_cursor(raw: str | None) -> str | None:
    """Return the public key inside raw, or None when raw is absent.

    Raises ValidationError when the cursor is not the v1 keyset form.
    Does not read the database.
    """
    if raw is None:
        return None
    key = _decode_cursor(raw)
    if not is_public_key(key):
        raise ValidationError(
            "The cursor is not valid.",
            details=[{"field": "cursor", "message": _CURSOR_MESSAGE}],
        )
    return key


def _decode_cursor(cursor: str) -> str:
    """Return the text after `v1:` in cursor.

    Raises ValidationError when the cursor is empty, not base64url, or not
    the v1 prefix. Does not read the database.
    """
    if cursor == "":
        raise ValidationError(
            "The cursor is not valid.",
            details=[{"field": "cursor", "message": _CURSOR_MESSAGE}],
        )
    padded = cursor + ("=" * (-len(cursor) % 4))
    try:
        text = base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")
    except (ValueError, UnicodeError) as exc:
        raise ValidationError(
            "The cursor is not valid.",
            details=[{"field": "cursor", "message": _CURSOR_MESSAGE}],
        ) from exc
    if not text.startswith(_CURSOR_PREFIX):
        raise ValidationError(
            "The cursor is not valid.",
            details=[{"field": "cursor", "message": _CURSOR_MESSAGE}],
        )
    return text[len(_CURSOR_PREFIX) :]
