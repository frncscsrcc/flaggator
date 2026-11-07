"""Create, fetch, list, and patch users, and issue local login JWTs.

Passwords never appear in audit JSON or API responses.
"""

import json
import time

from domain.clock import utc_timestamp
from domain.errors import (
    Conflict,
    ConstraintViolation,
    Forbidden,
    NotFound,
    Unauthenticated,
    ValidationError,
)
from domain.jwt import encode_hs256
from domain.models.principal import (
    ALLOWED_ROLES,
    KIND_ANONYMOUS,
    KIND_USER,
    ROLE_ADMIN,
    ROLE_PROTECTED_WRITER,
    Principal,
)
from domain.models.users import (
    AccessToken,
    CreateUser,
    LoginCommand,
    PatchUser,
    User,
    UserKeysetQuery,
    UserPage,
)
from domain.passwords import (
    burn_password_check,
    hash_password,
    password_is_checkable,
    require_password,
    verify_password,
)
from domain.services.access import require_admin, require_reader
from domain.services.audit import record_change
from domain.services.authentication import JwtSettings
from domain.validation import require_display_name, require_page_limit
from repositories.db import Connection
from repositories.users import (
    UserRow,
    find_enabled_user_by_email,
    find_user_by_email,
    find_user_by_id,
    insert_user,
    list_users,
    update_user,
)

_LOGIN_MESSAGE = "A valid credential is required."
_MAX_EMAIL_LENGTH = 320
# A stored name is at most 256 characters and an email at most 320, so a
# longer search cannot match either column.
_MAX_SEARCH_LENGTH = 320
_AUDIT_PROJECT = ""


def create_user(
    connection: Connection,
    principal: Principal,
    command: CreateUser,
) -> User:
    """Insert a user and an audit row.

    Raises Forbidden for an SDK token, ValidationError for a bad body, and
    Conflict when email already exists. password None stores a null hash.
    """
    require_admin(principal)
    email = require_email(command.email)
    name = require_display_name(command.name, "name")
    roles = require_roles(command.roles)
    _reject_new_grants(principal, roles, ())
    if command.password is None:
        password_hash = None
    else:
        password_hash = hash_password(require_password(command.password))
    created_at = utc_timestamp()
    with connection.transaction():
        try:
            user_id = insert_user(
                connection,
                email,
                name,
                password_hash,
                roles,
                created_at,
            )
        except ConstraintViolation as exc:
            raise Conflict("User email already exists.") from exc
        record_change(
            connection,
            principal,
            "user.create",
            _AUDIT_PROJECT,
            None,
            None,
            None,
            _user_document(user_id, email, name, roles, created_at, None),
        )
    return User(
        id=user_id,
        email=email,
        name=name,
        roles=roles,
        created_at=created_at,
        disabled_at=None,
    )


def get_user(connection: Connection, principal: Principal, user_id: int) -> User:
    """Return one user by id.

    Raises Forbidden for an SDK token and NotFound when missing.
    """
    require_reader(principal)
    row = find_user_by_id(connection, user_id)
    if row is None:
        raise NotFound("User does not exist.")
    return _to_user(row)


def list_users_page(
    connection: Connection,
    principal: Principal,
    query: UserKeysetQuery,
) -> UserPage:
    """Return one page of users ordered by id.

    search keeps users whose name or email contains that text. Raises
    Forbidden for an SDK token and ValidationError for a bad limit or search.
    """
    require_reader(principal)
    limit = require_page_limit(query.limit)
    search = _require_search(query.search)
    rows = list_users(connection, query.after_id, limit + 1, search)
    visible = rows[:limit]
    if len(rows) <= limit:
        next_id = None
    else:
        next_id = visible[-1].id
    return UserPage(items=visible, next_id=next_id)


