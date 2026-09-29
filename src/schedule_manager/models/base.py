"""Mixins and query classes shared across models.

Nothing here declares business logic or table columns; these are the pieces
that more than one model needs, and — for the user-scoping pair — the single
copy of security-critical code.
"""

from __future__ import annotations

import json
from typing import ClassVar

from rhosocial.activerecord.interface.update import IDataPreparationBehavior
from rhosocial.activerecord.query import AsyncActiveQuery

from ..identity import current_user_id


class UserScopedQuery(AsyncActiveQuery):
    """Attaches the current identity to ``where_clause`` at construction time.

    The seam is ``__init__`` rather than the terminal methods on purpose.
    ``AsyncQueryMixin.query()`` is ``return cls.__query_class__(cls)``
    (``base/query_mixin.py:135``) and ``AsyncActiveQuery.__init__`` is
    synchronous (``query/active_query.py:421``), so a predicate attached here is
    present in ``where_clause`` before any execution path reads it.

    That makes one override cover ``all`` / ``one`` / ``count`` / ``exists`` /
    ``aggregate`` / ``sum_`` / ``avg`` / ``min_`` / ``max_`` / ``update_all`` /
    ``delete_all``, plus whatever terminal methods are added later.  Overriding
    the terminal methods instead would require five or six separate patches,
    because the execution paths are not unified: ``count`` and the numeric
    aggregates funnel into ``await self.aggregate()`` (``aggregate.py:450``)
    while ``all()`` goes through ``self.to_sql()`` and ``fetch_all``.  Missing
    one of those patches raises nothing and silently returns another user's
    rows.
    """

    def __init__(self, model_class, *, user_scope: bool = True):
        super().__init__(model_class)
        if user_scope:
            self.where(model_class.c.user_id == current_user_id())


class UserOwnedMixin(IDataPreparationBehavior):
    """Forces ``user_id`` on every write so ownership never comes from a caller.

    The column default exists only so that ``Schedule(title=...)`` constructs;
    it is overwritten here before the statement is built, so the placeholder
    never reaches the database.  Applying it on UPDATE as well as INSERT means
    no code path can transfer a schedule to another user.
    """

    @classmethod
    def unscoped(cls):
        """Return a query that ignores the current identity.

        Management-plane only — migrations and audits.  Never reachable from an
        MCP tool or a business command.  Note that it also bypasses the
        soft-delete filter, because it constructs the query class directly
        instead of going through ``Model.query()``; that is the intent, since
        management wants to see deleted rows too.
        """
        return cls.__query_class__(cls, user_scope=False)

    def prepare_save_data(self, data: dict, is_new: bool) -> dict:
        user_id = current_user_id()
        data["user_id"] = user_id
        # Also reflect it on the instance: the framework hands this method a
        # payload, not the model, so without this the in-memory object would keep
        # the placeholder while the stored row has the real owner.
        self.user_id = user_id
        return data


class JsonbListMixin(IDataPreparationBehavior):
    """Serialises ``list`` fields into the JSON string PostgreSQL JSONB expects.

    This is a ``prepare_save_data`` hook rather than an ``_insert_internal``
    override on purpose: ``bulk_create`` also goes through
    ``_prepare_save_data`` (``base/bulk_operations.py:397``) but never through
    ``_insert_internal``, so overriding only the latter leaves bulk-loaded rows
    with the default adapter turning ``[]`` into the JSON *object* ``{}``, which
    then fails to read back.
    """

    __jsonb_list_fields__: ClassVar[tuple[str, ...]] = ()

    def prepare_save_data(self, data: dict, is_new: bool) -> dict:
        for key in self.__jsonb_list_fields__:
            value = data.get(key)
            if isinstance(value, list):
                data[key] = json.dumps(value, ensure_ascii=False)
        return data
