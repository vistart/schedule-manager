"""DDL for the ``schedules`` table."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rhosocial.activerecord.backend.expression import CreateTableExpression
from rhosocial.activerecord.backend.expression.statements import ReferentialAction

from ..models import Schedule
from ._column import TIMESTAMP, column
from .users import TABLE_NAME as USERS_TABLE

if TYPE_CHECKING:
    from rhosocial.activerecord.backend.expression.bases import SQLDialectBase

TABLE_NAME = "schedules"


def create_table_expression(dialect: "SQLDialectBase") -> CreateTableExpression:
    """Build ``CREATE TABLE IF NOT EXISTS schedules`` for the given dialect."""
    columns = [
        column(
            dialect,
            Schedule.primary_key_columns()[0],
            "SERIAL",
            primary_key=True,
        ),
        column(
            dialect,
            "user_id",
            "INTEGER",
            not_null=True,
            references=(USERS_TABLE, "id"),
            on_delete=ReferentialAction.RESTRICT,
        ),
        column(dialect, "title", "VARCHAR(200)", not_null=True),
        column(dialect, "description", "TEXT"),
        column(dialect, "status", "VARCHAR(20)", not_null=True, default="pending"),
        column(dialect, "priority", "INTEGER", not_null=True, default=3),
        column(dialect, "start_time", TIMESTAMP),
        column(dialect, "due_time", TIMESTAMP),
        column(dialect, "completed_at", TIMESTAMP),
        column(dialect, "location", "VARCHAR(200)"),
        column(dialect, "tags", "JSONB"),
        column(dialect, "rrule", "TEXT"),
        column(dialect, "rdate", "JSONB"),
        column(dialect, "exdate", "JSONB"),
        column(dialect, "created_at", TIMESTAMP, not_null=True),
        column(dialect, "updated_at", TIMESTAMP, not_null=True),
        column(dialect, "deleted_at", TIMESTAMP),
    ]

    return CreateTableExpression(
        dialect=dialect,
        table=TABLE_NAME,
        columns=columns,
        if_not_exists=True,
    )
