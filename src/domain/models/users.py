"""Users and login commands as the API and services share them.

Passwords are never fields on User. Database ids are the public user id.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class User:
    """One user as the API shows it.

    roles is the closed set. disabled_at is null when the user may log in.
    """

    id: int
    email: str
    name: str
    roles: tuple[str, ...]
    created_at: str
    disabled_at: str | None


@dataclass(frozen=True)
class CreateUser:
    """Body for creating a user.

    password None means an IdP-only user (no local login).
    """

    email: str
    name: str
    password: str | None
    roles: tuple[str, ...]


@dataclass(frozen=True)
class PatchUser:
    """Body for patching a user.

    None means the field was omitted. disabled True sets disabled_at; False
    clears it.
    """

    name: str | None
    password: str | None
    roles: tuple[str, ...] | None
    disabled: bool | None


@dataclass(frozen=True)
class UserKeysetQuery:
    """Page window for users ordered by id.

    after_id excludes that id and every smaller id. None starts at the first.
    search None lists every user in that window. A string keeps users whose
    name or email contains it.
    """

    limit: int
    after_id: int | None
    search: str | None


@dataclass(frozen=True)
class UserPage:
    """One page of users.

    next_id is the last id on this page when another page exists.
    """

    items: list[User]
    next_id: int | None


@dataclass(frozen=True)
class LoginCommand:
    """Email and password for local login."""

    email: str
    password: str


@dataclass(frozen=True)
class AccessToken:
    """The login response without a refresh token."""

    access_token: str
    token_type: str
    expires_in: int
