# Security

flaggator stores decisions that change production behavior, and it stores credentials that can read or change those decisions. This document states the threat assumptions and the controls that follow from them. [P4](decisions.md), [P8](decisions.md), [P13](decisions.md), and [P14](decisions.md) apply.

## Assumptions

- The service is one process on a machine the operator controls. The database is a SQLite file or a MySQL schema the operator points at.
- The default bind is `127.0.0.1` and a non-privileged port. The process does not terminate TLS.
- A deployment that is reachable beyond loopback is expected to sit behind a reverse proxy that terminates TLS and buffers the whole request before forwarding it. Plain HTTP off-loopback is outside the supported setup. The process caps half-open connections, and that cap is not a substitute for the proxy.
- Whoever can read or write the database can read flag configuration, salts, audit history, credential hashes, and user password hashes, and can change flags outside the audit log.
- One deployment is one tenant. Users and roles are global across projects. Separate organizations run separate processes.
- People use JWT-backed users. The opaque management token is break-glass, not the UI identity.

These assumptions are local-deploy assumptions. They are not a reason to skip hashing, scoping, or audit.

## Assets

| Asset | Why it matters |
| --- | --- |
| Targeting configuration | Turning a flag on or rewriting a rule changes application behavior. |
| Evaluation results | They reveal which variation a context receives. Combined with a guessed subject they can disclose a rollout assignment. |
| Salts and full rule trees | They let a holder recompute buckets for every subject. Served only to credentials of that environment. |
| Credential secrets | An opaque management token edits every project and can evaluate. An SDK token reads and evaluates one environment. A user password or JWT signing key issues management access. |
| Audit log | The record of who changed targeting and related management writes. |
| Context attributes | Callers may send identifiers and personal data. The server needs them to evaluate and does not need to store them. |

## Credentials

Kinds ([P8](decisions.md)):

| Kind | Bound to | May do |
| --- | --- | --- |
| Opaque management token | The whole server | Every management and evaluation route. Break-glass / bootstrap / automation. Not a person in a UI. |
| User (JWT) | The whole server, limited by roles | Management routes allowed by `reader` / `writer` / `protected_writer` / `admin`. Does not call evaluate or evaluation-config. |
| SDK token | One environment | Evaluate and read evaluation config for that environment only |

### Opaque tokens (management and SDK)

Issuance:

- The service generates the secret with `secrets.token_urlsafe` (standard library).
- It stores only a hash and a short non-secret prefix so operators can tell keys apart in a list. SDK tokens also store a mnemonic `name`.
- Hash is SHA-256 of the secret. The secret is high-entropy (at least 256 bits from `secrets`), so a salted password hash is not required for these tokens. The hash is still not reversible.
- The plaintext secret is returned once, in the create response, and is not written to the audit log.
- Revocation sets `revoked_at`. Lookup treats a revoked row as unknown and the API returns 401.
- Comparison of the hash uses `hmac.compare_digest`.

Lookup uses a bound parameter on the hash. The token from the header is never concatenated into SQL and never logged.

Opaque management tokens are created by `python -m main bootstrap`. It prints the secret once. Use it to create the first user, for recovery when login is broken, and for non-interactive automation. Prefer JWT users for day-to-day operators.

### Users and passwords

- A user has email (unique), display name, optional password hash, roles, and optional `disabled_at`.
- Password hashes use a slow KDF (stdlib `hashlib.scrypt` preferred). They are never returned by the API or written to audit `before`/`after` documents. A password being set must be 12 to 1024 characters. A password already stored may be as short as 8, and login still accepts that length.
- Local login is `POST /v1/auth/login` with email and password; the response is an access JWT only. Ten failed attempts for one email in 60 seconds answer 429 and do not run scrypt. Anyone who knows the email can hold that bucket full; the opaque management token is the recovery path. A successful login does not count.

### JWT ([P13](decisions.md))

- Locally issued tokens are signed with `FLAGGATOR_JWT_SECRET`. A non-empty secret must be at least 32 bytes. An empty secret disables local login. Issuer, audience, and lifetime come from `FLAGGATOR_JWT_ISSUER`, `FLAGGATOR_JWT_AUDIENCE`, and `FLAGGATOR_JWT_TTL_SECONDS` (default 3600, maximum 86400). Each local token carries `gen`, the user's `token_generation`. A missing or different `gen` is 401, the same message as an unknown token. Tokens issued before that column existed stop working at deploy. Changing the password and `POST /v1/auth/logout` increment the counter, so every local session for that user ends, not only the tab that signed out. The console calls logout and then clears `sessionStorage`.
- An external IdP is configured with `FLAGGATOR_JWT_JWKS_URL` plus the same `FLAGGATOR_JWT_ISSUER` / `FLAGGATOR_JWT_AUDIENCE` used for claim checks. Tokens whose header `alg` is RS* or ES* are verified against that JWKS. Verification checks signature, `iss`, `aud`, and `exp`. The JWKS fetch times out after 5 seconds.
- After verification, roles come from the enabled local user row, not from JWT claims. An HS256 token this process signed selects that row by a decimal `sub` (the local user id) and must carry a matching `gen`. A JWKS token selects it by the normalized `email` claim, or by `sub` only when `sub` itself contains `@`. A numeric `sub` on an IdP token does not select a local id. IdP tokens do not carry `gen`. Logout and a password change do not revoke them; they stay valid until `exp`. Disabling the user still rejects them, because every request loads the enabled row. No matching enabled user → 401. The IdP is not trusted to invent `protected_writer` or `admin`.
- Controllers never parse the bearer header; authentication builds a principal that carries kind, roles, and audit actor id.

