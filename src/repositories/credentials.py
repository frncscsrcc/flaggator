"""Credential rows.

This module hashes tokens and maps rows. It does not decide whether a
principal may call a route, and it does not commit.
"""

import hashlib
import hmac
from collections.abc import Mapping

from domain.models.principal import Principal
from repositories.db import Connection

PREFIX_LENGTH = 8
_HASH_LENGTH = 64
_DUMMY_HASH = "0" * _HASH_LENGTH


def hash_token(token: str) -> str:
    """Return the SHA-256 hex digest of token.

    Does not store or log token. The digest is 64 characters, which is the
    width of api_credentials.secret_hash.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def prefix_of(token: str) -> str:
    """Return the non-secret label for token.

    The label is the first PREFIX_LENGTH characters. It is not enough to
    reconstruct the secret. Does not log token.
    """
    return token[:PREFIX_LENGTH]


def insert_credential(
    connection: Connection,
    kind: str,
    token: str,
    environment_id: int | None,
    created_at: str,
) -> int:
    """Insert a credential and return its integer id.

    Stores the hash and the prefix, never token. environment_id is None for
    a management token. Does not commit and does not write an audit row.
    """
    cursor = connection.execute(
        """
        INSERT INTO api_credentials (
            kind, secret_hash, prefix, environment_id, created_at, revoked_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (kind, hash_token(token), prefix_of(token), environment_id, created_at, None),
    )
    return cursor.last_insert_id


def find_active_principal(connection: Connection, token: str) -> Principal | None:
    """Return the principal for token, or None when it does not match.

    A revoked row is not a match. The hash comparison uses
    hmac.compare_digest, including when no row exists, so the compare itself
    is not skipped. Does not commit and does not log token.
    """
    computed = hash_token(token)
    row = connection.execute(
        """
        SELECT id, kind, prefix, environment_id, secret_hash
          FROM api_credentials
         WHERE secret_hash = ? AND revoked_at IS NULL
        """,
        (computed,),
    ).fetchone()
    stored = _stored_hash(row)
    if not hmac.compare_digest(stored, computed):
        return None
    if row is None:
        return None
    environment_id = row["environment_id"]
    if environment_id is None:
        scoped_environment = None
    else:
        scoped_environment = int(environment_id)
    return Principal(
        credential_id=int(row["id"]),
        kind=str(row["kind"]),
        environment_id=scoped_environment,
        prefix=str(row["prefix"]),
    )


def _stored_hash(row: Mapping[str, object] | None) -> str:
    """Return the hash to compare, or a fixed dummy when row is missing.

    The dummy has the same length as a real digest so compare_digest does
    not reject it for length. It is not a credential.
    """
    if row is None:
        return _DUMMY_HASH
    return str(row["secret_hash"])
