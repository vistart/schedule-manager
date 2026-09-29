"""Offline tests for the per-tool scope gate.

The middleware is exercised directly over ASGI — no database, no socket — so the
decision logic is pinned independently of the transport.
"""

from __future__ import annotations

import json

import pytest

from schedule_manager.auth import TOOL_SCOPES, parse_bearer
from schedule_manager.mcp_server import (
    DRAIN_CEILING_BYTES,
    HEALTH_PATH,
    MAX_BODY_BYTES,
    MCP_PATH,
    PRM_PATH,
    _BodyTooLarge,
    _ScopeEnforcement,
    _drain,
    _discard,
    _header,
    _replay,
)
from schedule_manager.models import SCOPES
from schedule_manager.models.user import READ, WRITE


# ── Token header parsing ─────────────────────────────────────────────────────


class TestParseBearer:
    def test_extracts_credential(self):
        assert parse_bearer("Bearer sm_abc") == "sm_abc"

    def test_scheme_is_case_insensitive(self):
        assert parse_bearer("bearer sm_abc") == "sm_abc"

    def test_strips_surrounding_space(self):
        assert parse_bearer("Bearer   sm_abc  ") == "sm_abc"

    def test_rejects_missing_header(self):
        assert parse_bearer(None) is None
        assert parse_bearer("") is None

    def test_rejects_other_schemes(self):
        assert parse_bearer("Basic dXNlcjpwdw==") is None

    def test_rejects_scheme_without_value(self):
        assert parse_bearer("Bearer") is None
        assert parse_bearer("Bearer   ") is None


# ── Scope table ──────────────────────────────────────────────────────────────


class TestToolScopes:
    def test_every_data_tool_is_mapped(self):
        assert TOOL_SCOPES["get_schedule"] == READ
        assert TOOL_SCOPES["list_schedules"] == READ
        assert TOOL_SCOPES["search_schedules"] == READ
        assert TOOL_SCOPES["create_schedule"] == WRITE
        assert TOOL_SCOPES["update_schedule"] == WRITE
        assert TOOL_SCOPES["delete_schedule"] == WRITE
        assert TOOL_SCOPES["complete_schedule"] == WRITE

    def test_whoami_needs_no_scope(self):
        """A token with no grants must still be able to ask who it is."""
        assert "whoami" not in TOOL_SCOPES

    def test_scopes_are_from_the_closed_vocabulary(self):
        assert set(TOOL_SCOPES.values()) <= set(SCOPES)


# ── Body drain / replay ──────────────────────────────────────────────────────


class TestBodyReplay:
    async def test_drain_collects_the_whole_body(self):
        chunks = [
            {"type": "http.request", "body": b'{"a":', "more_body": True},
            {"type": "http.request", "body": b"1}", "more_body": False},
        ]
        queue = list(chunks)

        async def receive():
            return queue.pop(0)

        body, messages = await _drain(receive, 1024)
        assert body == b'{"a":1}'
        assert messages == chunks

    async def test_replay_defers_to_the_original_receive(self):
        """After the body, the app polls for disconnects; do not fake one."""
        body_msg = {"type": "http.request", "body": b"x", "more_body": False}
        disconnect = {"type": "http.disconnect"}
        seen = []

        async def original():
            seen.append("original")
            return disconnect

        replay = _replay([body_msg], original)
        assert await replay() == body_msg
        assert await replay() == disconnect
        assert seen == ["original"]


def _header_test():
    scope = {"headers": [(b"host", b"x"), (b"authorization", b"Bearer sm_1")]}
    assert _header(scope, "authorization") == "Bearer sm_1"
    assert _header(scope, "missing") is None


class TestHeaderLookup:
    def test_case_insensitive(self):
        _header_test()


# ── The gate itself ──────────────────────────────────────────────────────────


class _Recorder:
    """Collects what a downstream ASGI app would have sent."""

    def __init__(self):
        self.status = None
        self.headers: dict[bytes, bytes] = {}
        self.body = b""
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True
        await _respond(send, 200, {"x": "y"}, b"downstream")


async def _respond(send, status, headers, body):
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [(k.encode(), v.encode()) for k, v in headers.items()],
        }
    )
    await send({"type": "http.response.body", "body": body})


class _Send:
    """Single-argument ASGI ``send`` recorder."""

    def __init__(self):
        self.status = None
        self.headers: dict[str, str] = {}
        self.body = b""

    async def __call__(self, message):
        if message["type"] == "http.response.start":
            self.status = message["status"]
            for key, value in message["headers"]:
                self.headers[key.decode().lower()] = value.decode()
        elif message["type"] == "http.response.body":
            self.body += message.get("body", b"")