## Roles and protected environments ([P14](decisions.md))

| Role | Grants |
| --- | --- |
| `reader` | GET projects, environments, flags, targeting, audit, users |
| `writer` | All `reader` rights, plus create/mutate on non-protected environments and project-level resources that do not target a protected environment |
| `protected_writer` | Add-on only: together with `writer`, mutate protected environments and change the `protected` flag. Also required to grant `protected_writer` to a user and to revoke an opaque management token. |
| `admin` | Every management action: read, write, protected environments, users, and granting any role. Does not evaluate. |

An opaque management token is authorized as if it held every role. An SDK token has none of these roles.

Each environment has `protected` (default false). Changing `protected`, or writing targeting / SDK tokens / similar for a protected environment, requires `writer` and `protected_writer`.

### Authorization matrix (management plane)

| Family | reader | writer | writer + protected_writer | admin | opaque management | SDK |
| --- | --- | --- | --- | --- | --- | --- |
| Health | yes (no auth) | yes | yes | yes | yes | yes |
| Projects / environments GET | yes | yes | yes | yes | yes | no |
| Projects / environments write, `protected` toggle | no | non-protected only | yes | yes | yes | no |
| Flags GET, targeting GET | yes | yes | yes | yes | yes | no |
| Flags / targeting mutate | no | non-protected env only | yes | yes | yes | no |
| SDK token manage | no | non-protected env only | yes | yes | yes | no |
| Users GET | yes | yes | yes | yes | yes | no |
| Users create / patch | no | no | no | yes | yes | no |
| Management tokens GET | yes | yes | yes | yes | yes | no |
| Management tokens revoke | no | no | yes | yes | yes | no |
| Audit GET | yes | yes | yes | yes | yes | no |
| Evaluate / evaluate-all / evaluation-config | no | no | no | no | yes | own env |
| Issue or rotate `client_id` | no | non-protected env | yes | yes | yes | no |
| Change flag `public` | no | project with no protected env | yes | yes | yes | no |
| `POST /v1/client-evaluate` | yes (no auth) | yes | yes | yes | yes | yes |

“Non-protected only” means: if the target environment has `protected=true`, the caller also needs `protected_writer`. `admin` satisfies reader, writer, and protected_writer, and may assign any role. A user JWT still cannot evaluate. Issuing a `client_id` for a protected environment needs `protected_writer`. Changing a flag's `public` bit needs `protected_writer` when the project has any protected environment, because that decision would be visible there too. Changing the flag's name or description stays a writer action.

## Authorization placement

Authentication attaches a principal before the controller runs.

**Coarse gate** (route authorize): SDK tokens may continue only on evaluation paths; user JWTs need at least `reader`, `writer`, or `admin` for management paths; opaque management tokens may continue.

**Fine checks** (domain services): `require_reader`, `require_writer`, `require_admin`, and `require_protected_write(environment)` read `principal.roles` and, when needed, `environment.protected` from SQL. The controller does not load whether an environment is protected. Controllers do not call repositories ([Architecture](architecture.md)).

Other rules:

- SDK token whose environment is not the target of the read or evaluation → `Forbidden`
- Revoked, disabled, or missing credential → `Unauthenticated` (401)
- Valid credential lacking the role for this action → `Forbidden` (403)

## SQL injection

Repository functions bind values and allow-list any dynamic identifier. The rule and the house style are in [Persistence](persistence.md). Controllers and services do not format SQL.

## Rule data is data

The evaluator interprets clauses. It does not pass rule text to `eval`, `exec`, the shell, or an SQL function that evaluates an expression from the operator.

Operators are a closed set defined in [Domain](domain.md). An unknown operator fails validation on write.

Regular expressions are not a v1 operator. If they are added later, patterns need a length cap and a match timeout or an equivalent bound. That work is out of scope until the operator exists.

## Context data

Evaluation reads the context and discards it when the request ends. The service does not write subjects or attributes to the database as part of evaluation. `POST /v1/client-evaluate` follows the same rule.

Audit records store flag configuration before and after a management write. They do not store evaluation contexts.

Request logs, if added, must not include the `Authorization` header, the request body of evaluate calls, or SDK secrets. v1 can run without access logs. If they are turned on, the adapter logs method, path, status, and duration.

## Public flags

A flag is private unless `public` is true. `POST /v1/client-evaluate` takes an environment `client_id` and a context, and returns only the public flags' key, value, and variation index. It does not return rules, salts, segment members, or the reason for a decision. Private flags are still evaluated when a public flag lists them as prerequisites, and they are omitted from the response.

