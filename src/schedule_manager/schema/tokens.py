"""DDL for the ``api_tokens`` and ``api_token_scopes`` tables.

``ON DELETE`` follows the nature of the relationship, not a single rule:

* ``api_tokens.user_id`` is ``RESTRICT`` — the token is a credential with its
  own life; deleting a user must not silently destroy it.
* ``api_token_scopes.api_token_id`` is ``CASCADE`` — the scope rows are a child
  collection with no independent meaning, so they are garbage once the token is
  gone.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from rhosocial.activerecord.backend.expression import CreateTableExpression
from rhosocial.activerecord.backend.expression.statements import (
    ReferentialAction,
    TableConstraint,
    TableConstraintType,
)

from ._column import TIMESTAMP, column
from .users import TABLE_NAME as USERS_TABLE

if TYPE_CHECKING:
    from rhosocial.activerecord.backend.expression.bases import SQLDialectBase

TOKENS_TABLE = "api_tokens"
SCOPES_TABLE = "api_token_scopes"


def create_tokens_expression(dialect: "SQLDialectBase") -> CreateTableExpression:
    return CreateTableExpression(
        dialect=dialect,
        table=TOKENS_TABLE,
        columns=[
            column(dialect, "id", "SERIAL", primary_key=True),
            column(
                dialect,
                "user_id",
                "INTEGER",
                not_null=True,
                references=(USERS_TABLE, "id"),
                on_delete=ReferentialAction.RESTRICT,
            ),
            column(dialect, "token_hash", "CHAR(64)", not_null=True, unique=True),
            column(dialect, "label", "VARCHAR(64)"),
            column(dialect, "expires_at", TIMESTAMP),
            column(dialect, "revoked_at", TIMESTAMP),
            column(dialect, "created_at", TIMESTAMP, not_null=True),
            column(dialect, "updated_at", TIMESTAMP, not_null=True),
        ],
        if_not_exists=True,
    )


def create_token_scopes_expression(dialect: "SQLDialectBase") -> CreateTableExpression:
    return CreateTableExpression(
        dialect=dialect,
        table=SCOPES_TABLE,
        columns=[
            column(
                dialect,
                "api_token_id",
                "INTEGER",
                not_null=True,
                references=(TOKENS_TABLE, "id"),
                on_delete=ReferentialAction.CASCADE,
            ),
            column(dialect, "scope", "VARCHAR(64)", not_null=True),
        ],
        table_constraints=[
            TableConstraint(
                dialect,
                TableConstraintType.PRIMARY_KEY,
                name="pk_api_token_scopes",
                columns=["api_token_id", "scope"],
            )
        ],
        if_not_exists=True,
    )