def _scope(authorization: str | None = None, method="POST", path=MCP_PATH):
    headers = [(b"content-type", b"application/json")]
    if authorization is not None:
        headers.append((b"authorization", authorization.encode()))
    return {"type": "http", "method": method, "path": path, "headers": headers}


def _body(payload: dict) -> list:
    return [{"type": "http.request", "body": json.dumps(payload).encode(), "more_body": False}]


class TestScopeGate:
    """The gate must run before the downstream app, and only for tools/call.

    Every case here installs a pool; without one the gate short-circuits and the
    assertions below would hold for the wrong reason.
    """

    @pytest.fixture(autouse=True)
    def _pool(self, monkeypatch):
        from contextlib import asynccontextmanager

        from schedule_manager import mcp_server

        class _Pool:
            def connection(self):
                @asynccontextmanager
                async def _ctx():
                    yield object()

                return _ctx()

        monkeypatch.setattr(mcp_server, "_POOL", _Pool())

    @staticmethod
    def _send() -> _Send:
        return _Send()

    async def test_prm_lists_the_full_vocabulary(self):
        downstream = _Recorder()
        send = self._send()
        gate = _ScopeEnforcement(downstream, "http://x:8000")
        await gate(_scope(method="GET", path=PRM_PATH), _replay([], _empty), send)
        document = json.loads(send.body)
        assert document["scopes_supported"] == list(SCOPES)
        assert document["bearer_methods_supported"] == ["header"]
        assert send.status == 200
        assert downstream.called is False

    async def test_non_mcp_path_passes_through(self):
        downstream = _Recorder()
        gate = _ScopeEnforcement(downstream, "http://x:8000")
        await gate(_scope(path="/health"), _replay([], _empty), _noop)
        assert downstream.called is True

    async def test_get_passes_through_without_a_token(self):
        downstream = _Recorder()
        gate = _ScopeEnforcement(downstream, "http://x:8000")
        await gate(_scope(method="GET"), _replay([], _empty), _noop)
        assert downstream.called is True

    async def test_tools_list_needs_no_scope(self):
        downstream = _Recorder()
        gate = _ScopeEnforcement(downstream, "http://x:8000")
        await gate(_scope("Bearer sm_x"), _replay(_body({"method": "tools/list"}), _empty), _noop)
        assert downstream.called is True

    async def test_malformed_body_passes_through(self):
        downstream = _Recorder()
        gate = _ScopeEnforcement(downstream, "http://x:8000")
        messages = [{"type": "http.request", "body": b"not json", "more_body": False}]
        await gate(_scope("Bearer sm_x"), _replay(messages, _empty), _noop)
        assert downstream.called is True

    async def test_unresolvable_token_is_left_to_the_sdk(self, monkeypatch):
        """A token that does not resolve must not be answered with 403.

        The gate has a pool here, so the credential really is looked up; the
        lookup is stubbed to fail the way an unknown or revoked token does.
        """
        from schedule_manager.errors import Unauthenticated
        from schedule_manager.models import User

        async def resolve(_token):
            raise Unauthenticated("unknown token")

        monkeypatch.setattr(User, "resolve_token", staticmethod(resolve))

        downstream = _Recorder()
        send = _Send()
        gate = _ScopeEnforcement(downstream, "http://x:8000")
        await gate(
            _scope("Bearer sm_x"),
            _replay(_body({"method": "tools/call", "params": {"name": "create_schedule"}}), _empty),
            send,
        )
        assert downstream.called is True
        assert send.body == b"downstream"

    async def test_whoami_is_never_gated(self):
        """Even with an unresolvable token, whoami reaches the SDK for a 401."""
        downstream = _Recorder()
        gate = _ScopeEnforcement(downstream, "http://x:8000")
        await gate(
            _scope("Bearer sm_x"),
            _replay(_body({"method": "tools/call", "params": {"name": "whoami"}}), _empty),
            _noop,
        )
        assert downstream.called is True


async def _empty():
    return {"type": "http.disconnect"}


async def _noop(message):
    return None


