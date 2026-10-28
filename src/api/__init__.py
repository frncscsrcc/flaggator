"""HTTP controllers and the standard-library WSGI adapter.

GET /health is dispatched here. Calling this package does not bind a port.
serve() in api.http does, and the process entry does not call it yet.
"""
