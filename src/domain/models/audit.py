"""Audit events as the list API returns them."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class AuditActor:
    """Who performed the change.

    id is a credential id or a user id. kind is management, sdk, or user.
    """

    id: int
    kind: str


@dataclass(frozen=True)
class AuditEvent:
    """One mutation row for a project.

    before and after are parsed JSON objects or null. They never contain a
    credential secret or a password.
    """

    id: int
    at: str
    actor: AuditActor
    action: str
    flag_key: str | None
    environment_key: str | None
    before: Any
    after: Any


@dataclass(frozen=True)
class AuditQuery:
    """Filters for a project audit list.

    limit is already checked. flag_key and environment_key are optional
    public keys, or None to skip that filter.
    """

    limit: int
    flag_key: str | None
    environment_key: str | None
