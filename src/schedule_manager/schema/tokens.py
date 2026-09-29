"""DDL for the ``sm_api_tokens`` and ``sm_api_token_scopes`` tables.

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

from ..models import ApiToken, ApiTokenScope
from ._column import TIMESTAMP, column
from .users import TABLE_NAME as USERS_TABLE

if TYPE_CHECKING:
    from rhosocial.activerecord.backend.expression.bases import SQLDialectBase

#: Derived from the models for the same reason as ``users.TABLE_NAME``.
TOKENS_TABLE = ApiToken.table_name()
SCOPES_TABLE = ApiTokenScope.table_name()


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
                # Derived, not written out.  This is the only constraint in the
                # schema whose name reaches the SQL — an inline PRIMARY KEY,
                # UNIQUE or REFERENCES is emitted bare and PostgreSQL names it
                # from the table — and constraint names share one namespace with
                # index names.  A name written here outlives the table it belongs
                # to: keeping the old one blocked CREATE TABLE on the renamed
                # table with "relation already exists", because the previous
                # version of this table still owned it.
                name=f"pk_{SCOPES_TABLE}",
                columns=["api_token_id", "scope"],
            )
        ],
        if_not_exists=True,
    )
