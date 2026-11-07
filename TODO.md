# TODO

## Own password

Add a route a signed-in user calls to change their own password. Only that user can call it. The route does not accept another user's id.

An admin changing someone else's password stays on `PATCH /v1/users/{userId}`, which remains admin-only. An empty password on that patch keeps the current one.

This route is not implemented. Do not add it to OpenAPI until the controller exists.
