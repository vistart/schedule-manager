"""MCP server exposing schedule management tools over Streamable HTTP.

Identity arrives as a bearer token in the ``Authorization`` header.  The SDK's
``AuthContextMiddleware`` verifies it before any tool runs and publishes the
result on a request-scoped ContextVar, so tools never see a token and never
take an identity argument — ``Schedule``'s query class reads the verified
subject through :func:`schedule_manager.identity.current_user_id`.

No MCP tool may enumerate across users.  ``User`` and ``ApiToken`` are not
user-scoped models (management code needs cross-user reads), so exposing them
as tools would be a full user dump.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import datetime
from typing import AsyncIterator, Optional

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings
from mcp.server.context import ServerMiddleware
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from .auth import (
    TOOL_SCOPES,
    TokenTableVerifier,
    clear_resolved,
    parse_bearer,
    publish_resolved,
)
from .config import configure_logging, get_server_config
from .db import close_pool, connection, create_pool
from .errors import ScheduleManagerError
from .identity import current_user_id
from .models import SCOPES, Schedule, User
from .schema import create_all

MCP_PATH = "/mcp"
PRM_PATH = "/.well-known/oauth-protected-resource"
HEALTH_PATH = "/healthz"

#: JSON-RPC call bodies are a few hundred bytes.  The SDK caps the request body
#: at 4 MiB, but this middleware drains it *before* that middleware runs, so the
#: cap has to be re-imposed here or an unauthenticated caller can make the
#: process buffer an arbitrary amount.
MAX_BODY_BYTES = 64 * 1024

#: How much of an already-refused body is read before the connection is dropped
#: regardless.  Matches the SDK's own 4 MiB cap, so a client that is within the
#: limit the SDK would have accepted always fits here.
DRAIN_CEILING_BYTES = 4 * 1024 * 1024

log = logging.getLogger(__name__)

_POOL = None


def auto_migrate() -> bool:
    """Whether to ensure the schema on boot. Off means "run setup-db first"."""
    return os.environ.get("SCHEDULE_AUTO_MIGRATE", "1") != "0"


def worker_count() -> int:
    """Uvicorn worker processes. One worker is a single event loop on one core."""
    try:
        workers = int(os.environ.get("SCHEDULE_WORKERS", "1"))
    except ValueError:
        workers = 1
    return max(1, workers)


def _schedule_to_dict(s: Schedule) -> dict:
    return {
        "id": s.id,
        "title": s.title,
        "description": s.description,
        "status": s.status,
        "priority": s.priority,
        "start_time": s.start_time.isoformat() if s.start_time else None,
        "due_time": s.due_time.isoformat() if s.due_time else None,
        "completed_at": s.completed_at.isoformat() if s.completed_at else None,
        "location": s.location,
        "tags": s.tags or [],
        "rrule": s.rrule,
        "rdate": s.rdate or [],
        "exdate": s.exdate or [],
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
        "is_overdue": s.is_overdue,
    }


@asynccontextmanager
async def lifespan(server: MCPServer) -> AsyncIterator[dict]:
    configure_logging()
    global _POOL
    _POOL = await create_pool()
    try:
        if not auto_migrate():
            # Several instances booting at once would otherwise all issue
            # CREATE TABLE IF NOT EXISTS and contend on the same locks. Schema
            # changes belong in `schedule-manager-setup-db` as a deploy step.
            log.info("skipping schema check (SCHEDULE_AUTO_MIGRATE is off)")
        else:
            async with connection(_POOL) as backend:
                await create_all(backend)
        yield {}
    finally:
        await close_pool(_POOL)


class _ScopeEnforcement:
    """Per-tool scope enforcement, producing an HTTP 403 before dispatch.

    The SDK's ``RequireAuthMiddleware`` demands *every* scope in
    ``AuthSettings.required_scopes``, which makes scopes all-or-nothing: a
    read-only token would be refused even from ``list_schedules``.  So the global
    requirement is left empty and the check is done per operation here, which is
    also what the spec asks for — the challenge then names the scope actually
    missing.

    This wraps the app at the ASGI level because an HTTP status is only available
    there; a ``ServerMiddleware`` would raise inside dispatch and the client
    would see a JSON-RPC error under a 200.  The trade-off is that it runs
    outside the SDK's own middleware, so a request with a forged ``Host`` is
    answered with 403/401 rather than 421.  Nothing is disclosed that the caller
    did not already hold — they presented the token themselves.

    A token that does not resolve is passed through untouched, leaving the 401 to
    the SDK so the two paths cannot drift.

    The same wrapper answers the two unauthenticated GETs that have to exist
    outside the SDK's own paths: the OAuth protected-resource metadata
    (:data:`PRM_PATH`) and the health probe (:data:`HEALTH_PATH`).
    """

    def __init__(self, app, resource_url: str) -> None:
        self.app = app
        self.resource_url = resource_url.rstrip("/")

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if scope.get("method") == "GET" and path.startswith(PRM_PATH):
            await self._send_prm(send)
            return

        if scope.get("method") == "GET" and path == HEALTH_PATH:
            await self._send_health(_POOL, send)
            return

        # The pool is created in the lifespan, which runs after this wrapper is
        # built, so it is read per request rather than captured.
        pool = _POOL
        if scope.get("method") != "POST" or path != MCP_PATH or pool is None:
            await self.app(scope, receive, send)
            return

        try:
            body, messages = await _drain(receive, MAX_BODY_BYTES)
        except _BodyTooLarge as too_large:
            await self._drain_refusal(receive, too_large)
            await self._send_too_large(send)
            return
        try:
            payload = json.loads(body) if body else None
        except ValueError:
            payload = None

        reset = None
        # The connection is held across the downstream call, not just across the
        # lookup below.  ``pool.connection()`` reuses an already-open context, so
        # _PooledConnectionMiddleware (which every request needs anyway) picks this
        # one up instead of borrowing a second.  That second borrow was costing a
        # full ``SELECT 1`` liveness probe — a whole extra round trip on a
        # remote database — for a connection the request was going to take anyway.
        async with AsyncExitStack() as stack:
            if required := self._required_scope(payload):
                header = _header(scope, "authorization")
                token = parse_bearer(header)
                if not token:
                    # No credential at all: let the SDK answer 401.
                    await self.app(scope, _replay(messages, receive), send)
                    return

                await stack.enter_async_context(connection(pool))
                record = None
                try:
                    record = await User.resolve_token(token)
                    # ARRAY_AGG over zero rows is NULL, not an empty list.
                    granted = tuple(record.scope_list or ())
                except ScheduleManagerError:
                    # Present but unusable — unknown, revoked, expired, or the
                    # account is closed.  Delegating keeps every invalid-credential
                    # response in the SDK's hands; answering 403 here would both
                    # misreport the cause and bypass its 401 challenge.
                    record = None

                if record is not None:
                    if required not in granted:
                        await self._send_scope_error(send, required)
                        return

                    # The SDK is about to ask the verifier about this same token.
                    # Hand over what we just resolved so it does not look it up
                    # again.  Only successes are published: a failure must still
                    # be answered by the SDK, and the memo must never outlive this
                    # request.
                    reset = publish_resolved(token, record)

            try:
                await self.app(scope, _replay(messages, receive), send)
            finally:
                if reset is not None:
                    clear_resolved(reset)

    async def _drain_refusal(self, receive, too_large: "_BodyTooLarge") -> None:
        """Read out a body that was refused for being too large.

        Skipped entirely when the body had already arrived in full: there is
        nothing left to read, and asking anyway would block on a message the
        client has no reason to send.
        """
        if not too_large.more:
            return
        try:
            await _discard(receive, DRAIN_CEILING_BYTES)
        except Exception as exc:
            # The peer went away mid-upload.  Answering is then pointless but
            # harmless, and letting it escape would turn a refused request into a
            # 500 that says nothing about why.
            log.debug("client disconnected while discarding an oversized body: %s", exc)

    @staticmethod
    def _required_scope(payload) -> Optional[str]:
        """Which scope this call needs, or ``None`` to defer to the SDK.

        The whole envelope is caller-controlled, so nothing here is trusted to
        have the expected shape: a malformed body has to fall through to the SDK
        for a clean 400 rather than raising inside the authentication path.
        """
        if not isinstance(payload, dict) or payload.get("method") != "tools/call":
            return None
        params = payload.get("params")
        if not isinstance(params, dict):
            return None
        name = params.get("name")
        if not isinstance(name, str):
            return None
        return TOOL_SCOPES.get(name)

    async def _send_prm(self, send) -> None:
        """Serve RFC 9728 metadata with the full scope list.

        The SDK derives ``scopes_supported`` from ``required_scopes``, which is
        empty here, so the document is built locally instead of advertising
        nothing.
        """
        await _send_json(
            send,
            200,
            {
                "resource": self.resource_url,
                "authorization_servers": [self.resource_url],
                "scopes_supported": list(SCOPES),
                "bearer_methods_supported": ["header"],
            },
        )

    async def _send_too_large(self, send) -> None:
        await _send_json(send, 413, {"error": "payload_too_large"})

    async def _send_scope_error(self, send, required: str) -> None:
        challenge = ", ".join(
            [
                'error="insufficient_scope"',
                f'error_description="Required scope: {required}"',
                f'scope="{required}"',
                f'resource_metadata="{self.resource_url}{PRM_PATH}"',
            ]
        )
        await _send_json(
            send,
            403,
            {
                "error": "insufficient_scope",
                "error_description": f"Required scope: {required}",
            },
            extra_headers=[(b"www-authenticate", f"Bearer {challenge}".encode())],
        )

    async def _send_health(self, pool, send) -> None:
        """Report whether this process can actually serve.

        ``pool is None`` means the lifespan has not run, which is the normal
        answer while the process is still starting.  Once there is a pool, the
        database is probed with ``ping`` (``SELECT 1``): a health check that
        never touches the database reports a container that is running but
        unable to answer a single tool call.

        The probe borrows from the same pool the tool calls use, so a saturated
        pool delays it — there is no reserved spare connection, because an idle
        one is exactly what ``SCHEDULE_POOL_MAX`` exists to bound.  Any
        exception is swallowed: a probe that raised would surface as a 500 from
        uvicorn rather than the 503 a load balancer needs to see.
        """
        healthy = False
        if pool is not None:
            try:
                async with connection(pool) as backend:
                    healthy = bool(await backend.ping())
            except Exception as exc:
                log.warning("health check failed: %s", exc)
        await _send_json(
            send, 200 if healthy else 503, {"status": "ok" if healthy else "unavailable"}
        )


async def _send_json(
    send, status: int, payload: dict, extra_headers: Optional[list] = None
) -> None:
    """Emit one complete JSON response.

    These four answers are produced here rather than by the SDK because they
    have to happen before dispatch: a status code is only available at the ASGI
    layer, and the SDK's own error paths would answer 200 with a JSON-RPC error
    body, which is not what a health check or a challenge consumer reads.
    """
    raw = json.dumps(payload).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(raw)).encode()),
                *(extra_headers or ()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": raw})


class _BodyTooLarge(Exception):
    """Raised when the request body exceeds :data:`MAX_BODY_BYTES`.

    ``more`` records whether the body was still incomplete when the limit was
    passed.  It is the difference between "there is more to read" and "the whole
    body has already arrived", and asking for a message that will never come
    blocks until the client gives up.
    """

    def __init__(self, more: bool) -> None:
        super().__init__("request body too large")
        self.more = more


async def _drain(receive, limit: int) -> tuple[bytes, list]:
    """Read the whole request body, keeping the messages for replay.

    Aborts as soon as the limit is passed rather than after buffering
    everything, so the memory is bounded no matter what the caller sends.
    """
    body = b""
    messages = []
    more = True
    while more:
        message = await receive()
        messages.append(message)
        body += message.get("body", b"")
        more = bool(message.get("more_body", False))
        if len(body) > limit:
            raise _BodyTooLarge(more)
    return body, messages


async def _discard(receive, ceiling: int) -> bool:
    """Read and throw away the rest of a request body that was already refused.

    Returns ``True`` when the body was read to its end, ``False`` when it passed
    ``ceiling`` and the connection is being dropped mid-upload.

    Reading it is not politeness, it is what makes the 413 arrive at all.  A
    server that answers while the peer is still uploading has to close the
    connection, and closing a socket that still holds unread bytes in its receive
    buffer is an RST, not a FIN — the client keeps the status line it had already
    parsed and loses the body, surfacing as ``IncompleteRead`` under any client
    that sent ``Connection: close``.  With a keep-alive connection it happens to
    work, which is why this survives casual testing.

    Nothing is accumulated: each chunk is counted and dropped, so the ceiling
    bounds the work rather than the memory.
    """
    read = 0
    more = True
    while more:
        message = await receive()
        read += len(message.get("body", b""))
        more = message.get("more_body", False)
        if read > ceiling:
            return False
    return True


def _replay(messages: list, receive):
    """Hand the buffered body back, then defer to the original ``receive``.

    Delegating matters: once the body is consumed the downstream app keeps
    calling ``receive()`` to watch for a disconnect.  Synthesising
    ``http.disconnect`` at that point makes the server tear the request down
    mid-response.
    """
    queue = list(messages)

    async def replay():
        if queue:
            return queue.pop(0)
        return await receive()

    return replay


def _header(scope: dict, name: str) -> Optional[str]:
    """Read a request header.

    ASGI header names arrive as bytes, so the lookup key is encoded before the
    comparison; passing the plain name here used to silently match nothing, which
    read as "no token" and therefore as "no scope check".
    """
    wanted = name.lower().encode()
    for key, value in scope.get("headers", ()):
        if key.lower() == wanted:
            return value.decode("latin-1")
    return None


class _PooledConnectionMiddleware(ServerMiddleware):
    """Gives every inbound request its own pooled connection.

    A public server handles requests concurrently, so the connection has to be
    scoped to the request rather than to the process: one process-wide
    connection would interleave concurrent tool calls and their transactions.
    Inside this, every model resolves to the same backend for that request.
    """

    async def __call__(self, ctx, call_next):
        if _POOL is None:
            return await call_next(ctx)
        async with connection(_POOL):
            return await call_next(ctx)



def build_server() -> MCPServer:
    """Construct the server with authentication wired in."""
    server_config = get_server_config()
    return MCPServer(
        "schedule-manager",
        instructions=(
            "Schedule management system, scoped to the authenticated user. "
            "Create, read, update, delete, and search schedules with pagination, "
            "filtering, and sorting. Every tool sees only the caller's own "
            "schedules; call whoami to see who you are acting for."
        ),
        lifespan=lifespan,
        middleware=[_PooledConnectionMiddleware()],
        token_verifier=TokenTableVerifier(),
        auth=AuthSettings(
            issuer_url=server_config.public_url,
            resource_server_url=server_config.public_url,
            # Left empty on purpose: the SDK enforces these globally, which would
            # make every scope all-or-nothing.  _ScopeEnforcement checks per
            # tool instead, so a read-only token can still read.
            required_scopes=[],
        ),
    )


mcp = build_server()


@mcp.tool()
async def whoami() -> dict:
    """Report which user this session is acting as, and what the token may do.

    Returns the authenticated user's id and username together with the scopes
    granted to the presented token. Useful for confirming the session is
    operating on the right account.
    """
    token = get_access_token()
    user = await User.find_one(current_user_id())
    return {
        "user_id": user.id,
        "username": user.username,
        "client_id": token.client_id if token else None,
        "scopes": list(token.scopes) if token else [],
        "expires_at": token.expires_at.isoformat()
        if token and token.expires_at
        else None,
    }


@mcp.tool()
async def create_schedule(
    title: str,
    description: Optional[str] = None,
    status: str = "pending",
    priority: int = 3,
    start_time: Optional[str] = None,
    due_time: Optional[str] = None,
    location: Optional[str] = None,
    tags: Optional[list] = None,
    rrule: Optional[str] = None,
) -> dict:
    """Create a new schedule owned by the authenticated user.

    Args:
        title: Schedule title (required, non-empty).
        description: Detailed description of the schedule.
        status: Initial status (pending, in_progress, completed, cancelled). Default: pending.
        priority: Priority level 1 (highest) to 5 (lowest). Default: 3.
        start_time: ISO 8601 datetime for when the schedule starts.
        due_time: ISO 8601 datetime for the deadline.
        location: Physical or virtual location.
        tags: List of tag strings.
        rrule: RFC 5545 recurrence rule string (e.g., "FREQ=WEEKLY;BYDAY=MO").

    Returns:
        The created schedule as a dictionary.
    """
    s = Schedule(title=title, description=description, status=status, priority=priority)
    if start_time:
        s.start_time = datetime.fromisoformat(start_time)
    if due_time:
        s.due_time = datetime.fromisoformat(due_time)
    if location:
        s.location = location
    if tags:
        s.tags = tags
    if rrule:
        s.rrule = rrule
    await s.save()
    return _schedule_to_dict(s)


@mcp.tool()
async def get_schedule(schedule_id: int) -> dict:
    """Get one of the authenticated user's schedules by its ID.

    Args:
        schedule_id: The numeric ID of the schedule.

    Returns:
        The schedule as a dictionary, or an error message if it does not exist
        or belongs to another user.
    """
    s = await Schedule.find_one(schedule_id)
    if s is None:
        return {"error": f"Schedule {schedule_id} not found"}
    return _schedule_to_dict(s)


@mcp.tool()
async def update_schedule(
    schedule_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    status: Optional[str] = None,
    priority: Optional[int] = None,
    start_time: Optional[str] = None,
    due_time: Optional[str] = None,
    location: Optional[str] = None,
    tags: Optional[list] = None,
) -> dict:
    """Update one of the authenticated user's schedules. Only provided fields change.

    Args:
        schedule_id: The numeric ID of the schedule to update.
        title: New title.
        description: New description.
        status: New status (pending, in_progress, completed, cancelled).
        priority: New priority level 1-5.
        start_time: New ISO 8601 start time.
        due_time: New ISO 8601 due time.
        location: New location.
        tags: New list of tags.

    Returns:
        The updated schedule as a dictionary, or an error message if it does not
        exist or belongs to another user.
    """
    s = await Schedule.find_one(schedule_id)
    if s is None:
        return {"error": f"Schedule {schedule_id} not found"}
    if title is not None:
        s.title = title
    if description is not None:
        s.description = description
    if status is not None:
        s.status = status
    if priority is not None:
        s.priority = priority
    if start_time is not None:
        s.start_time = datetime.fromisoformat(start_time)
    if due_time is not None:
        s.due_time = datetime.fromisoformat(due_time)
    if location is not None:
        s.location = location
    if tags is not None:
        s.tags = tags
    await s.save()
    return _schedule_to_dict(s)


@mcp.tool()
async def delete_schedule(schedule_id: int) -> dict:
    """Soft-delete one of the authenticated user's schedules.

    Args:
        schedule_id: The numeric ID of the schedule to delete.

    Returns:
        Confirmation message or error.
    """
    s = await Schedule.find_one(schedule_id)
    if s is None:
        return {"error": f"Schedule {schedule_id} not found"}
    await s.delete()
    return {"message": f"Schedule {schedule_id} deleted"}


@mcp.tool()
async def list_schedules(
    page: int = 1,
    page_size: int = 20,
    status: Optional[str] = None,
    priority: Optional[int] = None,
    keyword: Optional[str] = None,
    sort_by: str = "due_time",
    sort_order: str = "asc",
) -> dict:
    """List the authenticated user's schedules with pagination, filtering, and sorting.

    Args:
        page: Page number (1-based). Default: 1.
        page_size: Number of items per page. Default: 20.
        status: Filter by status (pending, in_progress, completed, cancelled).
        priority: Filter by priority level (1-5).
        keyword: Search keyword (matches title and description).
        sort_by: Field to sort by (due_time, created_at, updated_at, priority, title). Default: due_time.
        sort_order: Sort direction (asc or desc). Default: asc.

    Returns:
        Dictionary with items, total count, current page, and page size.
    """
    items, total, page, page_size = await Schedule.list_page(
        page=page,
        page_size=page_size,
        status=status,
        priority=priority,
        keyword=keyword,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    return {
        "items": [_schedule_to_dict(s) for s in items],
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": (total + page_size - 1) // page_size if page_size > 0 else 0,
    }


@mcp.tool()
async def complete_schedule(schedule_id: int) -> dict:
    """Mark one of the authenticated user's schedules as completed.

    Args:
        schedule_id: The numeric ID of the schedule to complete.

    Returns:
        The updated schedule, or an error message if it does not exist or
        belongs to another user.
    """
    s = await Schedule.find_one(schedule_id)
    if s is None:
        return {"error": f"Schedule {schedule_id} not found"}
    await s.complete()
    await s.save()
    return _schedule_to_dict(s)


@mcp.tool()
async def search_schedules(keyword: str, page: int = 1, page_size: int = 20) -> dict:
    """Search the authenticated user's schedules by keyword (title and description).

    Results are ordered by most recently updated first.  Paginated: the response
    reports whether more matches exist rather than a total count, so a full page
    never has to be mistaken for the whole result set.

    Args:
        keyword: Search term to match against title and description.
        page: Page number (1-based). Default: 1.
        page_size: Items per page, 1-100. Default: 20.

    Returns:
        Dictionary with items, has_more, page, and page_size.  If has_more is
        true, call again with the next page.
    """
    items, has_more = await Schedule.search(keyword, page=page, page_size=page_size)
    return {
        "items": [_schedule_to_dict(s) for s in items],
        "has_more": has_more,
        "page": page,
        "page_size": page_size,
    }


def build_asgi_app():
    """Build the ASGI application.

    Exposed as a module-level factory so uvicorn's multiprocess supervisor can
    re-import it in each worker: with more than one worker, ``uvicorn.run``
    requires an import string rather than a live object, and each worker gets
    its own event loop and its own connection pool.

    The pool is created in the lifespan, not here, because that runs inside the
    worker's event loop.
    """
    server_config = get_server_config()
    inner = mcp.streamable_http_app(
        stateless_http=True,
        host=server_config.bind_host,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=server_config.effective_allowed_hosts(),
            allowed_origins=server_config.effective_allowed_origins(),
        ),
    )
    return _ScopeEnforcement(inner, server_config.public_url)


def run_server() -> None:
    """Serve over Streamable HTTP.

    DNS-rebinding protection is only switched on automatically for a loopback
    bind host (``mcpserver/server.py:1144-1150``), so on a real hostname it has
    to be passed explicitly or the endpoint is left unprotected.

    One worker pins the service to a single core and a single event loop, which
    is the throughput ceiling long before the database becomes one.  Workers
    scale that out; each one opens its own pool, so the connection count the
    server needs is ``workers * pool_max``.
    """
    server_config = get_server_config()
    workers = worker_count()

    import uvicorn

    if workers > 1:
        # Multiprocess mode re-imports the target per worker, so it has to be an
        # import string; a live app object cannot be shared across processes.
        uvicorn.run(
            f"{__name__}:build_asgi_app",
            factory=True,
            host=server_config.bind_host,
            port=server_config.bind_port,
            workers=workers,
            log_level=os.environ.get("SCHEDULE_UVICORN_LOG_LEVEL", "warning"),
        )
    else:
        uvicorn.run(
            build_asgi_app(),
            host=server_config.bind_host,
            port=server_config.bind_port,
        )


def main() -> None:
    configure_logging()
    try:
        run_server()
    except Exception as exc:
        print(f"schedule-manager-mcp failed to start: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