def patch_user(
    connection: Connection,
    principal: Principal,
    user_id: int,
    command: PatchUser,
) -> User:
    """Update name, password, roles, and/or disabled for user_id.

    Raises Forbidden for an SDK token, NotFound when missing, and
    ValidationError for a bad body. Omitted fields stay unchanged.
    """
    require_admin(principal)
    name = None
    if command.name is not None:
        name = require_display_name(command.name, "name")
    roles = None
    if command.roles is not None:
        roles = require_roles(command.roles)
    new_password_hash: str | None = None
    replace_password = False
    if command.password is not None:
        new_password_hash = hash_password(require_password(command.password))
        replace_password = True
    with connection.transaction():
        current = find_user_by_id(connection, user_id)
        if current is None:
            raise NotFound("User does not exist.")
        next_name = current.name if name is None else name
        next_roles = current.roles if roles is None else roles
        _reject_new_grants(principal, next_roles, current.roles)
        next_hash = new_password_hash if replace_password else current.password_hash
        if command.disabled is None:
            next_disabled = current.disabled_at
        elif command.disabled:
            next_disabled = current.disabled_at if current.disabled_at is not None else utc_timestamp()
        else:
            next_disabled = None
        before = _user_document(
            current.id,
            current.email,
            current.name,
            current.roles,
            current.created_at,
            current.disabled_at,
        )
        update_user(connection, current.id, next_name, next_hash, next_roles, next_disabled)
        after = _user_document(
            current.id,
            current.email,
            next_name,
            next_roles,
            current.created_at,
            next_disabled,
        )
        if before != after:
            record_change(
                connection,
                principal,
                "user.patch",
                _AUDIT_PROJECT,
                None,
                None,
                before,
                after,
            )
    return User(
        id=current.id,
        email=current.email,
        name=next_name,
        roles=next_roles,
        created_at=current.created_at,
        disabled_at=next_disabled,
    )


def login(
    connection: Connection,
    command: LoginCommand,
    jwt_settings: JwtSettings,
    client_address: str,
) -> AccessToken:
    """Verify email and password and return a local access JWT.

    Wrong email, wrong password, disabled user, IdP-only user, and missing
    JWT secret all raise the same Unauthenticated message. Every outcome
    writes one audit row, so the write itself does not say whether the
    account exists. The submitted text is stored only as a user id, and
    only when that email is a user. client_address is the rate-limit
    address. Does not log the password.
    """
    if jwt_settings.secret == "":
        _commit_login_audit(connection, None, client_address, False)
        raise Unauthenticated(_LOGIN_MESSAGE)
    try:
        email = require_email(command.email)
    except ValidationError:
        email = None
    user = None
    if email is not None:
        user = find_user_by_email(connection, email)
    accepted = (
        _password_matches(command.password, user)
        if email is not None and password_is_checkable(command.password)
        else False
    )
    _commit_login_audit(connection, user, client_address, accepted)
    if not accepted or user is None:
        raise Unauthenticated(_LOGIN_MESSAGE)
    now = int(time.time())
    expires_in = jwt_settings.ttl_seconds
    token = encode_hs256(
        {
            "sub": str(user.id),
            "roles": list(user.roles),
            "iss": jwt_settings.issuer,
            "aud": jwt_settings.audience,
            "iat": now,
            "exp": now + expires_in,
        },
        jwt_settings.secret,
    )
    return AccessToken(
        access_token=token,
        token_type="bearer",
        expires_in=expires_in,
    )


def _password_matches(password: str, user: UserRow | None) -> bool:
    """Return True when password matches an enabled user that has a hash.

    The caller has already checked that password is long enough to verify
    and that the email was well formed. A missing user, a disabled user,
    and an IdP-only user burn one scrypt check and return False. Does not
    write.
    """
    if user is None or user.password_hash is None or user.disabled_at is not None:
        burn_password_check(password)
        return False
    return verify_password(password, user.password_hash)