`client_id` is a random value from `secrets.token_urlsafe`, stored in clear text, and shown on environment reads. It is not a hash of the project name. Rotating it makes the previous value 404. A browser cannot keep a secret, so anyone who has the id can probe public flags with chosen contexts. That is the accepted exposure of a public flag. Private flags and evaluation-config stay behind a bearer token.

The route is limited per client address, separately from login: `FLAGGATOR_CLIENT_EVAL_LIMIT` (default 60) requests in `FLAGGATOR_CLIENT_EVAL_WINDOW_SECONDS` (default 60). Login is 10 attempts per address in 60 seconds. An empty address shares the bucket `unknown`. Over the limit is 429 `rate_limited`. The map lives in one process and keeps at most 4096 keys, dropping the quietest first. An unknown `client_id` is the same 404 whether or not the id once existed.

`FLAGGATOR_TRUSTED_PROXIES` is a comma-separated list of IP addresses and CIDR networks, empty by default. When the connecting peer is on that list, the client address is the nearest untrusted hop in `X-Forwarded-For`. A missing header, a hop that is not an IP, or a chain of only trusted hops uses the bucket `unknown`. A peer that is not on the list is counted itself, and its `X-Forwarded-For` is ignored. An IPv6 client counts as its /64. The same list exempts that peer from the per-address cap on half-open connections.

## Optimistic concurrency

Targeting updates require the caller's `version`. A stale write is a 409, not a silent merge. That prevents a lost update from an older editor overwriting a newer rule set. The SQL pattern is in [Persistence](persistence.md).

## Cycles and resource limits

Evaluation walks prerequisites with a visited set and a depth cap, specified in [Domain](domain.md). Writes reject cycles. The caps below are v1 defaults; changing them is a decision, not a silent tweak.

| Limit | Default |
| --- | --- |
| Request body | 256 KiB |
| Variations per flag | 20 |
| Variation value | 16 KiB |
| Rules per configuration | 100 |
| Clauses per rule | 20 |
| Values per clause | 50 |
| Attribute keys per context | 50 |
| Attribute name length | 64 |
| String attribute length | 1024 |
| Clause string length | 1024 |
| Context attribute array | 50 elements |
| Flags per project | 500 |
| Segments per environment | 200 |
| Prerequisite depth | 10 |
| Prerequisites per configuration | 20 |
| Targets per configuration (total subjects) | 1000 |
| Subjects per segment | 1000 |
| Segments cited by one variation | 20 |
| Audit `limit` | 200 |
| List `limit` | 200 |

Exceeding a limit is `ValidationError` (400), not a truncated write.

## Errors and information leakage

- 401 is the same message for a missing token, an unknown token, and a revoked token.
- 404 is for management reads of missing projects, environments, flags, and segments.
- Evaluation of a missing flag returns the caller-supplied default with reason `flag_not_found` ([Domain](domain.md)). It does not echo other flags.
- 500 messages do not include exception text from the database driver.
- Stack traces stay on the server's stderr, not in the JSON body.

## Database

**SQLite.** After each connect, and again after each commit, the process sets mode `0600` on the database file and on the `-wal` and `-shm` siblings when they exist. The parent directory mode is left to the operator (`0700` is appropriate). A backup of the file is a backup of every flag and of credential hashes.

**MySQL.** The configuration holds a host, database name, user, and password. The password is a secret: it is not logged, not written to the audit log, and not returned by any API. The MySQL account used by the process needs rights on this schema only. A dump of the schema is a backup of every flag and of credential hashes.

**Cache.** The evaluation bundle includes rule trees and rollout salts. The in-memory cache holds that inside the process, up to 128 MiB of bundle bytes, and does not store a bundle over 8 MiB. A write that would leave a bundle over 8 MiB is rejected. It does not hold bearer tokens, token hashes, or context attributes. A fill that started before an invalidation does not write back over the delete: each key has a generation bumped on commit. Credential checks go to the database, so revocation is not delayed by a TTL ([Cache](cache.md)). A later Redis or Memcached holds the same bundles and should be treated as confidential as the database.

## Dependency policy

The default install uses the standard library plus JWT libraries: `sqlite3`, `http.server` or `wsgiref`, `json`, `hashlib`, `hmac`, `secrets`, `dataclasses`, **PyJWT** (MIT), and **cryptography** (via `PyJWT[crypto]`; Apache-2.0 / BSD).

**PyJWT** signs local HS256 tokens and verifies HS256 or IdP JWKS tokens (`domain/jwt.py`). The `crypto` extra pulls in **cryptography** so RS*/ES* JWKS verification works. Reason: prefer a known JWT stack over hand-rolled serialization and key handling. Licenses are MIT-compatible with flaggator.

MySQL adds PyMySQL as an optional extra ([P3](decisions.md)). It is imported only by the MySQL database module. The reason: the standard library has no MySQL driver. License: MIT.

No ORM, no query builder, and no web framework are part of that policy ([A3](decisions.md), [A4](decisions.md)).

## Audits

Point-in-time reviews live in [Security audit](security_audit.md). The 2026-10-31 review is the first. Open findings and the remediation order are recorded there; this document stays the control baseline.
