"""JWT encode and verify via PyJWT.

Local access tokens use HS256 and FLAGGATOR_JWT_SECRET. External IdP tokens
use JWKS (PyJWT crypto extra / cryptography) when FLAGGATOR_JWT_JWKS_URL is
set. Does not read the database and does not log the token or the secret.
"""

from typing import Any

import jwt
from jwt import PyJWKClient

from domain.errors import Unauthenticated

_MESSAGE = "A valid credential is required."
_JWKS_ALGORITHMS = ("RS256", "RS384", "RS512", "ES256", "ES384", "ES512")
_JWKS_TIMEOUT_SECONDS = 5
_jwks_clients: dict[str, PyJWKClient] = {}


def encode_hs256(
    claims: dict[str, Any],
    secret: str,
) -> str:
    """Return a compact HS256 JWT for claims.

    secret must be non-empty. Does not check iss/aud/exp. Does not log
    secret or the token.
    """
    if secret == "":
        raise ValueError("A JWT signing secret is required.")
    return jwt.encode(claims, secret, algorithm="HS256")


def verify_access_token(
    token: str,
    secret: str,
    issuer: str,
    audience: str,
    jwks_url: str,
    now: int | None = None,
) -> tuple[dict[str, Any], str]:
    """Verify a local HS256 or IdP JWKS-signed JWT.

    Returns the payload and the algorithm that verified it. Chooses the
    path from the unprotected header alg. HS256 needs secret. RS*/ES*
    need a non-empty jwks_url. Raises Unauthenticated on any failure.
    Does not log token or secret.
    """
    if not looks_like_jwt(token):
        raise Unauthenticated(_MESSAGE)
    try:
        header = jwt.get_unverified_header(token)
    except jwt.PyJWTError as exc:
        raise Unauthenticated(_MESSAGE) from exc
    algorithm = header.get("alg")
    if algorithm == "HS256":
        return verify_hs256(token, secret, issuer, audience, now=now), "HS256"
    if algorithm in _JWKS_ALGORITHMS and jwks_url != "":
        return verify_jwks(token, jwks_url, issuer, audience, now=now), str(algorithm)
    raise Unauthenticated(_MESSAGE)


def verify_hs256(
    token: str,
    secret: str,
    issuer: str,
    audience: str,
    now: int | None = None,
) -> dict[str, Any]:
    """Return the payload when token is a valid HS256 JWT for this process.

    Checks signature, iss, aud, and exp (algorithms locked to HS256). Raises
    Unauthenticated with the same message as a bad opaque token on any
    failure. An empty secret rejects every token. Does not log token or
    secret.
    """
    if secret == "" or not looks_like_jwt(token):
        raise Unauthenticated(_MESSAGE)
    return _decode(
        token,
        secret,
        algorithms=["HS256"],
        issuer=issuer,
        audience=audience,
        now=now,
    )


def verify_jwks(
    token: str,
    jwks_url: str,
    issuer: str,
    audience: str,
    now: int | None = None,
) -> dict[str, Any]:
    """Return the payload when token verifies against the IdP JWKS URL.

    Fetches signing keys via PyJWKClient (cached per URL). Raises
    Unauthenticated on any failure. Does not log token.
    """
    if jwks_url == "" or not looks_like_jwt(token):
        raise Unauthenticated(_MESSAGE)
    try:
        signing_key = _jwks_client(jwks_url).get_signing_key_from_jwt(token)
    except Exception as exc:
        raise Unauthenticated(_MESSAGE) from exc
    return _decode(
        token,
        signing_key.key,
        algorithms=list(_JWKS_ALGORITHMS),
        issuer=issuer,
        audience=audience,
        now=now,
    )


def looks_like_jwt(token: str) -> bool:
    """Return True when token has the three-segment JWT shape.

    Does not verify the token. Does not log token.
    """
    parts = token.split(".")
    return len(parts) == 3 and all(part != "" for part in parts)


def _decode(
    token: str,
    key: Any,
    algorithms: list[str],
    issuer: str,
    audience: str,
    now: int | None,
) -> dict[str, Any]:
    """Decode and validate token with key.

    Raises Unauthenticated on failure.
    """
    options: dict[str, Any] = {
        "require": ["exp", "iss", "aud"],
    }
    if now is not None:
        options["verify_exp"] = False
    try:
        payload = jwt.decode(
            token,
            key,
            algorithms=algorithms,
            audience=audience,
            issuer=issuer,
            options=options,
        )
    except jwt.PyJWTError as exc:
        raise Unauthenticated(_MESSAGE) from exc
    if not isinstance(payload, dict):
        raise Unauthenticated(_MESSAGE)
    if now is not None:
        exp = payload.get("exp")
        if not isinstance(exp, int) or now >= exp:
            raise Unauthenticated(_MESSAGE)
    return payload


def _jwks_client(url: str) -> PyJWKClient:
    """Return a process-cached PyJWKClient for url."""
    client = _jwks_clients.get(url)
    if client is None:
        client = PyJWKClient(url, cache_keys=True, timeout=_JWKS_TIMEOUT_SECONDS)
        _jwks_clients[url] = client
    return client
