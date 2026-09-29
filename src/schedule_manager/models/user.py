"""User model plus the account-lifecycle mixin that gives it its interface.

``User`` deliberately does **not** set ``__query_class__`` and does **not** mix
in the soft-delete behaviour.  Management code needs cross-user reads, and the
single "is this account live?" flag for this entity is ``is_active`` — a second
mechanism (``deleted_at``) would leave nobody certain which column to filter on.

Consequence, and it is a hard rule: nothing cross-user may be exposed as an MCP
tool.  See ``AGENTS.md``.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, ClassVar, Iterable, Optional, Sequence

from dateutil.tz import tzutc
from rhosocial.activerecord.backend.errors import IntegrityError
from rhosocial.activerecord.base.field_proxy import FieldProxy
from rhosocial.activerecord.field.timestamp import DefaultTimestampMixin
from rhosocial.activerecord.model import AsyncActiveRecord

from ..errors import AccountClosed, Unauthenticated, UsernameTaken
from .token import ApiToken, ApiTokenScope, generate_token, hash_token

# Closed vocabulary: the SDK derives `scopes_supported` for the protected
# resource document from `AuthSettings.required_scopes`, so values stored in
# the database must come from this same set or the two drift apart.
READ = "schedules:read"
WRITE = "schedules:write"
SCOPES: tuple[str, ...] = (READ, WRITE)
DEFAULT_SCOPES: tuple[str, ...] = SCOPES


def _utcnow() -> datetime:
    return datetime.now(tzutc())


@asynccontextmanager
async def transaction() -> AsyncIterator[None]:
    """Run a unit of work in a transaction, joining one that is already open.

    Prefers the pool's ``transaction()``, which already handles reentrancy and
    rollback and is scoped to the current task's connection.  The fallback is
    ``Model.transaction()``, whose manager is instance-level: a nested call on
    a shared backend would commit the enclosing unit's work early and then let
    the outer context commit again.
    """
    # Imported here, not at module scope: db.py imports this module, so a
    # top-level import would be circular.
    from ..db import current_pool

    pool = current_pool()
    if pool is not None:
        async with pool.transaction():
            yield
        return
    backend = User.backend()
    if backend.in_transaction:
        yield
    else:
        async with User.transaction():
            yield


def validate_scopes(scopes: Iterable[str]) -> tuple[str, ...]:
    """Normalise and check ``scopes`` against the closed vocabulary."""
    wanted = tuple(dict.fromkeys(s.strip() for s in scopes if s and s.strip()))
    unknown = sorted(set(wanted) - set(SCOPES))
    if unknown:
        raise ValueError(
            f"unknown scope(s): {', '.join(unknown)}; allowed: {', '.join(SCOPES)}"
        )
    return wanted


#: PostgreSQL SQLSTATE for ``unique_violation``.
UNIQUE_VIOLATION = "23505"


def _is_unique_violation(exc: BaseException) -> bool:
    """Whether ``exc`` is the database rejecting a duplicate ``username``.

    Matching on the constraint *name* is what this used to do, and it never
    worked: the name PostgreSQL generates for a bare ``UNIQUE`` column is
    ``<table>_username_key``, not the ``uq_users_username`` the check spelled, so
    the branch only ever fired through its substring fallback.  Worse, any such
    name is a function of the table name, so it silently stops matching the day
    the table is renamed — a check that looks load-bearing and is not.

    The SQLSTATE is the signal that does not move when the schema does, and it
    distinguishes a unique violation from the other integrity failures the same
    exception class covers — a NOT NULL or CHECK violation must not be reported
    as "username already taken".  The class alone is therefore not enough, and
    the substring test stays only as a last resort for a driver that surfaces
    neither.
    """
    if not isinstance(exc, IntegrityError):
        return False
    cause = exc.__cause__
    sqlstate = getattr(cause, "sqlstate", None)
    if sqlstate is not None:
        return sqlstate == UNIQUE_VIOLATION
    text = str(exc).lower()
    return "unique" in text and "violat" in text


class UserAccountMixin:
    """Account lifecycle and credential verification.  Behaviour only."""

    @classmethod
    async def open_account(
        cls,
        username: str,
        *,
        scopes: Sequence[str] = DEFAULT_SCOPES,
        label: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> tuple["User", str]:
        """Create an account and issue its first token.

        Returns ``(user, plaintext_token)``.  The plaintext is shown once and
        cannot be recovered from the database afterwards.
        """
        handle = (username or "").strip()
        if not handle:
            raise UsernameTaken("username must not be empty")
        granted = validate_scopes(scopes)
        async with transaction():
            user = cls(username=handle)
            try:
                await user.save()
            except Exception as exc:  # noqa: BLE001 - narrowed below
                if _is_unique_violation(exc):
                    raise UsernameTaken(f"username {handle!r} is already taken") from exc
                raise
            plaintext, _token_id = await user.issue_token(
                scopes=granted, label=label, expires_at=expires_at
            )
        return user, plaintext

    async def close_account(self) -> None:
        """Deactivate the account and revoke every token it owns.

        The ``sm_users`` row is kept: ``sm_schedules.user_id`` is ``ON DELETE
        RESTRICT`` and historical attribution has to stay resolvable.  The
        account's schedules are left untouched — they simply stop being
        reachable, because a deactivated user cannot authenticate.
        """
        async with transaction():
            await ApiToken.query().where(
                (ApiToken.c.user_id == self.id) & (ApiToken.c.revoked_at.is_null())
            ).update_all({"revoked_at": _utcnow()})
            self.is_active = False
            await self.save()

    @classmethod
    async def resolve_token(cls, token: str) -> ApiToken:
        """Resolve a bearer token, returning the :class:`ApiToken` record.

        One round trip.  The token row, the owner's ``username``/``is_active``
        and the granted scopes all arrive in a single statement via the derived
        fields on :class:`ApiToken`; fetching them separately cost three.

        Returns the record rather than ``(User, ApiToken)`` on purpose.  The
        only owner data a caller needs is already on the record, and
        reconstructing a ``User`` from three columns would hand out a partially
        hydrated model whose other attributes are unset — a trap for any later
        ``user.save()``.  Callers wanting a real ``User`` should ask for one
        explicitly with :meth:`find_one`.
        """
        if not token or not token.strip():
            raise Unauthenticated("empty token")
        record = await ApiToken.find_one(
            {"token_hash": hash_token(token.strip())},
            derived=True,
        )
        if record is None:
            raise Unauthenticated("unknown token")
        if record.revoked_at is not None:
            raise Unauthenticated("token has been revoked")
        if record.expires_at is not None and record.expires_at <= _utcnow():
            raise Unauthenticated("token has expired")
        if record.owner_is_active is None:
            raise Unauthenticated("token owner no longer exists")
        if not record.owner_is_active:
            raise AccountClosed(f"user {record.owner_username!r} is deactivated")
        return record

    @classmethod
    async def authenticate(cls, token: str) -> "User":
        """Resolve a bearer token to its owner.

        This is the one and only credential check in the codebase — the SDK's
        ``TokenVerifier`` delegates here rather than reimplementing it, because
        two implementations inevitably drift and the one that forgets
        ``revoked_at`` is a full authentication bypass.
        """
        # Deliberately a second query: this is the one caller that needs a real,
        # fully hydrated User.  The hot request path uses resolve_token and
        # never needs the model, so it does not pay for this.
        record = await cls.resolve_token(token)
        return await cls.find_one(record.user_id)

    async def issue_token(
        self,
        *,
        scopes: Optional[Sequence[str]] = None,
        label: Optional[str] = None,
        expires_at: Optional[datetime] = None,
    ) -> tuple[str, int]:
        """Mint a token; return ``(plaintext, token_id)``.

        The plaintext is shown once and is not recoverable from the database.
        """
        granted = validate_scopes(DEFAULT_SCOPES if scopes is None else scopes)
        plaintext, digest = generate_token()
        async with transaction():
            record = ApiToken(
                user_id=self.id, token_hash=digest, label=label, expires_at=expires_at
            )
            await record.save()
            for scope in granted:
                await ApiTokenScope(api_token_id=record.id, scope=scope).save()
        return plaintext, record.id

    async def rotate_token(
        self,
        old_token_id: int,
        **kwargs,
    ) -> tuple[str, int]:
        """Issue a replacement for ``old_token_id`` and revoke the original."""
        async with transaction():
            plaintext, new_id = await self.issue_token(**kwargs)
            await self.revoke_token(old_token_id)
        return plaintext, new_id

    async def revoke_token(self, token_id: int) -> None:
        record = await ApiToken.find_one(token_id)
        if record is None:
            raise Unauthenticated(f"token {token_id} not found")
        if record.user_id != self.id:
            raise Unauthenticated(f"token {token_id} does not belong to this user")
        if record.revoked_at is None:
            record.revoked_at = _utcnow()
            await record.save()

    async def token_scopes(self, token_id: int) -> list[str]:
        rows = await ApiTokenScope.query().where(
            ApiTokenScope.c.api_token_id == token_id
        ).all()
        return [row.scope for row in rows]

    async def has_scope(self, token_id: int, scope: str) -> bool:
        return scope in await self.token_scopes(token_id)


class User(UserAccountMixin, DefaultTimestampMixin, AsyncActiveRecord):
    """Account holder.  Readable across users by design — see the module docstring."""

    __table_name__ = "sm_users"
    __pk_auto_generated__ = True

    c: ClassVar[FieldProxy] = FieldProxy()

    id: Optional[int] = None
    username: str
    is_active: bool = True
