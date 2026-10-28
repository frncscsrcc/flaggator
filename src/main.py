"""Process entry.

`python -m main bootstrap` creates a management token. The HTTP listener is
not started yet. A later phase connects configuration and serve().
"""

import sys


def main(argv: list[str] | None = None) -> None:
    """Create a management token, or stop before listening.

    The only accepted argument list is ["bootstrap"]. That command applies
    migrations, writes one secret to stdout, and does not bind a port. Any
    other invocation exits with status 1 and the message below. argv defaults
    to the arguments after the program name.
    """
    if argv is None:
        arguments = sys.argv[1:]
    else:
        arguments = argv
    if arguments == ["bootstrap"]:
        from composition import bootstrap_management_token

        bootstrap_management_token()
        return
    raise SystemExit(
        "flaggator cannot serve yet. The HTTP listener is added in a later phase."
    )


if __name__ == "__main__":
    main()
