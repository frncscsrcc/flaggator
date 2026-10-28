"""Record a mutation in the audit log.

The use-case service calls this inside its transaction, after authorization.
"""

from domain.clock import utc_timestamp
from domain.models.principal import Principal
from repositories.audit import append_event
from repositories.db import Connection


def record_change(
    connection: Connection,
    principal: Principal,
    action: str,
    project_key: str,
    environment_key: str | None,
    flag_key: str | None,
    before_json: str | None,
    after_json: str | None,
) -> None:
    """Append one audit row for a change principal is making.

    Does not commit. before_json and after_json are stored unchanged. The
    caller must not include a credential secret in either document. This
    function does not check that principal may perform action.
    """
    append_event(
        connection,
        principal,
        utc_timestamp(),
        action,
        project_key,
        environment_key,
        flag_key,
        before_json,
        after_json,
    )
