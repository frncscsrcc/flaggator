# API

The HTTP API is the product surface ([A2](decisions.md)). This document specifies how controllers behave, how errors look, and the resources of the first release ([P1](decisions.md), [P10](decisions.md)).

Paths and fields here are the design contract. The routes the process serves are listed in [openapi.yaml](openapi.yaml). A change to a path, status, query, request body, or response body updates that file in the same change. A route that is not implemented yet stays in this document and out of the OpenAPI file. If the two disagree about a route that exists, this document wins until the OpenAPI file is corrected.

## Protocol

- JSON requests and responses, `Content-Type: application/json`.
- UTF-8.
- Version prefix `/v1` on every resource except the health check.
- Resource identifiers in paths are the public keys (`project key`, `environment key`, `flag key`), not database integers.
- Timestamps in responses are UTC ISO-8601 strings.
- A write response returns the new representation, including the new `version` where the resource has one.

## Authentication

Every route except `GET /health` requires a bearer token:

```
Authorization: Bearer <token>
```

| Credential | Presented as | Scope |
| --- | --- | --- |
| Management token | Bearer token | All management routes. May call evaluation routes, and then must name `project_key` and `environment_key` in the body or path. |
| SDK key | Bearer token | Evaluation routes only. Project and environment are implied by the key. If the request also names them, they must match the key or the response is 403. |

A missing or unknown token is 401. A known token used on a route it cannot call is 403. Storage and hashing are specified in [Security](security.md). v1 tokens are opaque. A standard JWT is a later way to produce the same principal ([P13](decisions.md)); this version does not accept one.

## Controller rules

A controller function:

1. Receives an authenticated principal and a parsed request.
2. Builds one command dataclass from the body and path.
3. Calls one domain service function.
4. Maps the result or domain error to a status and a response dataclass.

Controllers do not:

- authorize beyond what the service enforces (they do not grow a second copy of the rule)
- open transactions
- contain SQL
- evaluate flags
- catch `StorageError` in order to hide it; they map it to the error document with status 500 and no driver detail in the body

Request-shape failures (invalid JSON, a missing required field, a wrong JSON type) are 400 `ValidationError` raised at the controller boundary, before the service runs. Domain failures use the same error document.

## Error document

```json
{
  "error": {
    "code": "conflict",
    "message": "Flag key already exists.",
    "details": []
  }
}
```

| HTTP | `code` | When |
| --- | --- | --- |
| 400 | `validation_error` | Malformed input or a domain rule rejected the command. |
| 401 | `unauthenticated` | Missing or unknown credential. |
| 403 | `forbidden` | Credential is valid and not allowed to perform this action. |
| 404 | `not_found` | Unknown project, environment, or flag on a management route. |
| 409 | `conflict` | Duplicate key, or stale config `version`. |
| 500 | `storage_error` | Database failure. The message is generic. |

`details` is a list of `{ "field": "...", "message": "..." }` for validation, and empty otherwise. `message` is a stable English sentence safe to show to an operator. It does not include SQL, file paths, or token contents.

## Health

`GET /health` → 200 `{"status": "ok"}`. No credential. It does not check the database. A later readiness route may check the database; it is not part of v1.

## Management resources

Base path: `/v1/projects`. All require a management token.

### Projects

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/v1/projects` | Create `{ "key", "name" }`. Response is 201 and the same two fields. |
| `GET` | `/v1/projects` | List projects as `{ "items", "next_cursor" }`. |
| `GET` | `/v1/projects/{projectKey}` | Fetch one project. An unknown key is 404. |

### Environments

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/v1/projects/{projectKey}/environments` | Create `{ "key", "name" }`. Response is 201 and `{ "key", "name" }`. Creating an environment does not copy targeting from another environment. Flags exist at project level; the new environment starts with each existing flag disabled, off variation `0`, and fallthrough variation `0`. |
| `GET` | `/v1/projects/{projectKey}/environments` | List environments in the project. An unknown project is 404. |

The response for an environment includes its key and name. It does not include the SDK key secret. SDK key issuance is a separate action so listing environments cannot leak secrets. A list response uses `next_cursor` null when the page is the last one.

