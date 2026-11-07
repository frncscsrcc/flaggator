"""User rows.

SQL for the users table lives here. Functions do not commit and do not
check the principal.
"""

import json
from dataclasses import dataclass

from domain.errors import StorageError
from domain.models.principal import ALLOWED_ROLES
from domain.models.users import User
from repositories.db import Connection


@dataclass(frozen=True)
class UserRow:
    """One user as authentication and management need it.

    password_hash is None for an IdP-only user. roles is the closed set
    from roles_json. disabled_at is None when the user may authenticate.
    """

    id: int
    email: str
    name: str
    password_hash: str | None
    roles: tuple[str, ...]
    created_at: str
    disabled_at: str | None


def insert_user(
    connection: Connection,
    email: str,
    name: str,
    password_hash: str | None,
    roles: tuple[str, ...],
    created_at: str,
) -> int:
    """Insert one user and return its id.

    Raises ConstraintViolation when email already exists. Does not commit.
    """
    cursor = connection.execute(
        """
        INSERT INTO users (
            email, name, password_hash, roles_json, created_at, disabled_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (email, name, password_hash, _roles_json(roles), created_at, None),
    )
    return cursor.last_insert_id


def update_user(
    connection: Connection,
    user_id: int,
    name: str,
    password_hash: str | None,
    roles: tuple[str, ...],
    disabled_at: str | None,
) -> None:
    """Replace mutable columns for user_id.

    Does not commit. The caller already checked that the row exists.
    """
    connection.execute(
        """
        UPDATE users
           SET name = ?,
               password_hash = ?,
               roles_json = ?,
               disabled_at = ?
         WHERE id = ?
        """,
        (name, password_hash, _roles_json(roles), disabled_at, user_id),
    )


def find_user_by_id(connection: Connection, user_id: int) -> UserRow | None:
    """Load one user by id, including disabled users.

    Returns None when no row matches. Does not commit.
    """
    row = connection.execute(
        """
        SELECT id, email, name, password_hash, roles_json, created_at, disabled_at
          FROM users
         WHERE id = ?
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return _from_row(row)


def find_enabled_user_by_id(connection: Connection, user_id: int) -> UserRow | None:
    """Load an enabled user by id.

    Returns None when the id is missing or disabled_at is set. Does not
    commit.
    """
    row = connection.execute(
        """
        SELECT id, email, name, password_hash, roles_json, created_at, disabled_at
          FROM users
         WHERE id = ? AND disabled_at IS NULL
        """,
        (user_id,),
    ).fetchone()
    if row is None:
        return None
    return _from_row(row)


def find_user_by_email(connection: Connection, email: str) -> UserRow | None:
    """Load a user by email, including a disabled row.

    Returns None when the email is missing. Does not commit.
    """
    row = connection.execute(
        """
        SELECT id, email, name, password_hash, roles_json, created_at, disabled_at
          FROM users
         WHERE email = ?
        """,
        (email,),
    ).fetchone()
    if row is None:
        return None
    return _from_row(row)


def find_enabled_user_by_email(connection: Connection, email: str) -> UserRow | None:
    """Load an enabled user by email.

    Returns None when the email is missing or disabled_at is set. Does not
    commit.
    """
    row = connection.execute(
        """
        SELECT id, email, name, password_hash, roles_json, created_at, disabled_at
          FROM users
         WHERE email = ? AND disabled_at IS NULL
        """,
        (email,),
    ).fetchone()
    if row is None:
        return None
    return _from_row(row)


def list_users(
    connection: Connection,
    after_id: int | None,
    limit: int,
    search: str | None,
) -> list[User]:
    """Return users ordered by id, at most limit rows.

    after_id excludes that id and every smaller id. None starts at the
    first. search None returns every row in that window. A string keeps
    rows whose name or email contains it. Does not commit and does not
    include password_hash.
    """
    pattern = None if search is None else _like_contains(search)
    if pattern is None and after_id is None:
        rows = connection.execute(
            """
            SELECT id, email, name, roles_json, created_at, disabled_at
              FROM users
             ORDER BY id
             LIMIT ?
            """,
            (limit,),
        ).fetchall()
    elif pattern is None:
        rows = connection.execute(
            """
            SELECT id, email, name, roles_json, created_at, disabled_at
              FROM users
             WHERE id > ?
             ORDER BY id
             LIMIT ?
            """,
            (after_id, limit),
        ).fetchall()
    elif after_id is None:
        rows = connection.execute(
            """
            SELECT id, email, name, roles_json, created_at, disabled_at
              FROM users
             WHERE name LIKE ? ESCAPE '!' OR email LIKE ? ESCAPE '!'
             ORDER BY id
             LIMIT ?
            """,
            (pattern, pattern, limit),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT id, email, name, roles_json, created_at, disabled_at
              FROM users
             WHERE id > ?
               AND (name LIKE ? ESCAPE '!' OR email LIKE ? ESCAPE '!')
             ORDER BY id
             LIMIT ?
            """,
            (after_id, pattern, pattern, limit),
        ).fetchall()
    return [_public_user(row) for row in rows]


def _like_contains(value: str) -> str:
    """Return a LIKE pattern that matches value as a substring.

    ! is the escape character on both engines. % and _ in value match those
    characters rather than wildcards.
    """
    escaped = value.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return "%" + escaped + "%"


def _public_user(row: object) -> User:
    """Map a list row to User without the password hash."""
    disabled_at = row["disabled_at"]
    return User(
        id=int(row["id"]),
        email=str(row["email"]),
        name=str(row["name"]),
        roles=_parse_roles(str(row["roles_json"])),
        created_at=str(row["created_at"]),
        disabled_at=None if disabled_at is None else str(disabled_at),
    )


def _from_row(row: object) -> UserRow:
    """Map one SQL row to UserRow.

    Raises StorageError when roles_json is not a valid role list.
    """
    password_hash = row["password_hash"]
    disabled_at = row["disabled_at"]
    return UserRow(
        id=int(row["id"]),
        email=str(row["email"]),
        name=str(row["name"]),
        password_hash=None if password_hash is None else str(password_hash),
        roles=_parse_roles(str(row["roles_json"])),
        created_at=str(row["created_at"]),
        disabled_at=None if disabled_at is None else str(disabled_at),
    )


def _roles_json(roles: tuple[str, ...]) -> str:
    """Serialize roles for storage."""
    return json.dumps(list(roles), separators=(",", ":"))


def _parse_roles(roles_json: str) -> tuple[str, ...]:
    """Return the role tuple stored in roles_json.

    Raises StorageError when the JSON is not a list of allowed role names.
    """
    try:
        parsed = json.loads(roles_json)
    except json.JSONDecodeError as exc:
        raise StorageError() from exc
    if not isinstance(parsed, list):
        raise StorageError()
    roles: list[str] = []
    for item in parsed:
        if not isinstance(item, str) or item not in ALLOWED_ROLES:
            raise StorageError()
        roles.append(item)
    return tuple(roles)
