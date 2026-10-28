"""Checks for public keys, display names, and list limits.

These are domain rules from the domain and security documents. They do not
read the database. Controllers use them after the JSON types are known, and
services use them again so a caller that skips HTTP still hits the same rule.
"""

import re

from domain.errors import ValidationError

PUBLIC_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_PUBLIC_KEY_LENGTH = 128
MAX_DISPLAY_NAME_LENGTH = 256
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
MAX_REQUEST_BODY_BYTES = 256 * 1024


def is_public_key(value: str) -> bool:
    """Return True when value can be a project, environment, or flag key.

    The pattern is lowercase letters, digits, and single hyphens, with no
    leading or trailing hyphen, and at most MAX_PUBLIC_KEY_LENGTH characters.
    Does not read the database.
    """
    if len(value) > MAX_PUBLIC_KEY_LENGTH:
        return False
    return PUBLIC_KEY_PATTERN.fullmatch(value) is not None


def require_public_key(value: str, field: str) -> str:
    """Return value when it is a public key.

    Raises ValidationError when the key is empty, too long, or uses any
    other character. Does not read the database.
    """
    if is_public_key(value):
        return value
    raise ValidationError(
        "The key is not valid.",
        details=[
            {
                "field": field,
                "message": "Use lowercase letters, digits, and single hyphens.",
            }
        ],
    )


def require_display_name(value: str, field: str) -> str:
    """Return value when it can be stored as a name.

    Raises ValidationError when the name is empty, has surrounding spaces,
    or is longer than MAX_DISPLAY_NAME_LENGTH. Does not read the database.
    """
    if value == "" or value != value.strip():
        raise ValidationError(
            "The name is not valid.",
            details=[
                {
                    "field": field,
                    "message": "Name must not be empty or have surrounding spaces.",
                }
            ],
        )
    if len(value) > MAX_DISPLAY_NAME_LENGTH:
        raise ValidationError(
            "The name is not valid.",
            details=[
                {
                    "field": field,
                    "message": "Name must be at most 256 characters.",
                }
            ],
        )
    return value


def require_page_limit(limit: int) -> int:
    """Return limit when it is inside the list cap.

    Raises ValidationError below 1 or above MAX_PAGE_LIMIT. The security
    document rejects an over-large limit instead of silently shortening it.
    Does not read the database.
    """
    if limit < 1 or limit > MAX_PAGE_LIMIT:
        raise ValidationError(
            "The list limit is not valid.",
            details=[{"field": "limit", "message": "Limit must be from 1 to 200."}],
        )
    return limit
