"""Authentication for the MCP server: credential checking and per-tool scopes.

Two distinct jobs, kept apart on purpose:

* :class:`TokenTableVerifier` answers "is this token valid, and who is it?"  It
  is a thin adapter over :meth:`User.resolve_token`, which is the single
  credential check in the codebase — a second implementation inevitably drifts,
  and the copy that forgets ``revoked_at`` is a full authentication bypass.
* :data:`TOOL_SCOPES` answers "may this token call this tool?"  Enforcement
  lives in ``mcp_server._ScopeEnforcementMiddleware`` so that an insufficient
  scope produces an HTTP 403, which is what a client can act on, rather than a
  JSON-RPC error buried inside a 200.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Mapping, Optional, Tuple

from mcp.server.auth.provider import AccessToken, TokenVerifier

from .errors import ScheduleManagerError
from .models import User
from .models.user import READ, WRITE

CLIENT_ID = "schedule-manager"

#: Which scope each tool needs.  ``whoami`` reports the caller's own identity and
#: reveals nothing about other users, so it needs no scope at all — a token with
#: no grants must still be able to find out who it is.
TOOL_SCOPES: Mapping[str, str] = {
    "get_schedule": READ,
    "list_schedules": READ,
    "search_schedules": READ,
    "create_schedule": WRITE,
    "update_schedule": WRITE,
    "delete_schedule": WRITE,
    "complete_schedule": WRITE,
}


def parse_bearer(header: Optional[str]) -> Optional[str]:
    """Extract the credential from an ``Authorization: Bearer`` header."""
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


#: A credential resolved earlier in the *same request*, keyed by the exact token
#: string that produced it.
#:
#: The scope gate has to resolve the token before it can know which scope the
#: tool needs, and the SDK then asks the verifier about the same token again.
#: Neither side can see the other's work, so the token was looked up twice per
#: request — two borrows, two ``SELECT 1`` liveness probes, two round trips.
#:
#: This is a per-request memo, not a cache: it is never read across requests, so
#: revocation and expiry semantics are unchanged.  The key is the presented
#: token, so a request presenting a different credential can never pick up
#: someone else's resolution.
_resolved: ContextVar[Optional[Tuple[str, object]]] = ContextVar(
    "schedule_manager_resolved_token", default=None
)


def publish_resolved(token: str, record) -> Token:
    """Hand a just-resolved credential to whoever asks next in this request.

    Returns a reset token so the caller can scope it to the downstream call
    rather than leaving it set for the rest of the task.
    """
    return _resolved.set((token, record))


def take_resolved(token: str):
    """Return the record published for *token* in this request, or ``None``.

    A miss — different token, never published, or already consumed — falls back
    to a real lookup, so this can only ever save work, never change the answer.
    """
    entry = _resolved.get()
    if entry is None:
        return None
    cached_token, record = entry
    # Comparing the exact credential is the whole safety property here.
    if not _constant_time_equals(cached_token, token):
        return None
    # Consume: a single request resolves once, and re-reading it would mask a
    # second resolve_token call that should have been caught.
    _resolved.set(None)
    return record


def clear_resolved(reset: Token) -> None:
    """Undo :func:`publish_resolved` once the downstream call is done."""
    _resolved.reset(reset)


def _constant_time_equals(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


class TokenTableVerifier(TokenVerifier):
    """Resolves a bearer token against the ``sm_api_tokens`` table."""
    async def verify_token(self, token: str) -> Optional[AccessToken]:
        record = take_resolved(token)
        if record is None:
            try:
                record = await User.resolve_token(token)
            except ScheduleManagerError:
                return None
        return AccessToken(
            token=token,
            client_id=CLIENT_ID,
            # ARRAY_AGG over zero rows is NULL, not an empty list.
            scopes=list(record.scope_list or ()),
            expires_at=record.expires_at,
            subject=str(record.user_id),
        )
