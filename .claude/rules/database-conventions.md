# Database Conventions

## Model Pattern

Models inherit from `AsyncActiveRecord` and use mixins:

```python
class MyModel(AsyncActiveRecord, TimestampMixin, SoftDeleteMixin):
    __table_name__ = "my_table"
    __pk_auto_generated__ = True
    id: Optional[int] = None
    # ... fields
```

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

Schema creation is **not** exposed in MCP/CLI tools (prevents privilege escalation). Use the standalone script:

```bash
python -m schedule_manager.setup_db
```

This calls `generate_create_table(if_not_exists=True)` and executes the SQL via the backend.

## Backend Configuration

```python
from rhosocial.activerecord.backend.impl.postgres import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

config = PostgresConnectionConfig(host=..., port=..., database=..., username=..., password=...)
await MyModel.configure(config, AsyncPostgresBackend)
```

## PostgreSQL Features Used

- `JSONB` for flexible array data (tags, rdate, exdate)
- `TIMESTAMPTZ` for timezone-aware timestamps
- `ILIKE` for case-insensitive text search
- `BIGSERIAL` for auto-increment primary keys

## Connection Isolation

The async backend uses `contextvars.ContextVar` for per-task isolation. In async frameworks (FastAPI, etc.), each request gets its own backend resolution. For connection pooling, use `AsyncBackendPool` with `PoolConfig`.
