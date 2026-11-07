"""Controllers for users and local login.

Login does not require a bearer token. User routes use a management
principal. Passwords never appear in responses.
"""

import base64

from api.body import read_json_object
from api.paging import page_body
from api.request import Request, Response
from domain.errors import NotFound, ValidationError
from domain.models.principal import Principal
from domain.models.users import CreateUser, LoginCommand, PatchUser, User, UserKeysetQuery
from domain.services import users as user_service
from domain.services.authentication import JwtSettings
from domain.validation import DEFAULT_PAGE_LIMIT, require_page_limit
from repositories.db import Connection

_CURSOR_PREFIX = "v1:"
_CURSOR_MESSAGE = "Send the cursor from a previous page."
_PATCH_FIELDS = ("name", "password", "roles", "disabled")


def logout(principal: Principal, connection: Connection) -> Response:
    """End the caller's local sessions.

    Returns 204. A management or SDK principal is Forbidden. Does not
    commit outside the service transaction.
    """
    user_service.logout(connection, principal)
    return Response(status=204, body={})


def login(connection: Connection, request: Request, jwt_settings: JwtSettings) -> Response:
    """Issue a local access JWT from email and password.

    Returns 200. Does not require a principal. Domain failures propagate.
    """
    token = user_service.login(
        connection,
        _login_command(request),
        jwt_settings,
        request.client_address,
    )
    return Response(
        status=200,
        body={
            "access_token": token.access_token,
            "token_type": token.token_type,
            "expires_in": token.expires_in,
        },
    )


def create_user(
    principal: Principal,
    connection: Connection,
    request: Request,
) -> Response:
    """Create a user from the JSON body.

    Returns 201 without a password. Domain failures propagate.
    """
    user = user_service.create_user(connection, principal, _create_command(request))
    return Response(status=201, body=_user_document(user))


def list_users(
    principal: Principal,
    connection: Connection,
    request: Request,
) -> Response:
    """List users for the query limit, cursor, and optional search.

    Returns 200. next_cursor is null on the last page. search matches a
    substring of name or email, and the cursor continues that same search.
    """
    page = user_service.list_users_page(connection, principal, _page_request(request))
    items = [_user_document(item) for item in page.items]
    if page.next_id is None:
        next_key = None
    else:
        next_key = str(page.next_id)
    return Response(status=200, body=page_body(items, next_key))


def get_user(
    principal: Principal,
    connection: Connection,
    user_id: str,
) -> Response:
    """Fetch one user by decimal id.

    Returns 200. A non-decimal id is the same 404 as a missing user.
    """
    parsed = _parse_user_id(user_id)
    user = user_service.get_user(connection, principal, parsed)
    return Response(status=200, body=_user_document(user))


def patch_user(
    principal: Principal,
    connection: Connection,
    request: Request,
    user_id: str,
) -> Response:
    """Patch one user by decimal id.

    Returns 200. A non-decimal id is the same 404 as a missing user.
    """
    parsed = _parse_user_id(user_id)
    user = user_service.patch_user(
        connection,
        principal,
        parsed,
        _patch_command(request),
    )
    return Response(status=200, body=_user_document(user))


def _login_command(request: Request) -> LoginCommand:
    """Build a login command from the JSON body."""
    body = read_json_object(request)
    details: list[dict[str, str]] = []
    for field in ("email", "password"):
        if field not in body:
            details.append({"field": field, "message": "This field is required."})
        elif not isinstance(body[field], str):
            details.append({"field": field, "message": "This field must be a string."})
    for field in sorted(set(body) - {"email", "password"}):
        details.append({"field": field, "message": "This field is not accepted."})
    if details:
        raise ValidationError("The request body has the wrong fields.", details=details)
    return LoginCommand(email=body["email"], password=body["password"])


