# Security audit — 2026-10-31

Point-in-time review of the flaggator process as it stood on 31 October 2026. This file is a record. The living controls stay in [Security](security.md). The findings below were closed the same day; the original text is unchanged apart from the status column.

Scope: the whole process (HTTP adapter, SQL, evaluation, cache, credentials, users, JWT), not only the authentication layer. Method: read of `src/` against [Security](security.md). No exploit was run against a live server. Default bind `127.0.0.1` reduces exposure; the ratings assume the process may also sit behind a reverse proxy on a network.

## Findings

| ID | Severity | Location | Finding | Status |
| --- | --- | --- | --- | --- |
| F1 | High | `src/api/http.py` (`request_from_environ`) | `Content-Length` is read in full before the 256 KiB cap in `src/api/body.py`. `GET /health` and `POST /v1/auth/login` need no bearer, so an anonymous client can force a large allocation. | Fixed |
| F2 | High | `src/api/http.py` (`serve`) | `ThreadingWSGIServer` has no connection cap, no read timeout, and no concurrency limit. A slow client holds a thread until the process exits. | Fixed |
| F3 | Medium | `src/domain/jwt.py` (`_jwks_client`) | With `FLAGGATOR_JWT_JWKS_URL` set, a bearer that merely looks like an RS*/ES* JWT triggers a JWKS fetch before the signature is checked. `PyJWKClient` is constructed without a timeout, so a slow URL blocks a worker thread. The URL is operator configuration, not client input. | Fixed |
| F4 | Medium | `src/domain/services/users.py` (`login`) | Login does not apply the 8–1024 character password rule before scrypt, and `/v1/auth/login` has no rate limit. | Fixed |
| F5 | Medium | `src/repositories/sqlite.py` (`connect`) | The SQLite file is created with the process umask. The code never sets mode `0600` on the database, `-wal`, or `-shm` files. Another local user can read password hashes, token hashes, rollout salts, and the audit log. | Fixed |
| F6 | Medium | `src/domain/services/credentials.py` | An opaque management token is created only by `bootstrap`. There is no list or revoke route. A leaked token stays valid until the row is deleted in SQL. | Fixed |
| F7 | Medium | `src/domain/services/authentication.py` (`_load_user`) | A decimal `sub` is resolved to a local user id before `email`. An external IdP token whose `sub` is a number can select that local user even when the email claim is someone else. Local HS256 tokens are safe because only this process can sign them. | Fixed |
| F8 | Medium | `src/repositories/eval_cache.py` (`load_document`) | A cache miss loads SQL and then `set`s. A write that invalidates the key in between can be overwritten by that `set`, so the previous bundle is served until the TTL (default 5 seconds). | Fixed |
| F9 | Low | `src/composition.py` (`load_settings`) | A non-empty `FLAGGATOR_JWT_SECRET` shorter than 32 bytes is accepted. An empty secret still disables local login. | Fixed |
| F10 | Low | `src/domain/services/users.py` (`login`) | Unknown email, a disabled user, and an IdP-only user skip scrypt. A wrong password runs it. The 401 body is the same; the latency is not. | Fixed |
| F11 | Low | `src/domain/services/users.py` (create and patch) | Any `writer` can add `protected_writer`, including onto their own row. Management tokens already hold every role, so they are unaffected. | Fixed |
| F12 | Low | `src/domain/services/evaluation.py` (`evaluate_all`) | `evaluate-all` walks every flag in the environment. There is no cap on flags per project, so one SDK token can repeat an arbitrarily large walk. | Fixed |
| F13 | Low | `src/domain/services/targeting.py` (`_check_clause_value`), `src/domain/services/evaluation.py` (`_require_attribute_value`) | Clause strings have no length cap of their own, and a context attribute array has no element cap. Both are still bounded by the 256 KiB body once F1 is fixed. Operators are a closed set; there is no regular expression. | Fixed |

## Checked and sound

Recorded so a later reader does not re-open them without a code change.

