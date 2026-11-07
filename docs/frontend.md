# Frontend

The operator console is optional. The HTTP API stays the product: every behavior the service offers is available without the console ([A2](decisions.md)). This document is the rule set for that console. The process does not serve it yet.

The console is the management client. A customer's page that calls `POST /v1/client-evaluate` with a `client_id` is a different client. That call stays on the public route and does not go through these pages.

## When it is served

`FLAGGATOR_HEADLESS` decides.

| Value | What the process serves |
| --- | --- |
| Omitted, empty, or `false` | The API and the console |
| `true` | The API only: `/health` and `/v1/...` |
| Anything else | Startup fails with `ValidationError`, field `headless` |

`true` means the console routes are absent. A request for a console path is then a normal 404. Missing console files do not stop the API.

## Where the files live

The console is static files served by the same process that serves the API. No second server and no extra framework. The files ship with the application, in `ui/` at the repository root, so `python -m main` can find them.

`.html/` is not this console. That directory is the flaggator.dev site. It is gitignored and published on its own. The two trees stay separate.

Console pages and assets use paths that do not overlap `/health` or `/v1`. A static request whose path would leave `ui/` is rejected. The handler does not follow `..` out of that directory.

## Pages

The console is small and functional: an operator signs in and works with the resources the API already exposes. Pages share one stylesheet, one type scale, and the same spacing. A header and a footer are drawn by one JavaScript module into a placeholder on each page. Pages load that module and the shared stylesheet. The markup of the shell is not copied from page to page.

The browser loads these files as ES modules from the same origin. There is no bundler and no minify step. Splitting the code across files is the structure. Comments are English and follow [Code style](style.md): a comment states a reason the next lines do not already state.

No third-party library, font, script, or stylesheet. A file loaded from another domain is a dependency and an extra request. A library needs the same written reason as any other dependency ([A3](decisions.md), [Security](security.md)).

## Session and secrets

Static files contain no secrets. The opaque management token from `python -m main bootstrap` is break-glass and stays in the terminal. It is never written into HTML or JavaScript.

The console signs in through `POST /v1/auth/login` and then calls the API with that user's JWT in the `Authorization: Bearer` header. The token is held in a JavaScript variable for as long as the page is open. A reload clears it, and the operator signs in again.

`sessionStorage` is the one stored alternative, and only if a reload should keep the session. It lasts for that tab and is still readable by script on this origin. `localStorage` is not used: a token placed there remains after the browser closes and is readable from every tab on the origin.

An `httpOnly` cookie is not part of this console. The login response is JSON, and the API expects the bearer header. Moving the session into a cookie would be a change to that API, including a defense for a browser that would attach the cookie on its own. Until that change exists, the console does not invent a cookie session.

Values that come back from the API are inserted as text. They are not turned into HTML. The console does not call `eval`.

The process does not send CORS headers. The console is same-origin with the API, so it does not need them.
