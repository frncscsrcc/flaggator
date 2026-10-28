"""The caller, after authentication has succeeded.

A principal never carries a secret or a hash. Opaque tokens and, later, a
standard JWT both build this same object.
"""

from dataclasses import dataclass

KIND_MANAGEMENT = "management"
KIND_SDK = "sdk"


@dataclass(frozen=True)
class Principal:
    """Who is calling.

    credential_id is the audit actor. kind is "management" or "sdk".
    environment_id is set for an SDK key and None for a management token.
    prefix is the non-secret label stored beside the hash. This object does
    not know whether it came from a stored token or, later, a JWT.
    """

    credential_id: int
    kind: str
    environment_id: int | None
    prefix: str