class TestScopeGateEnforcement:
    """The 403 branch, with credential resolution stubbed so it needs no database."""

    @staticmethod
    def _gate(monkeypatch, granted: tuple[str, ...], downstream):
        """Build a gate whose credential lookup is stubbed.

        ``monkeypatch`` is not optional here: replacing ``User.resolve_token``
        on the class outlives the test and silently disables real credential
        checking for everything that runs afterwards.
        """
        from schedule_manager import mcp_server
        from schedule_manager.models import User

        class _Pool:
            def connection(self):
                from contextlib import asynccontextmanager

                @asynccontextmanager
                async def _ctx():
                    yield object()

                return _ctx()

        class _Record:
            """Stands in for the resolved ApiToken.

            Carries the owner fields as derived-field attributes and the scopes
            as ``scope_list`` — the shape ``resolve_token`` now returns, rather
            than a ``(User, ApiToken)`` pair.
            """

            id = 7
            user_id = 1
            scope_list = None

        async def resolve(_token):
            record = _Record()
            # NULL and [] must behave the same: ARRAY_AGG over no rows is NULL.
            record.scope_list = None if granted is None else list(granted)
            return record

        monkeypatch.setattr(User, "resolve_token", staticmethod(resolve))
        monkeypatch.setattr(mcp_server, "_POOL", _Pool())
        return _ScopeEnforcement(downstream, "https://mcp.example.com")

    @staticmethod
    def _gate_invalid(monkeypatch, error, downstream):
        """A gate whose token always fails to resolve."""
        from schedule_manager import mcp_server
        from schedule_manager.models import User

        class _Pool:
            def connection(self):
                from contextlib import asynccontextmanager

                @asynccontextmanager
                async def _ctx():
                    yield object()

                return _ctx()

        async def resolve(_token):
            raise error

        monkeypatch.setattr(User, "resolve_token", staticmethod(resolve))
        monkeypatch.setattr(mcp_server, "_POOL", _Pool())
        return _ScopeEnforcement(downstream, "https://mcp.example.com")

    async def test_invalid_token_gets_401_not_403(self, monkeypatch):
        """A token string that does not resolve is an authentication failure."""
        from schedule_manager.errors import Unauthenticated

        downstream = _Recorder()
        send = _Send()
        gate = self._gate_invalid(monkeypatch, Unauthenticated("unknown token"), downstream)
        payload = {"method": "tools/call", "params": {"name": "create_schedule"}}
        await gate(_scope("Bearer sm_bogus"), _replay(_body(payload), _empty), send)
        assert downstream.called is True
        assert send.body == b"downstream"

    async def test_revoked_token_gets_401_not_403(self, monkeypatch):
        from schedule_manager.errors import Unauthenticated

        downstream = _Recorder()
        send = _Send()
        gate = self._gate_invalid(monkeypatch, Unauthenticated("token has been revoked"), downstream)
        payload = {"method": "tools/call", "params": {"name": "create_schedule"}}
        await gate(_scope("Bearer sm_revoked"), _replay(_body(payload), _empty), send)
        assert downstream.called is True

    async def test_read_only_token_is_refused_a_write_tool(self, monkeypatch):
        downstream = _Recorder()
        gate = self._gate(monkeypatch, (READ,), downstream)
        send = _Send()
        payload = {"method": "tools/call", "params": {"name": "create_schedule"}}
        await gate(_scope("Bearer sm_ro"), _replay(_body(payload), _empty), send)

        assert send.status == 403
        assert downstream.called is False
        challenge = send.headers["www-authenticate"]
        assert 'error="insufficient_scope"' in challenge
        assert f'scope="{WRITE}"' in challenge
        assert "resource_metadata=" in challenge
        assert json.loads(send.body)["error"] == "insufficient_scope"

    async def test_read_only_token_may_read(self, monkeypatch):
        downstream = _Recorder()
        gate = self._gate(monkeypatch, (READ,), downstream)
        send = _Send()
        payload = {"method": "tools/call", "params": {"name": "list_schedules"}}
        await gate(_scope("Bearer sm_ro"), _replay(_body(payload), _empty), send)
        # Delegated: the gate sent nothing itself, the stub downstream answered 200.
        assert downstream.called is True
        assert send.body == b"downstream"

    async def test_write_tool_is_allowed_with_the_scope(self, monkeypatch):
        downstream = _Recorder()
        gate = self._gate(monkeypatch, (READ, WRITE), downstream)
        send = _Send()
        payload = {"method": "tools/call", "params": {"name": "delete_schedule"}}
        await gate(_scope("Bearer sm_rw"), _replay(_body(payload), _empty), send)
        assert downstream.called is True

    async def test_whoami_allowed_with_no_grants(self, monkeypatch):
        downstream = _Recorder()
        gate = self._gate(monkeypatch, (), downstream)
        send = _Send()
        payload = {"method": "tools/call", "params": {"name": "whoami"}}
        await gate(_scope("Bearer sm_bare"), _replay(_body(payload), _empty), send)
        assert downstream.called is True

    async def test_read_tool_refused_without_the_read_scope(self, monkeypatch):
        downstream = _Recorder()
        gate = self._gate(monkeypatch, (WRITE,), downstream)
        send = _Send()
        payload = {"method": "tools/call", "params": {"name": "get_schedule"}}
        await gate(_scope("Bearer sm_wo"), _replay(_body(payload), _empty), send)
        assert send.status == 403
        assert f'scope="{READ}"' in send.headers["www-authenticate"]


