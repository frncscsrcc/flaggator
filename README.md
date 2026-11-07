# flaggator

flaggator is an open-source feature-flag service meant to run on your own machine. The HTTP API is the product: a local process serves it, and every behavior the service offers is available through that API.

The documents in `docs/` specify the HTTP contract, the schema, and the evaluation algorithm. The run section starts a local process that serves the implemented routes.

The domain will feel familiar to anyone who has used a hosted feature-flag product. The HTTP contract, the schema, and the evaluation algorithm are ours. They are specified in these documents so a later client can implement the same rules.

## Design principles

1. **API first.** Behavior is defined as a versioned HTTP API. A user interface, if one is ever added, is a client of that API. The rules for that optional console are in [Frontend](docs/frontend.md).
2. **Thin controllers.** A handler parses a request, calls one domain service, and writes the response. Business rules live in domain services.
3. **Explicit SQL.** Repository functions send SQL text and bind values through the DB-API. Rows are mapped into dataclass DTOs. The project does not use an ORM.
4. **Standard library first.** HTTP, JSON, hashing, and the default database driver (SQLite) come from the Python standard library. JWT uses **PyJWT** (with the `crypto` extra for IdP JWKS). MySQL is an optional extra. Any other third-party package needs a written reason ([Security](docs/security.md)).
5. **Secure by construction.** Credentials are scoped and stored as hashes. Mutations are audited. Flag rules are data, interpreted by the evaluator. SQL values are bound parameters. Operators use JWT users; the opaque management token is break-glass.

## Layout

```
src/api/                  HTTP controllers
src/domain/models/        dataclass models
src/domain/services/      use cases and the evaluator
src/repositories/         SQL, parameter binding, row mapping, evaluation cache-aside
src/cache/                get / set / delete, memory and null implementations
src/composition.py        process wiring: database engine and cache
ui/                       operator console, served by the same process
docs/                     design documents
```

Dependency direction is one way: `api` → `domain services` → `repositories`. Domain models depend on none of those layers. See [Architecture](docs/architecture.md).

## Documents

| Document | What it decides |
| --- | --- |
| [Decisions](docs/decisions.md) | Accepted constraints and proposals still open |
| [Architecture](docs/architecture.md) | Layers, request lifecycle, module layout |
| [Domain](docs/domain.md) | Flag model and the evaluation algorithm |
| [API](docs/api.md) | Resources, controllers, errors, credentials |
| [OpenAPI](docs/openapi.yaml) | Implemented routes, in a form tools can read |
| [Persistence](docs/persistence.md) | SQL style, DTO mapping, transactions, schema |
| [Cache](docs/cache.md) | Cache protocol, what is stored, invalidation |
| [Security](docs/security.md) | Tokens, authorization, injection, audit, limits |
| [Security audit](docs/security_audit.md) | Point-in-time findings and the fix order |
| [Code style](docs/style.md) | How functions are named, commented, and kept explicit |
| [Example](docs/example.md) | A checkout rollout, from the management token to an SDK call |
| [Frontend](docs/frontend.md) | Optional operator console: when it is served, how it is built, how the session is kept |

## Run

From the repository, with the virtual environment already activated and dependencies installed (`pip install -e .` so PyJWT is available):

```bash
source .venv/bin/activate
export FLAGGATOR_JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
python -m main bootstrap
```

Bootstrap creates `flaggator.db` when it is missing, applies migrations, and prints a **management token** once. The notice on stderr does not contain the token. Save it: it is break-glass / bootstrap only. Day-to-day operators should use a **user JWT**.

If you already have a database from before the `users` table and `environments.protected` column, delete `flaggator.db` (and any `-wal` / `-shm` siblings) and run bootstrap again. There is no in-place migration for that schema change.

```bash
python -m main
```

The process listens on `127.0.0.1:8080`. `FLAGGATOR_BIND_HOST`, `FLAGGATOR_BIND_PORT`, and `FLAGGATOR_SQLITE_PATH` change the address and the database file. A host that is not loopback (`127.0.0.1`, `::1`, or `localhost`) is refused unless `FLAGGATOR_ALLOW_CLEARTEXT_BIND=true`. That opt-in is cleartext HTTP on the interface you named. Put TLS on a proxy in front of it.

