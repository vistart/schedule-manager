"""PostgreSQL DDL for every table.

Order matters: foreign keys are declared inline, so a referenced table has to
exist before the one pointing at it.

Everything is ``IF NOT EXISTS``.  There is no migration path: the DDL is the
schema, and a database carrying an older shape is not something this project
upgrades in place.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._column import create_index, drop_table
from .schedules import create_table_expression as create_schedules_expression
from .tokens import (
    TOKENS_TABLE,
    create_token_scopes_expression,
    create_tokens_expression,
)
from .users import create_table_expression as create_users_expression

if TYPE_CHECKING:
    from rhosocial.activerecord.backend.impl.postgres.backend.async_backend import (
        AsyncPostgresBackend,
    )

#: ``(table name, factory)`` in dependency order.  A referenced table has to
#: exist before the one pointing at it.
TABLE_EXPRESSIONS: tuple[tuple[str, Any], ...] = (
    ("users", create_users_expression),
    (TOKENS_TABLE, create_tokens_expression),
    ("api_token_scopes", create_token_scopes_expression),
    ("schedules", create_schedules_expression),
)

#: Indexes are standalone statements — CREATE TABLE rejects inline ones.
INDEX_EXPRESSIONS = (
    lambda d: create_index(d, "ix_api_tokens_user", TOKENS_TABLE, ["user_id"]),
    lambda d: create_index(d, "ix_api_token_scopes_scope", "api_token_scopes", ["scope"]),
    lambda d: create_index(d, "ix_schedules_user", "schedules", ["user_id"]),
)

DROP_ORDER = ("schedules", "api_token_scopes", "api_tokens", "users")


async def create_all(backend: "AsyncPostgresBackend") -> list[str]:
    """Create every table and index if absent.  Returns the table names."""
    dialect = backend.dialect
    created: list[str] = []
    for name, factory in TABLE_EXPRESSIONS:
        await backend.execute(*factory(dialect).to_sql())
        created.append(name)
    for factory in INDEX_EXPRESSIONS:
        await backend.execute(*factory(dialect).to_sql())
    return created


async def drop_all(backend: "AsyncPostgresBackend") -> None:
    """Drop every table, children before parents."""
    dialect = backend.dialect
    for name in DROP_ORDER:
        await backend.execute(*drop_table(dialect, name).to_sql())


__all__ = ["create_all", "drop_all"]
