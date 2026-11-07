"""Audit rows for mutations.

The helper inserts one row and does not commit. The service that performs
the mutation calls it inside the same transaction. List helpers do not
commit either.
"""

from dataclasses import dataclass

from domain.models.principal import Principal
from repositories.db import Connection


@dataclass(frozen=True)
class AuditEventRow:
    """One audit_events row as stored.

    before_json and after_json stay text until the service parses them.
    """

    id: int
    at: str
    actor_kind: str
    actor_id: int
    action: str
    environment_key: str | None
    flag_key: str | None
    before_json: str | None
    after_json: str | None


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


def list_project_events(
    connection: Connection,
    project_key: str,
    flag_key: str | None,
    environment_key: str | None,
    limit: int,
) -> list[AuditEventRow]:
    """Return audit rows for project_key, newest id first.

    Optional flag_key and environment_key narrow the result. An empty
    project returns an empty list. Does not commit.
    """
    clauses = ["project_key = ?"]
    params: list[object] = [project_key]
    if flag_key is not None:
        clauses.append("flag_key = ?")
        params.append(flag_key)
    if environment_key is not None:
        clauses.append("environment_key = ?")
        params.append(environment_key)
    params.append(limit)
    where = " AND ".join(clauses)
    rows = connection.execute(
        f"""
        SELECT id, at, actor_kind, actor_id, action,
               environment_key, flag_key, before_json, after_json
          FROM audit_events
         WHERE {where}
         ORDER BY id DESC
         LIMIT ?
        """,
        tuple(params),
    ).fetchall()
    return [_row(row) for row in rows]


def list_server_events(
    connection: Connection,
    before_id: int | None,
    limit: int,
) -> list[AuditEventRow]:
    """Return rows whose project key is empty, newest id first.

    before_id None starts at the newest row. Otherwise only ids smaller
    than before_id are returned. Does not commit.
    """
    if before_id is None:
        rows = connection.execute(
            """
            SELECT id, at, actor_kind, actor_id, action,
                   environment_key, flag_key, before_json, after_json
              FROM audit_events
             WHERE project_key = ?
             ORDER BY id DESC
             LIMIT ?
            """,
            ("", limit),
        ).fetchall()
    else:
        rows = connection.execute(
            """
            SELECT id, at, actor_kind, actor_id, action,
                   environment_key, flag_key, before_json, after_json
              FROM audit_events
             WHERE project_key = ? AND id < ?
             ORDER BY id DESC
             LIMIT ?
            """,
            ("", before_id, limit),
        ).fetchall()
    return [_row(row) for row in rows]


def _row(row: object) -> AuditEventRow:
    """Map one SQL row to AuditEventRow."""
    environment_key = row["environment_key"]
    flag_key = row["flag_key"]
    before_json = row["before_json"]
    after_json = row["after_json"]
    return AuditEventRow(
        id=int(row["id"]),
        at=str(row["at"]),
        actor_kind=str(row["actor_kind"]),
        actor_id=int(row["actor_id"]),
        action=str(row["action"]),
        environment_key=None if environment_key is None else str(environment_key),
        flag_key=None if flag_key is None else str(flag_key),
        before_json=None if before_json is None else str(before_json),
        after_json=None if after_json is None else str(after_json),
    )
