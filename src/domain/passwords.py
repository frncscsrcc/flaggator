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
# A password already stored may be this short. Login still checks it.
_MIN_PASSWORD_LENGTH = 8
# A password being set now. Raising the login minimum would lock out
# hashes that were stored under the old rule.
_MIN_NEW_PASSWORD_LENGTH = 12
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
    if len(password) < _MIN_NEW_PASSWORD_LENGTH:
        raise ValidationError(
            "The password is not valid.",
            details=[
                {
                    "field": field,
                    "message": f"Password must be at least {_MIN_NEW_PASSWORD_LENGTH} characters.",
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


def password_is_checkable(password: str) -> bool:
    """Return True when password is inside the length rules.

    Surrounding spaces are rejected. Does not hash and does not log password.
    """
    if password == "" or password != password.strip():
        return False
    return _MIN_PASSWORD_LENGTH <= len(password) <= _MAX_PASSWORD_LENGTH


def burn_password_check(password: str) -> None:
    """Spend one scrypt verification and ignore the result.

    Login calls this when there is no stored hash, so the CPU cost matches
    a wrong password. Does not log password.
    """
    verify_password(password, _dummy_stored_hash())


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


_dummy_stored: str | None = None


def _dummy_stored_hash() -> str:
    """Return a fixed scrypt hash used only to burn a check.

    Built once per process. The plaintext is not a credential.
    """
    global _dummy_stored
    if _dummy_stored is None:
        _dummy_stored = hash_password("not-a-user-password")
    return _dummy_stored