class TestMalformedEnvelope:
    """The whole JSON-RPC envelope is caller-controlled.

    Anything unexpected must fall through to the SDK for a clean 400/404, not
    raise inside the authentication path.
    """

    @staticmethod
    async def _run(monkeypatch, payload, downstream, send):
        """Run the gate for real.

        A pool must be installed: without one the gate short-circuits before it
        ever looks at the body, which would make every case here pass for the
        wrong reason.
        """
        from schedule_manager import mcp_server

        monkeypatch.setattr(mcp_server, "_POOL", object())
        gate = _ScopeEnforcement(downstream, "http://x:8000")
        raw = json.dumps(payload).encode()
        messages = [{"type": "http.request", "body": raw, "more_body": False}]
        await gate(_scope("Bearer sm_x"), _replay(messages, _empty), send)
        return downstream

    @pytest.mark.parametrize(
        "payload",
        [
            {"method": "tools/call", "params": ["create_schedule"]},
            {"method": "tools/call", "params": "x"},
            {"method": "tools/call", "params": {"name": ["create_schedule"]}},
            {"method": "tools/call", "params": {"name": 123}},
            {"method": "tools/call", "params": {"name": None}},
            {"method": "tools/call", "params": None},
            {"method": "tools/call"},
            {"method": "tools/call", "params": {"name": "not_a_tool"}},
            [1, 2, 3],
            "a string",
            None,
        ],
    )
    async def test_malformed_envelope_reaches_the_sdk(self, monkeypatch, payload):
        downstream = _Recorder()
        send = _Send()
        await self._run(monkeypatch, payload, downstream, send)
        assert downstream.called is True
        assert send.body == b"downstream"


