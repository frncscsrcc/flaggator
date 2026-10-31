"""Password hashing with stdlib scrypt.

Stored form: scrypt$N$r$p$salt_b64$dk_b64. Does not log the password.
"""

import base64
import hashlib
import hmac
import secrets

from domain.errors import ValidationError

_N = 2**14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16
_MIN_PASSWORD_LENGTH = 8
_MAX_PASSWORD_LENGTH = 1024


def require_password(password: str, field: str = "password") -> str:
    """Return password when it meets length rules.

    Raises ValidationError when empty after strip rules fail or length is
    out of range. Surrounding spaces are rejected. Does not hash.
    """
    if password == "" or password != password.strip():
        raise ValidationError(
            "The password is not valid.",
            details=[
                {
                    "field": field,
                    "message": "Password must not be empty or have surrounding spaces.",
                }
            ],
        )
    if len(password) < _MIN_PASSWORD_LENGTH:
        raise ValidationError(
            "The password is not valid.",
            details=[
                {
                    "field": field,
                    "message": f"Password must be at least {_MIN_PASSWORD_LENGTH} characters.",
                }
            ],
        )
    if len(password) > _MAX_PASSWORD_LENGTH:
        raise ValidationError(
            "The password is not valid.",
            details=[
                {
                    "field": field,
                    "message": f"Password must be at most {_MAX_PASSWORD_LENGTH} characters.",
                }
            ],
        )
    return password


def hash_password(password: str) -> str:
    """Return a scrypt hash string for password.

    Does not log password. Uses a fresh salt each call.
    """
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_N,
        r=_R,
        p=_P,
        dklen=_DKLEN,
    )
    salt_b64 = base64.b64encode(salt).decode("ascii")
    digest_b64 = base64.b64encode(digest).decode("ascii")
    return f"scrypt${_N}${_R}${_P}${salt_b64}${digest_b64}"


def verify_password(password: str, stored: str) -> bool:
    """Return True when password matches stored.

    A malformed stored value is False. Uses compare_digest on the digests.
    Does not log password.
    """
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n = int(parts[1])
        r = int(parts[2])
        p = int(parts[3])
        salt = base64.b64decode(parts[4].encode("ascii"), validate=True)
        expected = base64.b64decode(parts[5].encode("ascii"), validate=True)
    except (ValueError, TypeError):
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
    )
    return hmac.compare_digest(digest, expected)