- Repository SQL binds values. The audit list builds `WHERE` from constant fragments only.
- The evaluator does not call `eval`, `exec`, or the shell. Clause operators are an allow-list.
- Prerequisite cycles are rejected on write. Evaluation stops at depth 10.
- An SDK token is confined to its environment. A mismatched project or environment is 403, including when the other environment does not exist.
- A user JWT cannot call evaluate, evaluate-all, or evaluation-config.
- Roles used for authorization come from the enabled local user row, not from JWT claims.
- Password hashes and token secrets are absent from API responses and from audit JSON.
- Opaque token lookup compares hashes with `hmac.compare_digest`, including when no row matches.
- Public keys are a closed pattern, so path segments cannot escape into other resources.
- Unhandled exceptions become a generic 500. Driver text stays on stderr.
- The evaluation cache stores bundles, not bearer tokens and not evaluation contexts.

## Remediation plan

Implement in this order. Each step updates tests. A step that changes an HTTP path, status, or body also updates `docs/openapi.yaml` and `.html/api.html` in the same change, and the matching paragraph in [Security](security.md) or [API](api.md).

1. **F1 — cap the body at the socket.** In `request_from_environ`, reject `Content-Length` above `MAX_REQUEST_BODY_BYTES` with 400 before calling `read`. Do not allocate the declared size. Missing or zero length stays an empty body.

2. **F2 — bound the server.** On each accepted socket set a read/write timeout (30 seconds). Cap in-flight requests (32) with a non-blocking semaphore; past the cap, respond 503 and close. Keep `daemon_threads`.

3. **F3 — time-bound JWKS.** Pass a timeout (5 seconds) into `PyJWKClient`. A fetch failure stays the same 401 as any other bad bearer. Do not log the token or the URL's response body.

4. **F4 and F10 — login cost.** Before `verify_password`, reject a password outside 8–1024 characters with the same 401 as a bad login (no distinct validation error). When the user is missing, disabled, or has no password hash, still run `verify_password` against a fixed scrypt hash so the CPU work matches a wrong password. Add an in-process limit on `POST /v1/auth/login` (10 attempts per 60 seconds per `REMOTE_ADDR`). Over the limit, respond 429 with a stable error code. Document 429 on that route only.

5. **F9 — secret length.** When `FLAGGATOR_JWT_SECRET` is non-empty, `load_settings` rejects it unless the UTF-8 form is at least 32 bytes. Empty still means “local login is off”.

6. **F7 — IdP subject.** Decimal `sub` selects a local user only for an HS256 token this process signed. A JWKS-verified token matches the normalized `email` claim only (and `sub` only when it contains `@`). A numeric `sub` on an IdP token must not override the email.

7. **F11 — who may grant `protected_writer`.** Creating or patching a user so that `protected_writer` is newly present requires the caller to already hold `protected_writer`. An opaque management token holds every role, so bootstrap still works. A plain `writer` may still grant `reader` and `writer`, and may remove `protected_writer`. Update the matrix in [Security](security.md).

8. **F5 — SQLite file mode.** After a successful SQLite connect, set mode `0600` on the database file and on the sibling `-wal` and `-shm` files when they exist. Do not change the parent directory mode. MySQL is unchanged.

9. **F8 — cache generation.** Keep a per-key generation counter. `invalidate_*` bumps that counter and deletes the key on commit, under the same lock the load path uses. `load_document` records the generation before reading SQL and calls `set` only when the generation is still that value. A stale fill is dropped. The TTL stays a backstop.

10. **F6 — revoke a management token.** Add list and revoke for opaque management tokens: `GET /v1/management-tokens` returns `id`, `prefix`, `created_at` (never the secret), and `DELETE /v1/management-tokens/{id}` sets `revoked_at`. Listing requires `reader`. Revoking requires `protected_writer` (a management token qualifies). Audit the revoke. A revoked management token then fails like any other revoked opaque token (401). Update OpenAPI, `.html/api.html`, and [API](api.md).

11. **F12 — cap the catalog.** Reject flag create above 500 flags per project, and segment create above 200 segments per environment, with 400. `evaluate-all` stays complete inside that cap.