class TestBodyLimit:
    async def test_drain_stops_at_the_limit(self):
        chunk = {"type": "http.request", "body": b"x" * 100, "more_body": True}
        queue = [chunk] * 50

        async def receive():
            return queue.pop(0)

        with pytest.raises(_BodyTooLarge):
            await _drain(receive, 1024)

    async def test_drain_passes_a_small_body(self):
        async def receive():
            return {"type": "http.request", "body": b"{}", "more_body": False}

        body, _ = await _drain(receive, 1024)
        assert body == b"{}"

    async def test_oversized_body_is_refused_with_413(self, monkeypatch):
        from schedule_manager import mcp_server

        downstream = _Recorder()
        send = _Send()
        # The gate skips itself when no pool is up, so one has to be installed for
        # the size check to be reachable at all.
        monkeypatch.setattr(mcp_server, "_POOL", object())
        gate = _ScopeEnforcement(downstream, "http://x:8000")

        async def receive():
            return {"type": "http.request", "body": b"x" * (MAX_BODY_BYTES + 1)}

        await gate(_scope("Bearer sm_x"), receive, send)
        assert send.status == 413
        assert downstream.called is False

    async def test_the_limit_is_far_below_the_sdk_cap(self):
        assert MAX_BODY_BYTES < 4 * 1024 * 1024

    async def test_refused_body_is_drained_so_the_413_can_arrive(self, monkeypatch):
        """A refused upload still has to be read to its end.

        Answering mid-upload forces a close, and a close over unread bytes is an
        RST: the client keeps the status line and loses the body.  Clients that
        send ``Connection: close`` see that as ``IncompleteRead``.
        """
        from schedule_manager import mcp_server

        class _Pool:
            def connection(self):
                from contextlib import asynccontextmanager

                @asynccontextmanager
                async def _ctx():
                    yield object()

                return _ctx()

        monkeypatch.setattr(mcp_server, "_POOL", _Pool())

        downstream = _Recorder()
        send = _Send()
        gate = _ScopeEnforcement(downstream, "http://x:8000")

        queue = [
            {"type": "http.request", "body": b"x" * (MAX_BODY_BYTES + 1), "more_body": True},
            {"type": "http.request", "body": b"x" * 100, "more_body": True},
            {"type": "http.request", "body": b"x", "more_body": False},
        ]

        async def receive():
            return queue.pop(0)

        await gate(_scope("Bearer sm_x"), receive, send)
        assert send.status == 413
        assert json.loads(send.body) == {"error": "payload_too_large"}
        assert queue == [], "the refused body must be read to its end"

    async def test_a_body_that_arrived_whole_is_not_read_again(self, monkeypatch):
        """The whole body can arrive in one message.

        Draining it anyway asks for a message the client will never send, which
        hangs until the client times out — the refusal has to notice that the
        upload is already finished.
        """
        from schedule_manager import mcp_server

        class _Pool:
            def connection(self):
                from contextlib import asynccontextmanager

                @asynccontextmanager
                async def _ctx():
                    yield object()

                return _ctx()

        monkeypatch.setattr(mcp_server, "_POOL", _Pool())

        downstream = _Recorder()
        send = _Send()
        gate = _ScopeEnforcement(downstream, "http://x:8000")

        async def receive():
            return {
                "type": "http.request",
                "body": b"x" * (MAX_BODY_BYTES + 1),
                "more_body": False,
            }

        await gate(_scope("Bearer sm_x"), receive, send)
        assert send.status == 413
        assert json.loads(send.body) == {"error": "payload_too_large"}

    async def test_drain_reports_whether_more_remained(self):
        chunk = {"type": "http.request", "body": b"x" * 100, "more_body": True}

        async def receive():
            return chunk

        with pytest.raises(_BodyTooLarge) as caught:
            await _drain(receive, 10)
        assert caught.value.more is True

        async def complete():
            return {"type": "http.request", "body": b"x" * 100, "more_body": False}

        with pytest.raises(_BodyTooLarge) as caught:
            await _drain(complete, 10)
        assert caught.value.more is False

    async def test_the_drain_ceiling_exceeds_the_refusal_limit(self):
        """Otherwise a body one byte over the limit could never be drained."""
        assert DRAIN_CEILING_BYTES > MAX_BODY_BYTES

    async def test_draining_stops_at_the_ceiling(self):
        """A body past the ceiling is abandoned rather than read forever."""
        chunks = [{"type": "http.request", "body": b"x" * 100, "more_body": True}] * 50
        queue = list(chunks)

        async def receive():
            return queue.pop(0)

        assert await _discard(receive, 250) is False
        assert len(queue) > 0

    async def test_draining_reports_a_body_it_finished(self):
        queue = [{"type": "http.request", "body": b"x" * 10, "more_body": False}]

        async def receive():
            return queue.pop(0)

        assert await _discard(receive, 1024) is True
        assert queue == []


