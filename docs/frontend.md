# Frontend

The operator console is optional. The HTTP API stays the product: every behavior the service offers is available without the console ([A2](decisions.md)). This document is the rule set for that console. The same process serves it from `ui/`.

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

The console is small and functional: an operator signs in and works with the resources the API already exposes. Pages share one stylesheet, one type scale, and the same spacing. A header and a footer are drawn by `ui/shell.js` into a placeholder on each page. Pages load that module and the shared stylesheet. The markup of the shell is not copied from page to page. The bar is across the top: the brand, the pages, and who is signed in.

`ui/index.html` is the dashboard. `ui/users.html` lists users for any signed-in user. The account list is a full-width table under a collapsed block for creating a user. Create sends `email`, `name`, `roles`, and an optional `password`. The list is the API's cursor pages, ordered by id, 50 per page. A search box sends `search` when the operator submits it, by button or Enter. Typing does not call the API. A new search starts again at the first page. The first page has no previous cursor. `next_cursor` null means there is no next page. There are no page numbers. The page calls `GET`, `POST`, and `PATCH /v1/users`. Edit sends `name`, `roles`, and `disabled`, and sends `password` only when the field is not empty. Email does not change after create. There is no delete: disabling the account is the removal the API offers. A missing token shows a sign-in message and does not load the list. The New user / Edit user block stays hidden unless the claim includes `admin`. Edit, Enable, and Disable stay disabled unless the claim includes `admin`.

`ui/projects.html` lists projects for any signed-in user, in the same layout. The list is cursor pages ordered by key, 50 per page. A search box sends `search` when the operator submits it, by button or Enter. Typing does not call the API. A new search starts again at the first page. The text is a substring of the project name or key. Each row lists that project's environments by walking `GET /v1/projects/{projectKey}/environments` until `next_cursor` is null. A protected environment is the same chip, in red. The project is `key` and `name` and has no update and no delete, so those two fields stay read-only. Edit opens the same block for that project's environments. A saved environment sends `PATCH` with `name` and `protected`. Its key cannot change, and there is no delete, so that row has no Remove. A new row in that block sends `POST /v1/projects/{projectKey}/environments`. Flags opens `flags.html?project=` with the project key. The New project block stays hidden unless the claim includes `admin`. It can also list environments. Create sends the project first, then one environment request per filled row, in order. Each environment body is `key`, `name`, and `protected`. An empty row is skipped. If a later environment request fails, the project and the environments already stored stay. A writer can still create a project by calling the API.

The browser loads these files as ES modules from the same origin. There is no bundler and no minify step. Splitting the code across files is the structure. Comments are English and follow [Code style](style.md): a comment states a reason the next lines do not already state.

No third-party library, font, script, or stylesheet. A file loaded from another domain is a dependency and an extra request. A library needs the same written reason as any other dependency ([A3](decisions.md), [Security](security.md)).

## Session and secrets

Static files contain no secrets. The opaque management token from `python -m main bootstrap` is break-glass and stays in the terminal. It is never written into HTML or JavaScript.

The console signs in through `POST /v1/auth/login` and then calls the API with that user's JWT in the `Authorization: Bearer` header. The token is kept in `sessionStorage` for this tab, so moving from one console page to another keeps the session. Closing the tab clears it.

The login JSON has no roles field. The roles are the `roles` claim inside the access token, the list stored on the user at login. `admin` counts as `reader`, `writer`, and `protected_writer` for display, because the server treats it that way. The claim is a snapshot: each API call reads the roles from the user row and ignores the claim.

Every console page, other than sign-in, checks that claim before it renders. `ui/roles.js` answers whether the claim includes a role. `admin` counts as the other management roles for that check. The users list and the dashboard block for it stay visible without `admin`. The New user / Edit user block stays hidden unless the claim includes `admin`. Edit, Enable, and Disable stay disabled unless the claim includes `admin`. The projects list stays visible the same way. The New project block and Edit environments stay hidden unless the claim includes `admin`. Edit stays disabled unless the claim includes `admin`. Flags stays available. This is presentation only. The API checks the role on every call, so a hidden button is not a security boundary.

`sessionStorage` lasts for this tab and is still readable by script on this origin. `localStorage` is not used: a token placed there remains after the browser closes and is readable from every tab on the origin.

An `httpOnly` cookie is not part of this console. The login response is JSON, and the API expects the bearer header. Moving the session into a cookie would be a change to that API, including a defense for a browser that would attach the cookie on its own. Until that change exists, the console does not invent a cookie session.

Values that come back from the API are inserted as text. They are not turned into HTML. The console does not call `eval`.

The process does not send CORS headers. The console is same-origin with the API, so it does not need them.