def _commit_login_audit(
    connection: Connection,
    user: UserRow | None,
    client_address: str,
    accepted: bool,
) -> None:
    """Append the login row and commit it.

    accepted selects user.login and the user as actor. A failure is
    user.login_failed with an anonymous actor. The document has the client
    address and, when user is not None, that user's id. Does not store the
    password or the submitted email text.
    """
    after: dict[str, object] = {"client_address": client_address}
    if user is not None:
        after["user_id"] = user.id
    if accepted and user is not None:
        actor = Principal(
            credential_id=user.id,
            kind=KIND_USER,
            environment_id=None,
            prefix="",
            roles=user.roles,
        )
        action = "user.login"
    else:
        actor = Principal(
            credential_id=0,
            kind=KIND_ANONYMOUS,
            environment_id=None,
            prefix="",
            roles=(),
        )
        action = "user.login_failed"
    document = json.dumps(after, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    with connection.transaction():
        record_change(
            connection,
            actor,
            action,
            _AUDIT_PROJECT,
            None,
            None,
            None,
            document,
        )


def require_email(value: str) -> str:
    """Return a normalized email address.

    Lowercases the value. Raises ValidationError when empty, too long, or
    missing '@'. Does not read the database.
    """
    if value == "" or value != value.strip():
        raise ValidationError(
            "The email is not valid.",
            details=[
                {
                    "field": "email",
                    "message": "Email must not be empty or have surrounding spaces.",
                }
            ],
        )
    normalized = value.strip().lower()
    if "@" not in normalized or normalized.startswith("@") or normalized.endswith("@"):
        raise ValidationError(
            "The email is not valid.",
            details=[{"field": "email", "message": "Email must contain a local part and a domain."}],
        )
    if len(normalized) > _MAX_EMAIL_LENGTH:
        raise ValidationError(
            "The email is not valid.",
            details=[
                {
                    "field": "email",
                    "message": f"Email must be at most {_MAX_EMAIL_LENGTH} characters.",
                }
            ],
        )
    return normalized


def require_roles(roles: tuple[str, ...]) -> tuple[str, ...]:
    """Return roles when every entry is allowed and the list is not empty.

    Duplicates are rejected. Does not read the database.
    """
    if len(roles) == 0:
        raise ValidationError(
            "The roles are not valid.",
            details=[{"field": "roles", "message": "Send at least one role."}],
        )
    seen: set[str] = set()
    ordered: list[str] = []
    for role in roles:
        if role not in ALLOWED_ROLES:
            raise ValidationError(
                "The roles are not valid.",
                details=[
                    {
                        "field": "roles",
                        "message": "Each role must be reader, writer, protected_writer, or admin.",
                    }
                ],
            )
        if role in seen:
            raise ValidationError(
                "The roles are not valid.",
                details=[{"field": "roles", "message": "Roles must not contain duplicates."}],
            )
        seen.add(role)
        ordered.append(role)
    return tuple(ordered)


def _require_search(search: str | None) -> str | None:
    """Return search when it can be a substring filter.

    None means the list is not filtered. Raises ValidationError when the
    text is longer than _MAX_SEARCH_LENGTH. Does not read the database.
    """
    if search is None:
        return None
    if len(search) > _MAX_SEARCH_LENGTH:
        raise ValidationError(
            "The search is not valid.",
            details=[
                {
                    "field": "search",
                    "message": f"Search must be at most {_MAX_SEARCH_LENGTH} characters.",
                }
            ],
        )
    return search


def _to_user(row: UserRow) -> User:
    """Map a UserRow to the public User shape."""
    return User(
        id=row.id,
        email=row.email,
        name=row.name,
        roles=row.roles,
        created_at=row.created_at,
        disabled_at=row.disabled_at,
    )


def _reject_new_grants(
    principal: Principal,
    roles: tuple[str, ...],
    already: tuple[str, ...],
) -> None:
    """Raise Forbidden when roles newly includes a role the caller lacks.

    already is the roles the user already holds. An empty tuple means a
    create. An admin may grant every role. Anyone else may grant
    protected_writer or admin only when they already hold that role.
    Removing either role is allowed. Does not read the database.
    """
    if ROLE_ADMIN in principal.roles:
        return
    gated = (
        (ROLE_PROTECTED_WRITER, "A protected_writer role is required to grant protected_writer."),
        (ROLE_ADMIN, "An admin role is required to grant admin."),
    )
    for role, message in gated:
        if role not in roles:
            continue
        if role in already:
            continue
        if role in principal.roles:
            continue
        raise Forbidden(message)


def _user_document(
    user_id: int,
    email: str,
    name: str,
    roles: tuple[str, ...],
    created_at: str,
    disabled_at: str | None,
) -> str:
    """Return audit JSON for a user without a password."""
    return json.dumps(
        {
            "id": user_id,
            "email": email,
            "name": name,
            "roles": list(roles),
            "created_at": created_at,
            "disabled_at": disabled_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
