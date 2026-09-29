"""Shared DDL helpers.

``rhosocial-activerecord`` no longer generates DDL from a model: ``DDLSourceMixin``
only collects and presents declarations, so building the statements is the
consumer's job.  This package is that consumer — it binds column types to the
active dialect and renders the expressions.

DDL is kept out of ``models/`` on purpose.  Models own the Python side; this
package owns the SQL side.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional

from rhosocial.activerecord.backend.expression import (
    CreateIndexExpression,
    DropTableExpression,
)
from rhosocial.activerecord.backend.expression.statements import (
    ColumnConstraint,
    ColumnConstraintType,
    ColumnDefinition,
    ReferentialAction,
)

if TYPE_CHECKING:
    from rhosocial.activerecord.backend.expression.bases import SQLDialectBase

TIMESTAMP = "TIMESTAMP WITH TIME ZONE"


def column(
    dialect: "SQLDialectBase",
    name: str,
    sql_type: str,
    *,
    primary_key: bool = False,
    not_null: bool = False,
    unique: bool = False,
    default: Any | None = None,
    references: Optional[tuple[str, str]] = None,
    on_delete: Optional[ReferentialAction] = None,
) -> ColumnDefinition:
    """Build one ``ColumnDefinition``.

    ``references`` is ``(table, column)``; combined with ``on_delete`` it emits
    a     column-level ``FOREIGN KEY``.

    ``PRIMARY KEY`` and ``FOREIGN KEY`` are emitted *bare*, so the names gathered
    for them here never reach the SQL — PostgreSQL picks ``<table>_<column>_pkey``
    and ``_fkey``, which are unique because the table name is.  The names are still
    set, since that is the shape the ORM's own DDL declarations use, but they are
    built from the *column* name and are only accidentally unique: four tables in
    this schema each carry a ``user_id``, so a formatter that began honouring them
    would collide on ``fk_user_id`` before ``create_all`` finished.

    So nothing here may rely on a constraint name.  A name that has to be unique
    belongs in ``table_constraints`` with a name derived from the table, as
    ``schema/tokens.py`` does for its composite primary key — which is exactly the
    distinction that renaming a table exposed.
    """
    constraints: list[ColumnConstraint] = []
    if primary_key:
        constraints.append(
            ColumnConstraint(dialect, ColumnConstraintType.PRIMARY_KEY, name=f"pk_{name}")
        )
    if not_null:
        constraints.append(ColumnConstraint(dialect, ColumnConstraintType.NOT_NULL))
    if unique:
        constraints.append(ColumnConstraint(dialect, ColumnConstraintType.UNIQUE))
    if default is not None:
        constraints.append(
            ColumnConstraint(dialect, ColumnConstraintType.DEFAULT, default_value=default)
        )
    if references is not None:
        table, target = references
        constraints.append(
            ColumnConstraint(
                dialect,
                ColumnConstraintType.FOREIGN_KEY,
                name=f"fk_{name}",
                foreign_key_reference=(table, [target]),
                on_delete=on_delete,
            )
        )
    return ColumnDefinition(
        dialect,
        name,
        dialect.parse_type(sql_type),
        constraints=constraints or None,
    )


def create_index(
    dialect: "SQLDialectBase",
    index_name: str,
    table_name: str,
    columns: list[str],
    *,
    unique: bool = False,
) -> CreateIndexExpression:
    """Build a standalone ``CREATE INDEX``.

    Indexes cannot be inlined in ``CREATE TABLE`` — the formatter rejects that
    with ``UnsupportedFeatureError`` (``dialect/mixins/ddl_table.py:613-620``).
    """
    return CreateIndexExpression(
        dialect,
        index_name,
        table_name,
        columns,
        unique=unique,
        if_not_exists=True,
    )


def drop_table(dialect: "SQLDialectBase", table_name: str) -> DropTableExpression:
    """Build ``DROP TABLE IF EXISTS``."""
    return DropTableExpression(dialect=dialect, table=table_name, if_exists=True)
