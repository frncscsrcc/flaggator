"""Audit rows for mutations.

The helper inserts one row and does not commit. The service that performs
the mutation calls it inside the same transaction.
"""

from domain.models.principal import Principal
from repositories.db import Connection


def append_event(
    connection: Connection,
    principal: Principal,
    at: str,
    action: str,
    project_key: str,
    environment_key: str | None,
    flag_key: str | None,
    before_json: str | None,
    after_json: str | None,
) -> None:
    """Insert one audit event.

    actor is principal's id and kind, never a secret. before_json and
    after_json are stored as given; the caller must not put a credential
    secret in them. Does not commit and does not check authorization.
    """
    connection.execute(
        """
        INSERT INTO audit_events (
            at, actor_kind, actor_id, action, project_key,
            environment_key, flag_key, before_json, after_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            at,
            principal.kind,
            principal.credential_id,
            action,
            project_key,
            environment_key,
            flag_key,
            before_json,
            after_json,
        ),
    )
