"""Authorization shared by management use cases.

The adapter already refuses an SDK key on a management path. The service
repeats the check so the rule is visible on the use case and does not depend
on HTTP.
"""

from domain.errors import Forbidden
from domain.models.principal import KIND_MANAGEMENT, Principal


def require_management(principal: Principal) -> None:
    """Raise Forbidden unless principal is a management token.

    Does not read the database and does not commit.
    """
    if principal.kind != KIND_MANAGEMENT:
        raise Forbidden("A management token is required.")