### SDK keys

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/v1/projects/{projectKey}/environments/{environmentKey}/sdk-keys` | Create a key. The plaintext secret is returned once, in this response. |
| `GET` | `/v1/projects/{projectKey}/environments/{environmentKey}/sdk-keys` | List key ids, prefixes, and created timestamps. No secrets. |
| `DELETE` | `/v1/projects/{projectKey}/environments/{environmentKey}/sdk-keys/{keyId}` | Revoke. Evaluation with that key becomes 401. |

### Flags

| Method | Path | Behavior |
| --- | --- | --- |
| `POST` | `/v1/projects/{projectKey}/flags` | Create a flag and its variations. Body includes `key`, `name`, `description`, and `variations` (at least two JSON values). Initializes a disabled configuration in every existing environment. |
| `GET` | `/v1/projects/{projectKey}/flags` | List flags. Each item includes the variation list and, if a query `environment` is present, that environment's configuration summary. |
| `GET` | `/v1/projects/{projectKey}/flags/{flagKey}` | Fetch the flag, variations, and every environment configuration. |
| `PATCH` | `/v1/projects/{projectKey}/flags/{flagKey}` | Update `name` and `description`. Append variations. Cannot change `key`. Cannot remove or reorder variations in v1. |

A flag response is `{ "key", "name", "description", "variations", "environments" }`. `variations` is the ordered JSON values; the index is the position, starting at 0. Each environment entry is `{ "key", "enabled", "off_variation", "fallthrough", "rules", "targets", "prerequisites", "version" }`. Create returns 201 with this document. Fetch and patch return 200 with it.

A list item has `key`, `name`, `description`, and `variations`. When the query `environment` names an environment, the item also has `environment`: `{ "key", "enabled", "off_variation", "version" }`. An unknown environment is 404. List pagination uses the same `limit` and `cursor` as the other lists.

`PATCH` accepts `name`, `description`, and `variations`. At least one is required. `key` is rejected. When `variations` is present it is the full ordered list and must start with the variations already stored; values after that prefix are appended. A shorter list or a different prefix is 400. Appended variations do not change existing environment configurations.

### Targeting

One environment's configuration is replaced as a whole document. Partial patches of a single clause are not offered in v1, so a client cannot accidentally drop rules it did not reload.

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/v1/projects/{projectKey}/flags/{flagKey}/environments/{environmentKey}` | Fetch the configuration, including `version`. |
| `PUT` | same | Replace targeting. Body includes `version` plus the fields in the [configuration document](domain.md#configuration-document). Success returns the stored document with `version + 1`. A wrong `version` is 409. |

`PUT` is idempotent only when replayed with the same version and the same body against the same stored version: the second call conflicts, because the version has moved. Clients retry by reading again. That is the concurrency rule, and it is preferable to a silent overwrite.

### Audit

| Method | Path | Behavior |
| --- | --- | --- |
| `GET` | `/v1/projects/{projectKey}/audit` | Newest first. Optional query: `flag`, `environment`, `limit`. `limit` defaults to 50 and caps at 200. |

An audit item: `id`, `at`, `actor` (credential id and kind, never the secret), `action`, `flag_key`, `environment_key`, `before`, `after`.

## Evaluation resources

These require an SDK key or a management token. With an SDK key, the environment is the key's environment and a mismatched project or environment field in the body is 403.

### Evaluate one

`POST /v1/evaluate`

```json
{
  "project_key": "shop",
  "environment_key": "prod",
  "flag_key": "checkout-v2",
  "context": {
    "key": "user-123",
    "attributes": {"country": "IT"}
  },
  "default": false
}
```

Evaluation is always for one environment. `environment_key` is required for a management token. An SDK key already identifies the environment; a body that includes `project_key` or `environment_key` must match that key.

Response:

```json
{
  "flag_key": "checkout-v2",
  "value": true,
  "variation_index": 1,
  "reason": "rule_match",
  "rule_id": "r1",
  "version": 7
}
```

When the flag does not exist, the response is still 200, `value` is `default`, `variation_index` is `null`, `reason` is `flag_not_found`, and `version` is `null`. This is deliberate: application code can treat the call as "give me a value". Management routes remain strict 404s.

### Evaluate all flags for a context

`POST /v1/evaluate-all`

Body: `project_key`, `environment_key`, `context`, with the same credential rules as evaluate-one. No default map in v1; every flag that exists has a configuration, so each returns a real decision. Unknown keys are simply absent.

Response: `{ "decisions": [ ... same decision objects ... ] }`.

This route is for server-side fan-out. It is not a dump of rule trees. Clients that need the raw configuration use the read route below.

### Read configuration for local evaluation later

`GET /v1/projects/{projectKey}/environments/{environmentKey}/evaluation-config`

Returns the variation lists and environment configurations required to run the algorithm in [Domain](domain.md). SDK keys may call it for their own environment. v1 does not ship a client evaluator; the route exists so the data needed for one is the same data the server uses, and so a server-side caller can cache definitions. Secrets are absent. Salts are present, because bucketing requires them; treat this response as environment-confidential and only serve it to a credential of that environment.

## Create bodies and conflicts

`POST` of a project, environment, or flag with a key that already exists returns 409 `conflict`. There is no silent upsert on create.

## Pagination

List routes take `limit` and `cursor`. `limit` defaults to 50 and must be a whole number from 1 to 200. A value outside that range is 400, not a shorter page. `cursor` is an opaque string the service issues. Clients do not construct it. A missing cursor starts at the first key. v1 may implement the cursor as a keyset on the public key, and the API still presents it as opaque so the encoding can change.

## House style for handlers

Illustrative only; this is the shape, not a module to copy into the tree yet.

```python
def put_targeting(principal: Principal, request: Request, conn: Connection) -> Response:
    command = TargetingWrite.from_request(request)  # shape check, or ValidationError
    config = targeting_service.replace(conn, principal, command)
    return Response.json(200, config.to_document())
```

`targeting_service.replace` is where version checks, authorization, cycle detection, and the transaction live.
