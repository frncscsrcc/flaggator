"""HTTP controllers and the standard-library WSGI adapter.

GET /health is dispatched here. Calling this package does not bind a port.
serve() in api.http does, and python -m main calls it.
"""