class TestSingleResolutionPerRequest:
    """The scope gate and the SDK verifier must resolve a token once, not twice.

    This is a performance invariant with no functional symptom: if the hand-off
    breaks, every request still works, still enforces scopes correctly, and just
    silently doubles its database round trips.  Nothing else in the suite would
    notice, so the count is asserted directly.
    """

    @staticmethod
    def _request():
        return {
            "type": "http.request",
            "body": json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "create_schedule", "arguments": {"title": "x"}},
                }
            ).encode(),
        }

    async def test_verifier_reuses_the_gate_result(self, monkeypatch):
        from schedule_manager import auth, mcp_server
        from schedule_manager.models import User

        calls = []

        class _Record:
            user_id = 42
            scope_list = [READ, WRITE]
            expires_at = None

        async def resolve(_token):
            calls.append(_token)
            return _Record()

        monkeypatch.setattr(User, "resolve_token", staticmethod(resolve))

        # Publish the way the gate does, then let the verifier ask.
        reset = auth.publish_resolved("sm_live", _Record())
        try:
            access = await auth.TokenTableVerifier().verify_token("sm_live")
        finally:
            auth.clear_resolved(reset)

        assert calls == []  # no second lookup
        assert access is not None
        assert access.subject == "42"
        assert list(access.scopes) == [READ, WRITE]

    async def test_a_different_token_does_not_reuse(self, monkeypatch):
        """A request presenting another credential must not inherit this one."""
        from schedule_manager import auth
        from schedule_manager.models import User

        calls = []

        class _Record:
            user_id = 42
            scope_list = [READ]
            expires_at = None

        async def resolve(token):
            calls.append(token)
            return _Record()

        monkeypatch.setattr(User, "resolve_token", staticmethod(resolve))

        reset = auth.publish_resolved("sm_mine", _Record())
        try:
            await auth.TokenTableVerifier().verify_token("sm_someone_else")
        finally:
            auth.clear_resolved(reset)

        assert calls == ["sm_someone_else"]

    async def test_the_memo_is_single_use(self, monkeypatch):
        """Consuming once keeps a second resolve_token call visible in tests."""
        from schedule_manager import auth

        class _Record:
            user_id = 42
            scope_list = [READ]
            expires_at = None

        reset = auth.publish_resolved("sm_once", _Record())
        try:
            assert auth.take_resolved("sm_once") is not None
            assert auth.take_resolved("sm_once") is None
        finally:
            auth.clear_resolved(reset)

    async def test_a_failed_resolution_is_never_published(self, monkeypatch):
        """Failures must stay the SDK's to answer, or 401s turn into 403s."""
        from schedule_manager import auth
        from schedule_manager.models import User
        from schedule_manager.errors import Unauthenticated

        async def resolve(_token):
            raise Unauthenticated("unknown token")

        monkeypatch.setattr(User, "resolve_token", staticmethod(resolve))

        assert auth.take_resolved("sm_bad") is None
        assert await auth.TokenTableVerifier().verify_token("sm_bad") is None


class TestHealthProbe:
    """``/healthz`` is 200 only while the process can actually answer a tool call.

    A probe that skips the database reports a container that is running but
    cannot serve; one that returns anything other than 200/503 — a 500 from an
    escaping exception, say — is not what a load balancer or Docker expects.
    """

    @staticmethod
    def _pool(outcome):
        """A pool whose one backend answers ``ping`` with ``outcome``.

        ``outcome`` may be the returned value or an exception to raise, which is
        how the two ways a probe fails are told apart.
        """
        from contextlib import asynccontextmanager

        class _Backend:
            async def ping(self):
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

        class _Pool:
            def connection(self):
                @asynccontextmanager
                async def _ctx():
                    yield _Backend()

                return _ctx()

        return _Pool()

    @staticmethod
    async def _probe(monkeypatch, pool, authorization=None):
        from schedule_manager import mcp_server

        monkeypatch.setattr(mcp_server, "_POOL", pool)
        downstream = _Recorder()
        send = _Send()
        gate = _ScopeEnforcement(downstream, "http://x:8000")
        await gate(
            _scope(authorization, method="GET", path=HEALTH_PATH),
            _replay([], _empty),
            send,
        )
        return send, downstream

    async def test_ok_when_the_database_answers(self, monkeypatch):
        send, downstream = await self._probe(monkeypatch, self._pool(True))
        assert send.status == 200
        assert json.loads(send.body) == {"status": "ok"}
        assert downstream.called is False

    async def test_unavailable_before_the_lifespan_runs(self, monkeypatch):
        """``_POOL`` is None until the lifespan opens it, which is 503 not 500."""
        send, downstream = await self._probe(monkeypatch, None)
        assert send.status == 503
        assert json.loads(send.body) == {"status": "unavailable"}
        assert downstream.called is False

    async def test_unavailable_when_the_database_does_not_answer(self, monkeypatch):
        send, _ = await self._probe(monkeypatch, self._pool(False))
        assert send.status == 503

    async def test_an_escaping_failure_is_still_a_503(self, monkeypatch):
        """A pool that cannot hand out a connection must not become a 500."""
        send, _ = await self._probe(monkeypatch, self._pool(RuntimeError("pool exhausted")))
        assert send.status == 503

    async def test_no_credential_is_required(self, monkeypatch):
        """Nothing polls the probe with a token, so asking for one would report
        a healthy container as broken."""
        from schedule_manager.models import User

        async def resolve(_token):
            raise AssertionError("the health probe must not resolve credentials")

        monkeypatch.setattr(User, "resolve_token", staticmethod(resolve))
        send, downstream = await self._probe(monkeypatch, self._pool(True))
        assert send.status == 200
        assert downstream.called is False

    async def test_a_bogus_token_does_not_change_the_answer(self, monkeypatch):
        send, _ = await self._probe(monkeypatch, self._pool(True), authorization="Bearer sm_bogus")
        assert send.status == 200
