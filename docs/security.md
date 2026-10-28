# Security

flaggator stores decisions that change production behavior, and it stores credentials that can read or change those decisions. This document states the v1 threat assumptions and the controls that follow from them. Proposals [P4](decisions.md) and [P8](decisions.md) apply.

## Assumptions

- v1 is one process on a machine the operator controls. The database is a SQLite file or a MySQL schema the operator points at.
- The default bind is `127.0.0.1` and a non-privileged port. The process does not terminate TLS.
- A deployment that is reachable beyond loopback is expected to sit behind a reverse proxy that terminates TLS. Plain HTTP off-loopback is outside the supported setup.
- Whoever can read or write the database can read flag configuration, salts, audit history, and credential hashes, and can change flags outside the audit log.
- There are no user accounts in v1. Holding a management token is the operator role.

These assumptions are local-deploy assumptions. They are not a reason to skip hashing, scoping, or audit.

## Assets

| Asset | Why it matters |
| --- | --- |
| Targeting configuration | Turning a flag on or rewriting a rule changes application behavior. |
| Evaluation results | They reveal which variation a context receives. Combined with a guessed context key they can disclose a rollout assignment. |
| Salts and full rule trees | They let a holder recompute buckets for every context key. Served only to credentials of that environment. |
| Credential secrets | A management token edits every project. An SDK key reads and evaluates one environment. |
| Audit log | The record of who changed targeting. |
| Context attributes | Callers may send identifiers and personal data. The server needs them to evaluate and does not need to store them. |

## Credentials

Two kinds ([P8](decisions.md)):

| Kind | Bound to | May do |
| --- | --- | --- |
| Management token | The whole server in v1 | Management routes, audit reads, and evaluation |
| SDK key | One environment | Evaluate and read evaluation config for that environment only |

Issuance:

- The service generates the secret with `secrets.token_urlsafe` (standard library).
- It stores only a hash and a short non-secret prefix so operators can tell keys apart in a list.
- Hash is SHA-256 of the secret. The secret is high-entropy (at least 256 bits from `secrets`), so a salted password hash is not required for these tokens. The hash is still not reversible.
- The plaintext secret is returned once, in the create response, and is not written to the audit log.
- Revocation sets `revoked_at`. Lookup treats a revoked row as unknown and the API returns 401.
- Comparison of the hash uses `hmac.compare_digest`.

Lookup uses a bound parameter on the hash. The token from the header is never concatenated into SQL and never logged.

Management tokens are created by a local bootstrap command, `python -m main bootstrap`. It prints the secret once. They are not seeded as a fixed default password.

## JWT later

Opaque tokens are the v1 credential ([P13](decisions.md)). A later authentication method accepts a standard JWT and builds the same principal the opaque token already produces: credential kind and scope, without the secret. Controllers stay on that principal. v1 does not sign or verify JWTs, and a JWT presented to this version is an unknown credential.

## Authorization

Authentication attaches a principal before the controller runs. The service then enforces:

- SDK key on a management route → `Forbidden`
- SDK key whose environment is not the target of the read or evaluation → `Forbidden`
- Management token on a management route → allowed in v1
- Revoked or missing credential → `Unauthenticated`, which the API renders as 401

Authorization checks sit in the service that performs the action, next to the action, so a new endpoint cannot forget them by calling a repository directly. Controllers do not call repositories ([Architecture](architecture.md)).

## SQL injection

Repository functions bind values and allow-list any dynamic identifier. The rule and the house style are in [Persistence](persistence.md). Controllers and services do not format SQL.

## Rule data is data

The evaluator interprets clauses. It does not pass rule text to `eval`, `exec`, the shell, or an SQL function that evaluates an expression from the operator.

Operators are a closed set defined in [Domain](domain.md). An unknown operator fails validation on write.

Regular expressions are not a v1 operator. If they are added later, patterns need a length cap and a match timeout or an equivalent bound. That work is out of scope until the operator exists.

## Context data

Evaluation reads the context and discards it when the request ends. The service does not write context keys or attributes to the database as part of evaluation.

Audit records store flag configuration before and after a management write. They do not store evaluation contexts.

Request logs, if added, must not include the `Authorization` header, the request body of evaluate calls, or SDK secrets. v1 can run without access logs. If they are turned on, the adapter logs method, path, status, and duration.

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
| Prerequisite depth | 10 |
| Targets per configuration (total keys) | 1000 |
| Audit `limit` | 200 |
| List `limit` | 200 |

Exceeding a limit is `ValidationError` (400), not a truncated write.

## Errors and information leakage

- 401 is the same message for a missing token, an unknown token, and a revoked token.
- 404 is for management reads of missing projects, environments, and flags.
- Evaluation of a missing flag returns the caller-supplied default with reason `flag_not_found` ([Domain](domain.md)). It does not echo other flags.
- 500 messages do not include exception text from the database driver.
- Stack traces stay on the server's stderr, not in the JSON body.

## Database

**SQLite.** The file should be readable and writable only by the account that runs the process. The operator guide (when written) should state mode `0600` on the file and `0700` on its directory. A backup of the file is a backup of every flag and of credential hashes.

**MySQL.** The configuration holds a host, database name, user, and password. The password is a secret: it is not logged, not written to the audit log, and not returned by any API. The MySQL account used by the process needs rights on this schema only. A dump of the schema is a backup of every flag and of credential hashes.

**Cache.** The evaluation bundle includes rule trees and rollout salts. The in-memory cache holds that inside the process. It does not hold bearer tokens, token hashes, or context attributes. Credential checks go to the database, so revocation is not delayed by a TTL ([Cache](cache.md)). A later Redis or Memcached holds the same bundles and should be treated as confidential as the database.

## Dependency policy

The default install uses the standard library only: `sqlite3`, `http.server` or `wsgiref`, `json`, `hashlib`, `hmac`, `secrets`, `dataclasses`.

MySQL adds PyMySQL as an optional extra ([P3](decisions.md)). It is imported only by the MySQL database module. The reason: the standard library has no MySQL driver.

No ORM, no query builder, and no web framework are part of that policy ([A3](decisions.md), [A4](decisions.md)).