12. **F13 — value sizes.** Cap each clause string at 1024 characters and each context attribute array at 50 elements. Reject over the cap with 400. Update the limits table in [Security](security.md).

After the twelve steps, append a short “Fixed” note to this file (date and the ids closed). Do not delete the original findings.

## Fixed — 2026-10-31

F1–F13 are closed in the process. Body size is rejected before `read`. The server caps in-flight requests and sets a socket timeout. JWKS fetches time out. Login burns scrypt on a miss, rejects out-of-range passwords with 401, and returns 429 after 10 attempts per address per minute. A non-empty JWT secret must be at least 32 bytes. IdP tokens match email, not a numeric `sub`. Granting `protected_writer` and revoking a management token require `protected_writer`. SQLite files are mode `0600`. Cache fills check a generation counter. Flag and segment creates, clause strings, and context arrays enforce the caps in [Security](security.md).

## Re-checked — 2026-10-31

A second read of `src/` against the closures above, the same day. The suite at that read was 109 tests, all passing. The finding text higher up is the defect as it was found. The control now in the code is below.

| ID | Control now in the code |
| --- | --- |
| F1 | `request_from_environ` rejects `Content-Length` above 256 KiB before `wsgi.input.read`. |
| F2 | Each accepted socket has a 30 second timeout. At most 32 requests run at once. The next answer is HTTP 503 and the socket is closed. `daemon_threads` stays on. |
| F3 | `PyJWKClient` is built with `timeout=5`. A fetch failure is the same 401 as any other bad bearer. |
| F4 | A password outside 8–1024 characters, or with surrounding spaces, is 401 and does not run scrypt. `LoginLimiter` allows 10 attempts per 60 seconds per `REMOTE_ADDR`. An empty address shares the bucket `unknown`. Over the limit is 429, code `rate_limited`. |
| F5 | After SQLite connect, and again after each commit, mode `0600` is set on the database file and on `-wal` and `-shm` when those files exist. |
| F6 | `GET /v1/management-tokens` requires `reader` and returns `id`, `prefix`, `created_at`. `DELETE /v1/management-tokens/{tokenId}` requires `protected_writer`, sets `revoked_at`, and audits `management_token.revoke`. The path value is the decimal id. A prefix is 404. |
| F7 | A decimal `sub` selects a local user id only when the verified algorithm is HS256. A JWKS token selects the user by the normalized `email` claim, or by `sub` only when `sub` contains `@`. |
| F8 | `load_document` samples a per-key generation before the SQL read and stores only when that generation is unchanged. The check and the store share the lock that invalidation uses to bump the generation and delete the key. |
| F9 | `load_settings` rejects a non-empty `FLAGGATOR_JWT_SECRET` shorter than 32 UTF-8 bytes. An empty secret still means local login is off. |
| F10 | A missing user, a disabled user, and an IdP-only user run `burn_password_check` against a process-cached dummy scrypt hash, then the same 401. |
| F11 | Create and patch call `_reject_new_protected_writer`. Adding the role when it was not already on the row requires the caller to hold it. Sending a role the user already has, or removing it, does not. |
| F12 | Creating a flag past 500 in one project is 400. Creating a segment past 200 in one environment is 400. |
| F13 | A clause string longer than 1024 characters is 400. A context attribute array longer than 50 elements is 400. |

## Limits that remain

These are not open findings. They are the edges the closures do not cover.

- The login limit is one map inside one process. Several processes behind one proxy do not share it. The key is `REMOTE_ADDR`, not a forwarded header.
- A password outside 8–1024 characters returns 401 without scrypt. A missing user still burns one scrypt verification.
- JWKS is still fetched from the operator-configured URL before the signature is checked. The fetch now stops after 5 seconds.
- SQLite mode is applied after the file exists, not in the `open` call. A crash between create and `chmod` can leave the umask mode. The parent directory mode is unchanged.
- The 30 second timeout is on the accepted socket. It stops a slow client. It does not abort a handler that is already running on the CPU.
- `evaluate-all` still walks every flag in the environment, inside the 500-flag cap.
