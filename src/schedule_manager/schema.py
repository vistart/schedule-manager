"""PostgreSQL DDL for the ``schedules`` table.

``rhosocial-activerecord`` no longer generates DDL from a model: the
``DDLSourceMixin`` only collects and presents declarations, so building the
statement is the consumer's job.  This module is that consumer — it binds
column types to the active dialect and renders the expressions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rhosocial.activerecord.backend.expression import (
    CreateTableExpression,
    DropTableExpression,
)
from rhosocial.activerecord.backend.expression.statements import (
    ColumnConstraint,
    ColumnConstraintType,
    ColumnDefinition,
)

from .model import Schedule

if TYPE_CHECKING:
    from rhosocial.activerecord.backend.expression.bases import SQLDialectBase
    from rhosocial.activerecord.backend.impl.postgres.backend.async_backend import (
        AsyncPostgresBackend,
    )

TABLE_NAME = "schedules"

_TIMESTAMP = "TIMESTAMP WITH TIME ZONE"


def _column(
    dialect: SQLDialectBase,
    name: str,
    sql_type: str,
    *,
    primary_key: bool = False,
    not_null: bool = False,
    default: Any | None = None,
) -> ColumnDefinition:
    constraints: list[ColumnConstraint] = []
    if primary_key:
        constraints.append(
            ColumnConstraint(
                dialect, ColumnConstraintType.PRIMARY_KEY, name="pk_schedules"
            )
        )
    if not_null:
        constraints.append(ColumnConstraint(dialect, ColumnConstraintType.NOT_NULL))
    if default is not None:
        constraints.append(
            ColumnConstraint(
                dialect, ColumnConstraintType.DEFAULT, default_value=default
            )
        )
    return ColumnDefinition(
        dialect,
        name,
        dialect.parse_type(sql_type),
        constraints=constraints or None,
    )


def create_table_expression(
    dialect: SQLDialectBase,
) -> CreateTableExpression:
    """Build ``CREATE TABLE IF NOT EXISTS schedules`` for the given dialect."""
    columns = [
        _column(
            dialect,
            Schedule.primary_key_columns()[0],
            "SERIAL",
            primary_key=True,
        ),
        _column(dialect, "title", "VARCHAR(200)", not_null=True),
        _column(dialect, "description", "TEXT"),
        _column(dialect, "status", "VARCHAR(20)", not_null=True, default="pending"),
        _column(dialect, "priority", "INTEGER", not_null=True, default="3"),
        _column(dialect, "start_time", _TIMESTAMP),
        _column(dialect, "due_time", _TIMESTAMP),
        _column(dialect, "completed_at", _TIMESTAMP),
        _column(dialect, "location", "VARCHAR(200)"),
        _column(dialect, "tags", "JSONB"),
        _column(dialect, "rrule", "TEXT"),
        _column(dialect, "rdate", "JSONB"),
        _column(dialect, "exdate", "JSONB"),
        _column(dialect, "created_at", _TIMESTAMP, not_null=True),
        _column(dialect, "updated_at", _TIMESTAMP, not_null=True),
        _column(dialect, "deleted_at", _TIMESTAMP),
    ]

    return CreateTableExpression(
        dialect=dialect,
        table=TABLE_NAME,
        columns=columns,
        if_not_exists=True,
    )


def drop_table_expression(dialect: SQLDialectBase) -> DropTableExpression:
    """Build ``DROP TABLE IF EXISTS schedules`` for the given dialect."""
    return DropTableExpression(dialect=dialect, table=TABLE_NAME, if_exists=True)


async def create_table(backend: AsyncPostgresBackend) -> None:
    """Create the ``schedules`` table if it does not already exist."""
    await backend.execute(*create_table_expression(backend.dialect).to_sql())


async def drop_table(backend: AsyncPostgresBackend) -> None:
    """Drop the ``schedules`` table if it exists."""
    await backend.execute(*drop_table_expression(backend.dialect).to_sql())
