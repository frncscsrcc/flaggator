"""Failures the API can render, and storage failures the service translates.

Messages on these errors are safe to show to an operator. They do not include
SQL, file paths, or token contents. Driver detail stays on the exception
chain for stderr.
"""


class DomainError(Exception):
    """A failure with an HTTP status and a stable error code.

    details is empty except when a validation error names a field. The
    message is the sentence the JSON error document shows.
    """

    status = 500
    code = "storage_error"

    def __init__(
        self,
        message: str,
        details: list[dict[str, str]] | None = None,
    ) -> None:
        """Store the operator-facing message.

        details defaults to an empty list. This does not write a response.
        """
        super().__init__(message)
        self.message = message
        if details is None:
            self.details = []
        else:
            self.details = details


class ValidationError(DomainError):
    """The command or the request shape breaks a rule.

    status is 400. The service or the controller raises this before a write
    is committed.
    """

    status = 400
    code = "validation_error"


class Unauthenticated(DomainError):
    """No usable credential was presented.

    status is 401. The same message is used for a missing, unknown, or
    revoked token so the response does not say which of those it was.
    """

    status = 401
    code = "unauthenticated"


class Forbidden(DomainError):
    """The credential is valid and may not perform this action.

    status is 403.
    """

    status = 403
    code = "forbidden"


class NotFound(DomainError):
    """A management read named a project, environment, or flag that is absent.

    status is 404. Evaluation of a missing flag does not use this error.
    """

    status = 404
    code = "not_found"


class Conflict(DomainError):
    """A key already exists, or a targeting version is stale.

    status is 409.
    """

    status = 409
    code = "conflict"


class StorageError(DomainError):
    """The database driver failed.

    status is 500. The message is generic. The original driver exception is
    chained for the server log and is not copied into this message.
    """

    status = 500
    code = "storage_error"

    def __init__(self, message: str = "The database failed to complete the request.") -> None:
        """Use the generic message unless the caller has another safe sentence.

        Does not include driver text. details stays empty.
        """
        super().__init__(message)


class ConstraintViolation(StorageError):
    """A unique constraint rejected a write.

    The service turns this into Conflict. If it reaches the API untranslated
    it is still a storage error with the generic message, so the response
    does not name the constraint.
    """

    def __init__(self) -> None:
        """Build a storage error that carries no driver text."""
        super().__init__()
