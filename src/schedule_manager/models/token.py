"""API tokens and their granted scopes.

Tokens are opaque high-entropy strings.  Only the SHA-256 digest is stored, so
a database disclosure does not hand out live credentials.  The digest is a
plain indexed equality lookup, which is why no slow password hash is needed:
this is a 256-bit random secret, not a low-entropy passphrase.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, ClassVar, Optional

from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.base.fields import DerivedField
from rhosocial.activerecord.backend.expression.core import Subquery
from rhosocial.activerecord.backend.expression.functions.array import array_agg
from rhosocial.activerecord.field.composite_pk import CompositePKMixin
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import AsyncActiveRecord

if TYPE_CHECKING:  # pragma: no cover - import cycle only matters to type checkers
    from .user import User

TOKEN_PREFIX = "sm_"


def generate_token() -> tuple[str, str]:
    """Return ``(plaintext, sha256_hexdigest)``.

    The plaintext exists only in memory and in the caller's return value; it is
    never recoverable from the database.
    """
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return plaintext, hash_token(plaintext)


def hash_token(token: str) -> str:
    """Return the stored digest for ``token``."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _owner_is_active(dialect) -> Subquery:
    """Correlated subquery for the owner's ``is_active``.

    Imported lazily: ``user.py`` imports this module, so a top-level import
    back would be a cycle.  The factory only runs at query time, by which point
    both modules are loaded.
    """
    from .user import User

    return Subquery(
        dialect,
        User.query()
        .select(User.c.is_active)
        .where(User.c.id == ApiToken.c.user_id)
        .to_sql(),
    )


def _scope_list(dialect) -> Subquery:
    """Correlated subquery aggregating the token's granted scopes."""
    return Subquery(
        dialect,
        ApiTokenScope.query()
        .select(array_agg(dialect, ApiTokenScope.c.scope))
        .where(ApiTokenScope.c.api_token_id == ApiToken.c.id)
        .to_sql(),
    )


def _owner_username(dialect) -> Subquery:
    """Correlated subquery for the owner's ``username``.

    Only needed because the deactivated-account error message names the user,
    and a second query just to render an error string is not worth it.
    """
    from .user import User

    return Subquery(
        dialect,
        User.query()
        .select(User.c.username)
        .where(User.c.id == ApiToken.c.user_id)
        .to_sql(),
    )


class ApiToken(DefaultTimestampMixin, AsyncActiveRecord):
    """A single bearer credential.  Invalidation is ``revoked_at``, not deletion.

    ``owner_is_active`` and ``scope_list`` are derived fields, not columns: they
    are computed by correlated subqueries in the same statement that finds the
    token, so a full credential check costs one round trip instead of three.

    They are deliberately subqueries rather than a join.  A ``JOIN users`` would
    put a non-scoped model in the FROM clause, where the user filter has a
    handle to reach it; a subquery's FROM is closed, so isolation here is a
    property of the SQL shape rather than something to remember.

    Pass ``derived=True`` (or ``derived="all"``) to ``find_one``/``find_all`` to
    select them — they are opt-in and cost a subquery each.
    """

    __table_name__ = "sm_api_tokens"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: Optional[int] = None
    user_id: int
    token_hash: str
    label: Optional[str] = None
    expires_at: Optional[datetime] = None
    revoked_at: Optional[datetime] = None

    owner_is_active: ClassVar[Annotated[Optional[bool], DerivedField(_owner_is_active)]]
    owner_username: ClassVar[Annotated[Optional[str], DerivedField(_owner_username)]]
    scope_list: ClassVar[Annotated[Optional[list], DerivedField(_scope_list)]]


class ApiTokenScope(CompositePKMixin, AsyncActiveRecord):
    """One granted scope per row; a token with no rows can do nothing."""

    __table_name__ = "sm_api_token_scopes"
    __primary_key__ = ("api_token_id", "scope")

    c: ClassVar[FieldProxy] = FieldProxy()

    api_token_id: int
    scope: str
