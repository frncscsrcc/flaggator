"""Checks for public keys, display names, variations, and list limits.

These are domain rules from the domain and security documents. They do not
read the database. Controllers use them after the JSON types are known, and
services use them again so a caller that skips HTTP still hits the same rule.
"""

import json
import math
import re

from domain.errors import ValidationError

PUBLIC_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_PUBLIC_KEY_LENGTH = 128
MAX_DISPLAY_NAME_LENGTH = 256
DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 200
MAX_REQUEST_BODY_BYTES = 256 * 1024
MIN_VARIATIONS = 2
MAX_VARIATIONS = 20
MAX_VARIATION_BYTES = 16 * 1024
# MySQL TEXT holds 65,535 bytes. A longer description would fail at insert.
MAX_DESCRIPTION_BYTES = 65535


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


def require_description(value: str) -> str:
    """Return value when it can be stored as a flag description.

    An empty description is allowed. Raises ValidationError when the UTF-8
    form is longer than MAX_DESCRIPTION_BYTES. Does not read the database.
    """
    if len(value.encode("utf-8")) > MAX_DESCRIPTION_BYTES:
        raise ValidationError(
            "The description is not valid.",
            details=[
                {
                    "field": "description",
                    "message": "Description must be at most 65535 bytes.",
                }
            ],
        )
    return value


def require_variation_count(count: int, minimum: int) -> None:
    """Raise ValidationError when count is outside the variation cap.

    minimum is 2 for a new flag and the stored length when appending.
    Does not read the database.
    """
    if count < minimum:
        raise ValidationError(
            "The variations are not valid.",
            details=[
                {
                    "field": "variations",
                    "message": "A flag needs at least two variations.",
                }
            ],
        )
    if count > MAX_VARIATIONS:
        raise ValidationError(
            "The variations are not valid.",
            details=[
                {
                    "field": "variations",
                    "message": "A flag can have at most 20 variations.",
                }
            ],
        )


def variation_text(value: object, index: int) -> str:
    """Return the JSON text stored for one variation value.

    index is the position in the list, used in the error message. Raises
    ValidationError for null, a non-finite number, or a value over 16 KiB.
    Does not read the database.
    """
    _reject_nulls(value, index)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValidationError(
            "The variations are not valid.",
            details=[
                {
                    "field": "variations",
                    "message": f"Variation {index} must be a finite JSON value.",
                }
            ],
        )
    if not isinstance(value, (str, int, float, bool, list, dict)):
        raise ValidationError(
            "The variations are not valid.",
            details=[
                {
                    "field": "variations",
                    "message": f"Variation {index} must be a JSON value.",
                }
            ],
        )
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValidationError(
            "The variations are not valid.",
            details=[
                {
                    "field": "variations",
                    "message": f"Variation {index} must be a JSON value.",
                }
            ],
        ) from exc
    if len(encoded.encode("utf-8")) > MAX_VARIATION_BYTES:
        raise ValidationError(
            "The variations are not valid.",
            details=[
                {
                    "field": "variations",
                    "message": f"Variation {index} must be at most 16 KiB.",
                }
            ],
        )
    return encoded


def _reject_nulls(value: object, index: int) -> None:
    """Raise ValidationError when value contains a JSON null.

    index names the variation in the message. Does not read the database.
    """
    if value is None:
        raise ValidationError(
            "The variations are not valid.",
            details=[
                {
                    "field": "variations",
                    "message": f"Variation {index} must not contain null.",
                }
            ],
        )
    if isinstance(value, list):
        for item in value:
            _reject_nulls(item, index)
    if isinstance(value, dict):
        for item in value.values():
            _reject_nulls(item, index)


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
