# Database Conventions

## Model Pattern

Models live in `src/schedule_manager/models/`, one class per file, and are
imported from the package rather than from submodules:

```python
from .models import Schedule
```

```python
class MyModel(UserOwnedMixin, JsonbListMixin,
              DefaultTimestampMixin, DefaultAsyncSoftDeleteMixin, AsyncActiveRecord):
    __table_name__ = "my_table"
    __pk_auto_generated__ = True
    __query_class__ = UserScopedQuery      # only if it is owned by a user
    id: Optional[int] = None
    # ... fields
```

Import them from the package, never from `models.schedule` directly — the
package `__init__` is the single import surface.

## User Scoping

Read scoping lives in `UserScopedQuery.__init__`
(`models/base.py`), which attaches the current identity to `where_clause` at
construction time. `Model.query()` is `return cls.__query_class__(cls)`
(`base/query_mixin.py:135`), so one override covers every terminal method —
`all`, `one`, `count`, `exists`, `aggregate`, `sum_`, `update_all`,
`delete_all` — and any added later.

Do **not** override the terminal methods instead. The execution paths are not
unified: `count` and the numeric aggregates funnel into
`await self.aggregate()` (`aggregate.py:450`) while `all()` goes through
`self.to_sql()`. Missing one of those patches raises nothing and silently
returns another user's rows.

Write scoping lives in `UserOwnedMixin.prepare_save_data`, which the framework
chains along the MRO (`base/base.py:1074-1081`). It overwrites `user_id` on
both INSERT and UPDATE, so ownership can never be supplied by a caller.

Assert the contract in tests by inspecting generated SQL, which needs no
database:

```python
with as_user(7):
    assert '"user_id" = %s' in Schedule.query().to_sql()[0]
```

`Schedule.unscoped()` is the management-plane escape hatch. It is never
reachable from an MCP tool or a business command, and it also bypasses the
soft-delete filter.

## Forbidden Query Paths

Do not read `schedules`, `users`, `api_tokens` or `api_token_scopes` through
`CTEQuery`, `SetOperationQuery` or `backend.execute`. They bypass
`Model.query()` and are the only route that can miss the user filter. Raw SQL
does not appear in application code at all: the schema is assembled in
`schema/`, and no query is written by hand anywhere in `src/`.

## Async Usage

All DB methods are async — always `await`:

```python
await model.save()
await Model.find_one(id)
await Model.find_all()
await Model.query().where(...).all()
await Model.query().where(...).count()
await model.delete()
```

## Mixins

- `TimestampMixin` — auto-manages `created_at` and `updated_at`
- `SoftDeleteMixin` — adds `deleted_at` for soft delete (never hard-delete)

## Column Reference

Use `Model.c.column_name` for type-safe column references in queries:

```python
await MyModel.query().where(MyModel.c.status == "active").all()
```

## Pydantic Validation

Models inherit from `pydantic.BaseModel` via `ActiveRecordBase`. Use:

- `@field_validator("field_name")` for per-field validation
- `@model_validator(mode="after")` for cross-field validation
- `validate_record()` classmethod for business rules (e.g., uniqueness)

Validation runs automatically before `save()`.

## DDL / Schema Setup

Schema creation is **not** exposed in MCP/CLI tools (prevents privilege
escalation). Use the standalone script:

```bash
schedule-manager-setup-db
```

It runs `schema.create_all()`. DDL is hand-written in `schema/`, one module per
table; `rhosocial-activerecord` no longer renders DDL from a model.

There is no migration path. Every statement is `IF NOT EXISTS`, and a database
carrying an older shape is not upgraded in place.

Indexes must be standalone `CreateIndexExpression` statements — the formatter
rejects them inside `CREATE TABLE` with `UnsupportedFeatureError`
(`dialect/mixins/ddl_table.py:613-620`).

## Backend Configuration

`Model.configure()` is per-class: each call constructs a new backend instance
and opens its own connection. Configuring several models therefore yields
several connections, and a transaction opened through one model does not cover
statements issued through another.

Always go through `db.connect()`, which configures one class and shares the
resulting backend with the rest:

```python
from .db import connect

backend = await connect()          # or await connect(config)
await create_all(backend)
```

The Postgres backend keeps a single connection and its transaction state is
instance-level, so a nested `backend.transaction()` would commit the enclosing
unit's work early. `models/user.py` works around this with a `transaction()`
helper that joins an already-open transaction instead of starting a new one —
use that helper for any multi-statement model operation.

## PostgreSQL Features Used

- `JSONB` for flexible array data (tags, rdate, exdate)
- `TIMESTAMPTZ` for timezone-aware timestamps
- `ILIKE` for case-insensitive text search
- `BIGSERIAL` for auto-increment primary keys

## Connection Isolation

The async backend uses `contextvars.ContextVar` for per-task isolation. In async frameworks (FastAPI, etc.), each request gets its own backend resolution. For connection pooling, use `AsyncBackendPool` with `PoolConfig`.
