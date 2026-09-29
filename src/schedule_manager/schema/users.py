"""DDL for the ``users`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rhosocial.activerecord.backend.expression import CreateTableExpression

from ._column import TIMESTAMP, column

if TYPE_CHECKING:
    from rhosocial.activerecord.backend.expression.bases import SQLDialectBase

TABLE_NAME = "users"


def create_table_expression(dialect: "SQLDialectBase") -> CreateTableExpression:
    return CreateTableExpression(
        dialect=dialect,
        table=TABLE_NAME,
        columns=[
            column(dialect, "id", "SERIAL", primary_key=True),
            column(dialect, "username", "VARCHAR(64)", not_null=True, unique=True),
            column(dialect, "is_active", "BOOLEAN", not_null=True, default=True),
            column(dialect, "created_at", TIMESTAMP, not_null=True),
            column(dialect, "updated_at", TIMESTAMP, not_null=True),
        ],
        if_not_exists=True,
    )
