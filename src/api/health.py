"""GET /health.

This route does not require a credential and does not check the database.
"""

from api.request import Request, Response


def get_health(request: Request) -> Response:
    """Report that the process is up.

    Returns 200 and {"status": "ok"}. Does not read the request body and
    does not open a database connection. The adapter still passes request,
    so the signature matches every other controller.
    """
    # Health has no fields to read. Drop the reference so a later edit that
    # starts using the body is a visible change in this function.
    del request
    return Response(status=200, body={"status": "ok"})
