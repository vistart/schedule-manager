# Project Overview

## Purpose

A schedule/task management system that exposes its functionality through:
1. **MCP tools over Streamable HTTP** — for LLM tool calling via the official
   `mcp` Python SDK, authenticated with a bearer token
2. **CLI commands** — same token, presented with `--token` / `SCHEDULE_TOKEN`

Both surfaces share one credential check (`User.resolve_token`) and one scoping
mechanism (`UserScopedQuery`), so a token behaves identically on either.

## Identity

Every operation resolves a token to exactly one user before touching data:

| Transport | Token arrives as |
|---|---|
| Streamable HTTP | `Authorization: Bearer <token>` header |
| CLI | `--token`, `SCHEDULE_TOKEN`, or `SCHEDULE_TOKEN_FILE` |

The SDK's `AuthContextMiddleware` validates the header and publishes the result
on a request-scoped ContextVar; the CLI binds the same value explicitly. Both
are read by `identity.current_user_id()`, which is the only place identity is
obtained. Tokens are opaque random strings stored as SHA-256 digests; they are
revoked, never deleted.

## Core Concepts

- **Schedule** — A task or event with title, description, status, priority, timing, and recurrence
- **User** — account holder; `is_active` is its single "is it live" flag
- **ApiToken** — one bearer credential, invalidated by `revoked_at`
- **ApiTokenScope** — one granted scope per row, composite primary key
- Uses PostgreSQL with JSONB for tags/rdate/exdate fields
- Soft delete via `deleted_at` timestamp (never hard-deletes)
- Auto-managed `created_at`/`updated_at` timestamps
- Scope vocabulary is closed: `schedules:read`, `schedules:write`

## Dependencies

- `rhosocial-activerecord` — ORM (ActiveRecord pattern, Python)
- `rhosocial-activerecord-postgres` — PostgreSQL backend (psycopg3)
- `mcp[cli]` — Official MCP Python SDK v2 with FastMCP
- `python-dateutil` — RFC 5545 RRULE parsing
