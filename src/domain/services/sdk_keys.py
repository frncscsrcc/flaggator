"""Issue, list, and revoke SDK keys for one environment.

The plaintext secret is returned once. The audit row stores the id, the
name, and the prefix, never the secret.
"""

import json
import secrets

from domain.clock import utc_timestamp
from domain.errors import Conflict, NotFound
from domain.models.principal import KIND_SDK, Principal
from domain.services.access import require_management
from domain.services.audit import record_change
from domain.validation import is_public_key, require_display_name
from repositories.credentials import (
    SdkKeyRow,
    find_active_sdk_key_by_name,
    insert_credential,
    list_sdk_keys,
    prefix_of,
    revoke_sdk_key,
)
from repositories.db import Connection
from repositories.environments import EnvironmentRow, find_environment
from repositories.projects import ProjectRow, find_project

_TOKEN_BYTES = 32


class IssuedSdkKey:
    """The one response that contains an SDK secret.

    secret is the plaintext token. name is the operator label. prefix is
    the non-secret start of the secret. Later reads return name and prefix
    and not secret.
    """

    def __init__(
        self,
        credential_id: int,
        name: str,
        prefix: str,
        secret: str,
        created_at: str,
    ) -> None:
        """Store the values the create response shows. Does not write them."""
        self.id = credential_id
        self.name = name
        self.prefix = prefix
        self.secret = secret
        self.created_at = created_at


def issue_sdk_key(
    connection: Connection,
    principal: Principal,
    project_key: str,
    environment_key: str,
    name: str,
) -> IssuedSdkKey:
    """Create an SDK key for one environment and return the secret once.

    name is the operator label shown in the list. Raises Forbidden for an
    SDK key, NotFound when the project or the environment is absent,
    ValidationError for a bad name, and Conflict when an active key already
    has that name in the environment. The audit document has the id, the
    name, and the prefix, not the secret. Does not print the secret.
    """
    require_management(principal)
    label = require_display_name(name, "name")
    with connection.transaction():
        project, environment = _require_scope(connection, project_key, environment_key)
        if find_active_sdk_key_by_name(connection, environment.id, label) is not None:
            raise Conflict("SDK key name already exists.")
        created_at = utc_timestamp()
        secret = secrets.token_urlsafe(_TOKEN_BYTES)
        credential_id = insert_credential(
            connection,
            kind=KIND_SDK,
            token=secret,
            environment_id=environment.id,
            created_at=created_at,
            name=label,
        )
        issued = IssuedSdkKey(
            credential_id,
            label,
            prefix_of(secret),
            secret,
            created_at,
        )
        record_change(
            connection,
            principal,
            "sdk_key.create",
            project.key,
            environment.key,
            None,
            None,
            _audit_document(issued.id, issued.name, issued.prefix),
        )
    return issued


def list_sdk_key_page(
    connection: Connection,
    principal: Principal,
    project_key: str,
    environment_key: str,
) -> list[SdkKeyRow]:
    """Return the active SDK keys for one environment.

    Raises Forbidden for an SDK key, and NotFound when the project or the
    environment is absent. An environment with no keys returns an empty
    list. Secrets are not loaded. Does not open a transaction.
    """
    require_management(principal)
    _project, environment = _require_scope(connection, project_key, environment_key)
    return list_sdk_keys(connection, environment.id)


def revoke_sdk_key_now(
    connection: Connection,
    principal: Principal,
    project_key: str,
    environment_key: str,
    credential_id: int,
) -> None:
    """Revoke one SDK key so the next evaluation with it is rejected.

    Raises Forbidden for an SDK key. Raises NotFound when the project, the
    environment, or the key is absent, including when the key is already
    revoked. The audit document does not contain a secret.
    """
    require_management(principal)
    with connection.transaction():
        project, environment = _require_scope(connection, project_key, environment_key)
        rows = list_sdk_keys(connection, environment.id)
        name = ""
        prefix = ""
        found = False
        for row in rows:
            if row.id == credential_id:
                name = row.name
                prefix = row.prefix
                found = True
        if not found:
            raise NotFound("SDK key does not exist.")
        changed = revoke_sdk_key(connection, credential_id, environment.id, utc_timestamp())
        if changed == 0:
            raise NotFound("SDK key does not exist.")
        record_change(
            connection,
            principal,
            "sdk_key.revoke",
            project.key,
            environment.key,
            None,
            _audit_document(credential_id, name, prefix),
            _audit_document(credential_id, name, prefix),
        )


def _require_scope(
    connection: Connection,
    project_key: str,
    environment_key: str,
) -> tuple[ProjectRow, EnvironmentRow]:
    """Return the project row and the environment row.

    Raises NotFound when either key cannot exist or no row matches, with
    one message per resource. Does not commit.
    """
    if not is_public_key(project_key):
        raise NotFound("Project does not exist.")
    project = find_project(connection, project_key)
    if project is None:
        raise NotFound("Project does not exist.")
    if not is_public_key(environment_key):
        raise NotFound("Environment does not exist.")
    environment = find_environment(connection, project.id, environment_key)
    if environment is None:
        raise NotFound("Environment does not exist.")
    return project, environment


def _audit_document(credential_id: int, name: str, prefix: str) -> str:
    """Return the audit JSON for an SDK key.

    The document has the id, the name, and the prefix. It does not have
    the secret or the hash.
    """
    return json.dumps(
        {"id": credential_id, "name": name, "prefix": prefix},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
