"""Offline contract tests: identity resolution, generated SQL, DDL, validation.

None of these need a database.  They are the load-bearing ones — they assert
that every read path is forced through the current identity, and they do it by
inspecting the SQL the ORM generates rather than by observing query results.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from schedule_manager.config import ServerConfig
from schedule_manager.errors import Unauthenticated
from schedule_manager.identity import current_user_id
from schedule_manager.models import (
    SCOPES,
    ApiToken,
    ApiTokenScope,
    Schedule,
    User,
    UserScopedQuery,
    generate_token,
    hash_token,
)
from schedule_manager.models.user import validate_scopes
from schedule_manager.schema import INDEX_EXPRESSIONS, TABLE_EXPRESSIONS


# ── Identity context ─────────────────────────────────────────────────────────


class TestIdentityContext:
    def test_raises_without_context(self):
        with pytest.raises(Unauthenticated):
            current_user_id()

    def test_returns_bound_cli_user(self, as_user):
        with as_user(42):
            assert current_user_id() == 42

    def test_context_is_scoped(self, as_user):
        with as_user(1):
            assert current_user_id() == 1
        with pytest.raises(Unauthenticated):
            current_user_id()

    def test_schedules_table_is_scoped(self):
        assert Schedule.__query_class__ is UserScopedQuery

    def test_user_table_is_not_scoped(self):
        """Management reads cross users; see the model module docstring."""
        assert User.__query_class__ is not UserScopedQuery

    def test_token_table_is_not_scoped(self):
        assert ApiToken.__query_class__ is not UserScopedQuery
        assert ApiTokenScope.__query_class__ is not UserScopedQuery


# ── Generated SQL ────────────────────────────────────────────────────────────


class TestGeneratedSQL:
    def test_query_carries_user_filter(self, offline_backend, as_user):
        with as_user(7):
            sql = Schedule.query().to_sql()[0]
        assert '"user_id" = %s' in sql
        assert "schedules" in sql

    def test_unscoped_drops_user_filter(self, offline_backend, as_user):
        with as_user(7):
            sql = Schedule.unscoped().to_sql()[0]
        assert "user_id" not in sql

    def test_user_table_has_no_user_filter(self, offline_backend, as_user):
        with as_user(7):
            sql = User.query().to_sql()[0]
        assert "user_id" not in sql
        assert "users" in sql

    def test_soft_delete_filter_survives_scoping(self, offline_backend, as_user):
        with as_user(7):
            sql = Schedule.query().to_sql()[0]
        assert "deleted_at" in sql

    def test_every_terminal_path_shares_the_filter(self, offline_backend, as_user):
        """The filter is attached at construction, so all paths inherit it."""
        with as_user(11):
            for terminal in ("all", "one", "count", "exists", "aggregate"):
                assert callable(getattr(Schedule.query(), terminal))
            sql = Schedule.query().limit(5).offset(10).to_sql()[0]
        assert '"user_id" = %s' in sql
        assert "LIMIT" in sql
        assert "OFFSET" in sql

    def test_filter_present_on_fresh_queries(self, offline_backend, as_user):
        """Two queries built in the same context must both be filtered."""
        with as_user(3):
            first = Schedule.query().to_sql()[0]
            second = Schedule.query().to_sql()[0]
        assert '"user_id" = %s' in first
        assert '"user_id" = %s' in second

    def test_query_without_context_fails_closed(self, offline_backend):
        with pytest.raises(Unauthenticated):
            Schedule.query()


# ── Write path ───────────────────────────────────────────────────────────────


class TestOwnershipOnInsert:
    def test_user_id_is_forced(self, as_user):
        with as_user(5):
            data = Schedule(title="t").prepare_save_data(
                {"title": "t", "user_id": 999}, is_new=True
            )
        assert data["user_id"] == 5

    def test_update_also_forces_user_id(self, as_user):
        """Ownership cannot be transferred by passing a different user_id."""
        with as_user(5):
            data = Schedule(title="t").prepare_save_data(
                {"title": "t2", "user_id": 999}, is_new=False
            )
        assert data["user_id"] == 5

    def test_constructs_without_user_id(self):
        """Tools build Schedule(title=...) and let the mixin supply the owner."""
        assert Schedule(title="no owner").user_id == 0


# ── Tokens and scopes ────────────────────────────────────────────────────────


class TestTokenHelpers:
    def test_generate_returns_plaintext_and_digest(self):
        plaintext, digest = generate_token()
        assert plaintext.startswith("sm_")
        assert digest == hash_token(plaintext)
        assert plaintext not in digest

    def test_tokens_are_unique(self):
        assert len({generate_token()[0] for _ in range(50)}) == 50

    def test_hash_is_stable(self):
        assert hash_token("sm_abc") == hash_token("sm_abc")
        assert hash_token("sm_abc") != hash_token("sm_abd")

    def test_scope_vocabulary(self):
        assert SCOPES == ("schedules:read", "schedules:write")

    def test_validate_scopes_normalises(self):
        assert validate_scopes(["schedules:read", " schedules:read ", ""]) == (
            "schedules:read",
        )

    def test_validate_scopes_rejects_unknown(self):
        with pytest.raises(ValueError, match="unknown scope"):
            validate_scopes(["schedules:delete"])

    def test_empty_scope_set_is_allowed(self):
        assert validate_scopes([]) == ()


# ── DDL ──────────────────────────────────────────────────────────────────────


class TestDDL:
    def _sql(self, offline_backend) -> list[str]:
        return [
            factory(offline_backend.dialect).to_sql()[0]
            for _name, factory in TABLE_EXPRESSIONS
        ]

    def test_four_tables_in_dependency_order(self, offline_backend):
        tables = [
            t for t in self._sql(offline_backend)
        ]
        joined = " ".join(tables)
        assert '"users"' in joined
        assert '"api_tokens"' in joined
        assert '"api_token_scopes"' in joined
        assert '"schedules"' in joined
        # users must be created before anything that references it
        assert joined.index('"users"') < joined.index('"api_tokens"')
        assert joined.index('"api_tokens"') < joined.index('"schedules"')

    def test_all_statements_are_idempotent(self, offline_backend):
        for sql in self._sql(offline_backend):
            assert "IF NOT EXISTS" in sql

    def test_schedules_user_id_is_restricted(self, offline_backend):
        sql = [s for s in self._sql(offline_backend) if '"schedules"' in s][0]
        assert 'REFERENCES "users"("id") ON DELETE RESTRICT' in sql
        assert '"user_id" INTEGER NOT NULL' in sql

    def test_token_scopes_cascade_but_token_user_restricts(self, offline_backend):
        sql = " ".join(self._sql(offline_backend))
        assert 'REFERENCES "api_tokens"("id") ON DELETE CASCADE' in sql
        assert 'REFERENCES "users"("id") ON DELETE RESTRICT' in sql

    def test_scope_table_composite_primary_key(self, offline_backend):
        sql = [s for s in self._sql(offline_backend) if '"api_token_scopes"' in s][0]
        assert 'PRIMARY KEY ("api_token_id", "scope")' in sql

    def test_token_digest_is_unique(self, offline_backend):
        sql = [s for s in self._sql(offline_backend) if '"api_tokens"' in s][0]
        assert '"token_hash" CHAR(64) NOT NULL UNIQUE' in sql

    def test_no_token_column_anywhere(self, offline_backend):
        """Only the digest is persisted; there must be no plaintext column."""
        joined = " ".join(self._sql(offline_backend))
        assert '"token"' not in joined

    def test_indexes_are_standalone(self, offline_backend):
        for factory in INDEX_EXPRESSIONS:
            sql = factory(offline_backend.dialect).to_sql()[0]
            assert sql.startswith("CREATE INDEX IF NOT EXISTS")
            assert "CREATE TABLE" not in sql

    def test_schedule_index_exists(self, offline_backend):
        names = " ".join(
            factory(offline_backend.dialect).to_sql()[0] for factory in INDEX_EXPRESSIONS
        )
        assert 'ix_schedules_user' in names


# ── Model validation (no database) ────────────────────────────────────────────


class TestScheduleValidation:
    def test_empty_title_rejected(self):
        with pytest.raises(ValidationError, match="title must not be empty"):
            Schedule(title="", user_id=1)

    def test_whitespace_title_rejected(self):
        with pytest.raises(ValidationError, match="title must not be empty"):
            Schedule(title="   ", user_id=1)

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError, match="status must be one of"):
            Schedule(title="Test", status="bogus")

    def test_priority_out_of_range_rejected(self):
        with pytest.raises(ValidationError, match="priority must be between 1 and 5"):
            Schedule(title="Test", priority=0)
        with pytest.raises(ValidationError, match="priority must be between 1 and 5"):
            Schedule(title="Test", priority=6)

    def test_invalid_rrule_rejected(self):
        with pytest.raises(ValidationError, match="invalid RRULE"):
            Schedule(title="Test", rrule="NOT_AN_RRULE")

    def test_start_after_due_rejected(self):
        from datetime import datetime

        from dateutil.tz import tzutc

        with pytest.raises(ValidationError, match="start_time must be before due_time"):
            Schedule(
                title="Test",
                user_id=1,
                start_time=datetime(2026, 12, 31, tzinfo=tzutc()),
                due_time=datetime(2026, 1, 1, tzinfo=tzutc()),
            )

    def test_title_is_trimmed(self):
        assert Schedule(title="  Hello  ").title == "Hello"

    def test_defaults(self):
        s = Schedule(title="Test", user_id=1)
        assert s.status == "pending"
        assert s.priority == 3
        assert s.tags == []

    def test_boundaries_accepted(self):
        for priority in (1, 5):
            assert Schedule(title="T", priority=priority).priority == priority


class TestMaliciousInput:
    def test_sql_injection_kept_as_data(self):
        s = Schedule(title="'; DROP TABLE schedules; --")
        assert s.title == "'; DROP TABLE schedules; --"

    def test_xss_kept_as_data(self):
        s = Schedule(title="T", description="<script>alert('xss')</script>")
        assert s.description == "<script>alert('xss')</script>"

    def test_unicode_preserved(self):
        assert Schedule(title="测试日程 🎉ünchen").title == "测试日程 🎉ünchen"

    def test_rrule_injection_rejected(self):
        with pytest.raises(ValidationError):
            Schedule(title="T", rrule="FREQ=BAD;EVIL=true")


class TestUserValidation:
    def test_username_required(self):
        with pytest.raises(ValidationError):
            User()

    def test_defaults_active(self):
        assert User(username="alice").is_active is True


class TestServerConfig:
    """The allowlist derivation is easy to get subtly wrong, so pin it."""

    def test_default_is_loopback(self):
        cfg = ServerConfig()
        assert cfg.hostname == "127.0.0.1"
        assert cfg.scheme == "http"
        assert cfg.is_loopback is True
        assert cfg.mcp_path == "http://127.0.0.1:8000/mcp"

    def test_hosts_use_any_port_wildcard(self):
        """A bare '*' is not a wildcard in the SDK — it matches nothing."""
        hosts = ServerConfig().effective_allowed_hosts()
        assert "*" not in hosts
        assert all(h.endswith(":*") for h in hosts)
        assert "127.0.0.1:*" in hosts
        assert "localhost:*" in hosts

    def test_hosts_derived_from_public_url(self):
        cfg = ServerConfig(public_url="https://mcp.example.com:9443")
        assert cfg.hostname == "mcp.example.com"
        assert cfg.scheme == "https"
        assert cfg.is_loopback is False
        assert "mcp.example.com:*" in cfg.effective_allowed_hosts()
        assert cfg.effective_allowed_origins() == ["https://mcp.example.com:*"]

    def test_explicit_lists_win(self):
        cfg = ServerConfig(
            public_url="https://a.example",
            allowed_hosts=("a.example", "b.example:9000"),
            allowed_origins=("https://a.example",),
        )
        assert cfg.effective_allowed_hosts() == ["a.example", "b.example:9000"]
        assert cfg.effective_allowed_origins() == ["https://a.example"]

    def test_ipv6_loopback(self):
        cfg = ServerConfig(public_url="http://[::1]:8000")
        assert cfg.hostname == "::1"
        assert cfg.is_loopback is True
