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
from .schedules import TABLE_NAME as SCHEDULES_TABLE
from .schedules import create_table_expression as create_schedules_expression
from .tokens import (
    SCOPES_TABLE,
    TOKENS_TABLE,
    create_token_scopes_expression,
    create_tokens_expression,
)
from .users import TABLE_NAME as USERS_TABLE
from .users import create_table_expression as create_users_expression

if TYPE_CHECKING:
    from rhosocial.activerecord.backend.impl.postgres.backend.async_backend import (
        AsyncPostgresBackend,
    )

#: ``(table name, factory)`` in dependency order.  A referenced table has to
#: exist before the one pointing at it.  Names come from the models, so renaming a
#: table is a one-line change in the model and cannot half-apply here.
TABLE_EXPRESSIONS: tuple[tuple[str, Any], ...] = (
    (USERS_TABLE, create_users_expression),
    (TOKENS_TABLE, create_tokens_expression),
    (SCOPES_TABLE, create_token_scopes_expression),
    (SCHEDULES_TABLE, create_schedules_expression),
)

#: Indexes are standalone statements — CREATE TABLE rejects inline ones.
#:
#: The index name is derived from the table name rather than written out, because
#: an index name has to be unique per schema: PostgreSQL indexes live in one
#: namespace shared by every table, so ``ix_api_tokens_user`` in a database that
#: hosts another service's identically named table collides on creation and takes
#: the whole ``create_all`` down with it.  Prefixing the table name reaches the
#: same safety from the other direction.
INDEX_EXPRESSIONS = (
    lambda d: create_index(d, f"ix_{TOKENS_TABLE}_user", TOKENS_TABLE, ["user_id"]),
    lambda d: create_index(d, f"ix_{SCOPES_TABLE}_scope", SCOPES_TABLE, ["scope"]),
    lambda d: create_index(d, f"ix_{SCHEDULES_TABLE}_user", SCHEDULES_TABLE, ["user_id"]),
)

#: Children before parents, so a foreign key never blocks its own drop.
DROP_ORDER = (SCHEDULES_TABLE, SCOPES_TABLE, TOKENS_TABLE, USERS_TABLE)


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
