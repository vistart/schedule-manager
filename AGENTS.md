# Schedule Manager

Schedule management system exposing MCP tools and CLI commands for LLM tool calling.
Multi-user: every request carries a bearer token, and every read is scoped to the
user that token resolves to.

## Tech Stack

- Python 3.12+
- `rhosocial-activerecord` ORM (ActiveRecord pattern)
- `rhosocial-activerecord-postgres` (PostgreSQL backend via psycopg3)
- `mcp[cli]` v2 (MCPServer) — Streamable HTTP transport
- `python-dateutil` for RFC 5545 RRULE parsing

## Architecture

```
src/schedule_manager/
├── config.py       # DB + server + token configuration (env vars, .env fallback)
├── db.py           # shared backend wiring — see the note below
├── errors.py       # Unauthenticated / AccountClosed / UsernameTaken
├── identity.py     # current_user_id(): the only place identity is read
├── auth.py         # SDK TokenVerifier, a thin adapter over User.resolve_token
├── models/         # ActiveRecord models
│   ├── base.py     # UserScopedQuery / UserOwnedMixin / JsonbListMixin
│   ├── user.py     # User + UserAccountMixin
│   ├── token.py    # ApiToken + ApiTokenScope
│   └── schedule.py # Schedule
├── schema/         # hand-written PostgreSQL DDL (IF NOT EXISTS; no migrations)
│   ├── _column.py  # column() / create_index() helpers
│   ├── users.py  tokens.py  schedules.py
│   └── __init__.py # create_all() / drop_all()
├── mcp_server.py   # Streamable HTTP MCP server
├── cli.py          # CLI entry point
└── setup_db.py     # schema bootstrap
```

## Hard rules

These are load-bearing. Changing one without reading the reasoning can open a
cross-user data leak.

1. **Identity is never a tool or command argument.** No `user_id` parameter
   anywhere. It comes from the bearer token, resolved at the start of each
   operation.

2. **No MCP tool may enumerate across users.** `User` and `ApiToken` are
   deliberately *not* user-scoped models, because management commands need
   cross-user reads. Exposing them as tools would be a full user dump. The same
   applies to `Schedule.unscoped()`.

3. **Do not query `schedules`, `users`, `api_tokens` or `api_token_scopes` with
   `CTEQuery`, `SetOperationQuery` or `backend.execute`.** Those bypass
   `Model.query()` and are the only path that can miss the user filter. There is
   no hand-written SQL in the application at all — the schema is assembled in
   `schema/` — so this rule has nothing to route around.

4. **Scopes are enforced per tool, not globally.** `AuthSettings.required_scopes`
   is deliberately empty — the SDK's requirement is all-or-nothing, which would
   lock a read-only token out of reads too. The check lives in
   `_ScopeEnforcement` at the ASGI layer, because only there can a 403 be
   produced; a `ServerMiddleware` would surface as a JSON-RPC error under a 200.
   A token that cannot be resolved is passed through untouched so the SDK owns
   every 401. The scope vocabulary is defined **once**, in `models/user.py`.

5. **`api_tokens` stores only a SHA-256 digest.** There is no plaintext column
   and none may be added. Tokens are 256-bit random secrets, so a slow password
   hash buys nothing; if a token is ever given a low entropy, revisit that.

6. **Use `db.create_pool()` and `db.connection()`, not `Model.configure()`.**
   `Model.configure()` is per-class and opens a separate connection for each
   model it is called on, so a transaction opened through one model does not
   cover statements issued through another. A single shared connection is not
   the answer either — concurrent requests would interleave on it. The pool
   publishes the backend on a ContextVar that `Model.backend()` reads before
   `__backend__`, so every model resolves to the same connection inside the
   context while different tasks get different connections.

7. **Management commands need no token.** `user open` provisions the first
   token, so requiring one would be circular. `user` and `token` are authorised
   by local access — whoever can run the process already holds the database
   credentials in its environment. They must never be exposed over the network.

8. **Tokens are opaque, not JWT.** They are bound to this service by
   construction, so there is no `aud` confusion to guard against, and revocation
   is immediate. There is no client-supplied identity assertion to validate.

## Rules

See `.claude/rules/` for detailed conventions:
- `project-overview.md` — architecture and purpose
- `code-style.md` — Python coding conventions
- `database-conventions.md` — model and DDL patterns
- `testing.md` — test framework and patterns
