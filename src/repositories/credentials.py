"""Credential rows.

This module hashes tokens and maps rows. It does not decide whether a
principal may call a route, and it does not commit.
"""

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass

from domain.models.principal import KIND_SDK, Principal
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
    name: str | None = None,
) -> int:
    """Insert a credential and return its integer id.

    Stores the hash and the prefix, never token. environment_id is None for
    a management token. name is the operator label for an SDK token, or None
    for a management token. Does not commit and does not write an audit row.
    """
    cursor = connection.execute(
        """
        INSERT INTO api_credentials (
            kind, name, secret_hash, prefix, environment_id, created_at, revoked_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (kind, name, hash_token(token), prefix_of(token), environment_id, created_at, None),
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


@dataclass(frozen=True)
class SdkTokenRow:
    """One active SDK token as a list shows it.

    The secret and the hash are absent. name is the operator label. prefix
    is the non-secret start of the secret.
    """

    id: int
    name: str
    prefix: str
    created_at: str


def list_sdk_tokens(connection: Connection, environment_id: int) -> list[SdkTokenRow]:
    """Return active SDK tokens for one environment, ordered by id.

    Revoked rows are absent. An environment with no tokens returns an empty
    list. Does not commit and does not return a secret.
    """
    rows = connection.execute(
        """
        SELECT id, name, prefix, created_at
          FROM api_credentials
         WHERE kind = ? AND environment_id = ? AND revoked_at IS NULL
         ORDER BY id
        """,
        (KIND_SDK, environment_id),
    ).fetchall()
    return [
        SdkTokenRow(
            id=int(row["id"]),
            name=str(row["name"]),
            prefix=str(row["prefix"]),
            created_at=str(row["created_at"]),
        )
        for row in rows
    ]


def find_active_sdk_token_by_name(
    connection: Connection,
    environment_id: int,
    name: str,
) -> SdkTokenRow | None:
    """Return the active SDK token with this name in the environment.

    Returns None when no active token has that name. Does not commit.
    """
    row = connection.execute(
        """
        SELECT id, name, prefix, created_at
          FROM api_credentials
         WHERE kind = ?
           AND environment_id = ?
           AND name = ?
           AND revoked_at IS NULL
        """,
        (KIND_SDK, environment_id, name),
    ).fetchone()
    if row is None:
        return None
    return SdkTokenRow(
        id=int(row["id"]),
        name=str(row["name"]),
        prefix=str(row["prefix"]),
        created_at=str(row["created_at"]),
    )


def revoke_sdk_token(
    connection: Connection,
    credential_id: int,
    environment_id: int,
    revoked_at: str,
) -> int:
    """Revoke one active SDK token in environment_id.

    credential_id is the integer id from the list. Returns how many rows
    changed. Zero means the id is missing, revoked, or not an SDK token of
    this environment. Does not commit.
    """
    cursor = connection.execute(
        """
        UPDATE api_credentials
           SET revoked_at = ?
         WHERE id = ?
           AND kind = ?
           AND environment_id = ?
           AND revoked_at IS NULL
        """,
        (revoked_at, credential_id, KIND_SDK, environment_id),
    )
    return cursor.rowcount


def _stored_hash(row: Mapping[str, object] | None) -> str:
    """Return the hash to compare, or a fixed dummy when row is missing.

    The dummy has the same length as a real digest so compare_digest does
    not reject it for length. It is not a credential.
    """
    if row is None:
        return _DUMMY_HASH
    return str(row["secret_hash"])