def _create_command(request: Request) -> CreateUser:
    """Build a create command from the JSON body.

    password may be omitted for an IdP-only user.
    """
    body = read_json_object(request)
    details: list[dict[str, str]] = []
    for field in ("email", "name"):
        if field not in body:
            details.append({"field": field, "message": "This field is required."})
        elif not isinstance(body[field], str):
            details.append({"field": field, "message": "This field must be a string."})
    if "roles" not in body:
        details.append({"field": "roles", "message": "This field is required."})
    elif not isinstance(body["roles"], list) or not all(isinstance(item, str) for item in body["roles"]):
        details.append({"field": "roles", "message": "This field must be a list of strings."})
    password: str | None
    if "password" not in body:
        password = None
    elif not isinstance(body["password"], str):
        details.append({"field": "password", "message": "This field must be a string."})
        password = None
    else:
        password = body["password"]
    for field in sorted(set(body) - {"email", "name", "password", "roles"}):
        details.append({"field": field, "message": "This field is not accepted."})
    if details:
        raise ValidationError("The request body has the wrong fields.", details=details)
    return CreateUser(
        email=body["email"],
        name=body["name"],
        password=password,
        roles=tuple(body["roles"]),
    )


def _patch_command(request: Request) -> PatchUser:
    """Build a patch command from the JSON body."""
    body = read_json_object(request)
    details: list[dict[str, str]] = []
    if "email" in body:
        details.append({"field": "email", "message": "The email cannot be changed."})
    if not any(field in body for field in _PATCH_FIELDS):
        details.append(
            {
                "field": "body",
                "message": "Send a name, password, roles, or disabled.",
            }
        )
    if "name" in body and not isinstance(body["name"], str):
        details.append({"field": "name", "message": "This field must be a string."})
    if "password" in body and not isinstance(body["password"], str):
        details.append({"field": "password", "message": "This field must be a string."})
    if "roles" in body and (
        not isinstance(body["roles"], list) or not all(isinstance(item, str) for item in body["roles"])
    ):
        details.append({"field": "roles", "message": "This field must be a list of strings."})
    if "disabled" in body and not isinstance(body["disabled"], bool):
        details.append({"field": "disabled", "message": "This field must be a boolean."})
    for field in sorted(set(body) - set(_PATCH_FIELDS) - {"email"}):
        details.append({"field": field, "message": "This field is not accepted."})
    if details:
        raise ValidationError("The request body has the wrong fields.", details=details)
    name = body["name"] if "name" in body else None
    password = body["password"] if "password" in body else None
    roles = tuple(body["roles"]) if "roles" in body else None
    disabled = body["disabled"] if "disabled" in body else None
    return PatchUser(name=name, password=password, roles=roles, disabled=disabled)


def _page_request(request: Request) -> UserKeysetQuery:
    """Read limit, id cursor, and search for the users list."""
    raw_limit = request.query.get("limit")
    if raw_limit is None:
        limit = DEFAULT_PAGE_LIMIT
    elif not raw_limit.isdigit():
        raise ValidationError(
            "The list limit is not valid.",
            details=[{"field": "limit", "message": "Limit must be a whole number from 1 to 200."}],
        )
    else:
        limit = require_page_limit(int(raw_limit))
    return UserKeysetQuery(
        limit=limit,
        after_id=_parse_id_cursor(request.query.get("cursor")),
        search=_search_text(request.query.get("search")),
    )


def _search_text(raw: str | None) -> str | None:
    """Return the search text, or None when the parameter is absent or empty."""
    if raw is None or raw == "":
        return None
    return raw


def _parse_id_cursor(raw: str | None) -> int | None:
    """Return the user id inside a list cursor, or None when absent."""
    if raw is None:
        return None
    if raw == "":
        raise ValidationError(
            "The cursor is not valid.",
            details=[{"field": "cursor", "message": _CURSOR_MESSAGE}],
        )
    padded = raw + ("=" * (-len(raw) % 4))
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
    key = text[len(_CURSOR_PREFIX) :]
    if not key.isdigit() or (len(key) > 1 and key[0] == "0"):
        raise ValidationError(
            "The cursor is not valid.",
            details=[{"field": "cursor", "message": _CURSOR_MESSAGE}],
        )
    return int(key)


def _parse_user_id(raw: str) -> int:
    """Return a positive decimal user id.

    A non-decimal value is the same 404 as a missing user.
    """
    if not raw.isdigit() or (len(raw) > 1 and raw[0] == "0"):
        raise NotFound("User does not exist.")
    return int(raw)


def _user_document(user: User) -> dict:
    """Return the JSON object for one user without a password."""
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "roles": list(user.roles),
        "created_at": user.created_at,
        "disabled_at": user.disabled_at,
    }