The same process serves the operator console. The sign-in page is `http://127.0.0.1:8080/login.html`. `FLAGGATOR_HEADLESS=true` serves only the API; a console path is then 404. Any other value stops startup. Omitted, empty, and `false` serve the console.

### Docker

`Dockerfile` builds one process on `python:3.12-slim-bookworm`, pinned by digest. The image does not add gunicorn or uvicorn. `python -m main` is the HTTP server: the standard-library WSGI server, at most 32 requests in flight once the body has been read, and a 30 second deadline measured from accept. A published port should sit behind a proxy that buffers the whole request.

The image listens on `0.0.0.0:8080` and sets `FLAGGATOR_ALLOW_CLEARTEXT_BIND=true`, so the process serves cleartext HTTP on every interface. Put TLS on the proxy in front of the container. The SQLite file is `/data/flaggator.db`. Use the same volume and the same `FLAGGATOR_JWT_SECRET` for bootstrap and for the long-running process: the management token is stored in the database, and the secret signs local login tokens. A bind-mounted data directory must be writable by uid 1000.

```bash
export FLAGGATOR_JWT_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

docker build -t flaggator:1.0.0 .

docker volume create flaggator-data
docker run --rm -v flaggator-data:/data \
  -e FLAGGATOR_JWT_SECRET \
  flaggator:1.0.0 python -m main bootstrap

docker run -d --name flaggator -p 8080:8080 \
  -v flaggator-data:/data \
  -e FLAGGATOR_JWT_SECRET \
  flaggator:1.0.0
```

`-e FLAGGATOR_JWT_SECRET` forwards the variable from the shell, so bootstrap and the long-running process share it. Bootstrap prints the management token once on stdout. `GET /health` is the image health check and does not need a credential.

### First user and login

```bash
export TOKEN=the-printed-management-token

curl -s -X POST http://127.0.0.1:8080/v1/users \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"email":"ops@example.com","name":"Ops","password":"password-ok-1","roles":["reader","writer","protected_writer","admin"]}'

curl -s -X POST http://127.0.0.1:8080/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"ops@example.com","password":"password-ok-1"}'
```

Put the login `access_token` in `TOKEN` for management calls (projects, flags, audit, users). The roles above include `admin`, which is what create and patch of users require. Login needs `FLAGGATOR_JWT_SECRET` (at least 32 bytes). Optional IdP: set `FLAGGATOR_JWT_JWKS_URL`, `FLAGGATOR_JWT_ISSUER`, and `FLAGGATOR_JWT_AUDIENCE`, pre-create the user with the IdP email (password may be omitted), then send the IdP Bearer token. Detail in [Security](docs/security.md).

### Project, environment, flag

Create a project, an environment, and a flag:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/projects \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key":"shop","name":"Shop"}'

curl -s -X POST http://127.0.0.1:8080/v1/projects/shop/environments \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key":"prod","name":"Prod"}'

curl -s -X POST http://127.0.0.1:8080/v1/projects/shop/flags \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"key":"checkout-v2","name":"Checkout","description":"","variations":[false,true]}'
```

Turn the flag on. `version` is the version returned when the flag was created, which is `1` for a new flag. Fallthrough variation `1` is `true`:

```bash
curl -s -X PUT http://127.0.0.1:8080/v1/projects/shop/flags/checkout-v2/environments/prod \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"enabled":true,"off_variation":0,"fallthrough":{"variation":1},"rules":[],"targets":{},"prerequisites":[],"version":1}'
```

Evaluate it. The flag exists, so `default` is ignored:

```bash
curl -s -X POST http://127.0.0.1:8080/v1/evaluate \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"project_key":"shop","environment_key":"prod","flag_key":"checkout-v2","context":{"key":"user-1","attributes":{}},"default":false}'
```

The decision is `value` `true`, `variation_index` `1`, `reason` `fallthrough`, and `version` `2`.

The same path, with the country rollout and an SDK key, is written out in [Example](docs/example.md).
