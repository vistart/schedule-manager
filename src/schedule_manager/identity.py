"""Single exit point for the current identity.

Every read of "who is calling" goes through :func:`current_user_id`.  It
resolves, in order:

1. ``get_access_token()`` — the SDK's request-scoped ContextVar, populated by
   ``AuthContextMiddleware`` on every remote HTTP request after
   ``TokenVerifier.verify_token`` has already validated the bearer token.
2. ``_cli_user`` — a ContextVar the CLI sets once in ``_init()``, because the
   CLI has no HTTP request and therefore no SDK authentication layer.

Both paths re-resolve on every operation; the ContextVar only carries the
already-resolved integer for the duration of one call.  It is a per-invocation
hand-off, not session state.

This module deliberately imports no model, so that ``models`` may depend on it
without creating a cycle.
"""

from __future__ import annotations

from contextvars import ContextVar, Token

from mcp.server.auth.middleware.auth_context import get_access_token

from .errors import Unauthenticated

_cli_user: ContextVar[int | None] = ContextVar("schedule_cli_user", default=None)
_cli_token: ContextVar[str | None] = ContextVar("schedule_cli_token", default=None)


def set_cli_user(user_id: int, token: str) -> Token:
    """Bind ``user_id`` and the presented ``token`` for the CLI process.

    The token is kept so that ``whoami`` can report the scopes actually granted
    to it, rather than the full vocabulary.
    """
    _cli_token.set(token)
    return _cli_user.set(user_id)


def reset_cli_user(token: Token) -> None:
    """Undo a previous :func:`set_cli_user`."""
    _cli_user.reset(token)


def current_cli_token() -> str | None:
    """Return the token this process authenticated with, if any."""
    return _cli_token.get()


def current_user_id() -> int:
    """Return the current user id, or raise :class:`Unauthenticated`."""
    token = get_access_token()
    if token is not None:
        return int(token.subject)
    user_id = _cli_user.get()
    if user_id is None:
        raise Unauthenticated(
            "no verified identity in this context. Pass a token with "
            "--token / SCHEDULE_TOKEN, or send 'Authorization: Bearer <token>' "
            "on a remote MCP request."
        )
    return user_id
