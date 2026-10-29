"""Process entry.

`python -m main` reads configuration, applies migrations, and listens.
`python -m main bootstrap` creates a management token and does not listen.
"""

import sys

_USAGE = "Usage: python -m main [bootstrap]"


def main(argv: list[str] | None = None) -> None:
    """Listen for HTTP, or create a management token.

    No arguments reads settings, applies migrations, and serves until the
    process is killed. ["bootstrap"] writes one secret to stdout and does
    not bind a port. Any other argument list exits with status 1. argv
    defaults to the arguments after the program name.
    """
    if argv is None:
        arguments = sys.argv[1:]
    else:
        arguments = argv
    if arguments == ["bootstrap"]:
        from composition import bootstrap_management_token

        bootstrap_management_token()
        return
    if arguments:
        raise SystemExit(_USAGE)
    from api.http import serve
    from composition import load_settings, prepare

    runtime = prepare(load_settings())
    serve(runtime.application, runtime.settings.bind_host, runtime.settings.bind_port)


if __name__ == "__main__":
    main()
