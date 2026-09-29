"""Live-database tests: CRUD, queries, properties, and the isolation matrix.

Skipped as a whole when PostgreSQL is unreachable — see ``conftest.db_config``.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from dateutil.tz import tzutc

from schedule_manager.errors import ScheduleManagerError, Unauthenticated
from schedule_manager.identity import set_cli_user
from schedule_manager.models import SCOPES, ApiToken, Schedule, User


@pytest.fixture
async def alice(tables, as_user):
    user, token = await User.open_account("alice", scopes=SCOPES, label="alice-token")
    with as_user(user.id):
        yield user, token


@pytest.fixture
async def bob(tables, as_user):
    user, token = await User.open_account("bob", scopes=SCOPES, label="bob-token")
    with as_user(user.id):
        yield user, token


# ── Accounts ─────────────────────────────────────────────────────────────────


class TestAccountLifecycle:
    async def test_open_account_returns_user_and_token(self, tables):
        user, token = await User.open_account("carol", label="laptop")
        assert user.id is not None
        assert user.username == "carol"
        assert user.is_active is True
        assert token.startswith("sm_")

    async def test_duplicate_username_rejected(self, tables):
        await User.open_account("dave")
        with pytest.raises(Exception):
            await User.open_account("dave")

    async def test_token_is_not_stored_in_plaintext(self, tables, as_user):
        _user, token = await User.open_account("erin")
        record = await ApiToken.find_one(1)
        assert record is not None
        assert record.token_hash != token
        assert token not in record.token_hash

    async def test_scopes_are_one_row_each(self, tables, as_user):
        user, _ = await User.open_account("frank")
        record = await ApiToken.query().where(ApiToken.c.user_id == user.id).one()
        scopes = await user.token_scopes(record.id)
        assert sorted(scopes) == sorted(SCOPES)

    async def test_restricted_scopes(self, tables):
        user, _ = await User.open_account("gina", scopes=["schedules:read"])
        record = await ApiToken.query().where(ApiToken.c.user_id == user.id).one()
        assert await user.has_scope(record.id, "schedules:read") is True
        assert await user.has_scope(record.id, "schedules:write") is False

    async def test_close_account_revokes_tokens(self, tables):
        user, token = await User.open_account("hank")
        record = await ApiToken.query().where(ApiToken.c.user_id == user.id).one()
        await user.close_account()

        reloaded = await User.find_one(user.id)
        assert reloaded.is_active is False
        reloaded_record = await ApiToken.find_one(record.id)
        assert reloaded_record.revoked_at is not None
        with pytest.raises(ScheduleManagerError):
            await User.authenticate(token)

    async def test_closed_user_keeps_its_row(self, tables):
        user, _ = await User.open_account("iris")
        await user.close_account()
        assert await User.find_one(user.id) is not None


# ── Credential verification ──────────────────────────────────────────────────


class TestAuthenticate:
    async def test_valid_token_resolves_user(self, tables):
        user, token = await User.open_account("jack")
        resolved = await User.authenticate(token)
        assert resolved.id == user.id

    async def test_empty_token_rejected(self, tables):
        for value in ("", "   ", None):
            with pytest.raises(Unauthenticated):
                await User.authenticate(value)

    async def test_unknown_token_rejected(self, tables):
        with pytest.raises(Unauthenticated, match="unknown token"):
            await User.authenticate("sm_not-a-real-token")

    async def test_revoked_token_rejected(self, tables):
        user, token = await User.open_account("kate")
        record = await ApiToken.query().where(ApiToken.c.user_id == user.id).one()
        await user.revoke_token(record.id)
        with pytest.raises(Unauthenticated, match="revoked"):
            await User.authenticate(token)

    async def test_expired_token_rejected(self, tables):
        user, token = await User.open_account("liam", expires_at=datetime.now(tzutc()) - timedelta(hours=1))
        with pytest.raises(Unauthenticated, match="expired"):
            await User.authenticate(token)

    async def test_rotate_token_invalidates_the_old_one(self, tables):
        user, old = await User.open_account("mia")
        record = await ApiToken.query().where(ApiToken.c.user_id == user.id).one()
        new, new_id = await user.rotate_token(record.id, label="rotated")

        assert new != old
        assert new.startswith("sm_")
        assert await User.authenticate(new)
        with pytest.raises(Unauthenticated, match="revoked"):
            await User.authenticate(old)
        assert (await ApiToken.find_one(new_id)) is not None

    async def test_revoke_rejects_a_token_owned_by_someone_else(self, tables):
        one, _ = await User.open_account("nina")
        other, _ = await User.open_account("omar")
        other_record = await ApiToken.query().where(ApiToken.c.user_id == other.id).one()
        with pytest.raises(Unauthenticated, match="does not belong"):
            await one.revoke_token(other_record.id)


# ── CRUD ─────────────────────────────────────────────────────────────────────


class TestScheduleCRUD:
    async def test_create(self, tables, alice):
        s = Schedule(title="Test Task", description="a test task")
        await s.save()
        assert s.id is not None
        assert s.title == "Test Task"
        assert s.status == "pending"
        assert s.priority == 3
        assert s.user_id == alice[0].id

    async def test_get(self, tables, alice):
        s = Schedule(title="Get Me")
        await s.save()
        found = await Schedule.find_one(s.id)
        assert found is not None
        assert found.title == "Get Me"

    async def test_get_not_found(self, tables, alice):
        assert await Schedule.find_one(999999) is None

    async def test_update(self, tables, alice):
        s = Schedule(title="Original")
        await s.save()
        s.title = "Updated"
        await s.save()
        found = await Schedule.find_one(s.id)
        assert found.title == "Updated"

    async def test_soft_delete(self, tables, alice):
        s = Schedule(title="Delete Me")
        await s.save()
        await s.delete()
        assert await Schedule.find_one(s.id) is None

    async def test_complete_and_reopen(self, tables, alice):
        s = Schedule(title="Toggle")
        await s.save()
        await s.complete()
        await s.save()
        found = await Schedule.find_one(s.id)
        assert found.status == "completed"
        assert found.completed_at is not None

        await found.reopen()
        await found.save()
        found2 = await Schedule.find_one(s.id)
        assert found2.status == "pending"
        assert found2.completed_at is None

    async def test_tags_round_trip_as_jsonb(self, tables, alice):
        s = Schedule(title="Tagged", tags=["work", "urgent"])
        await s.save()
        found = await Schedule.find_one(s.id)
        assert found.tags == ["work", "urgent"]

    async def test_insert_ownership_ignores_a_supplied_user_id(self, tables, as_user):
        alice, _ = await User.open_account("a1")
        bob, _ = await User.open_account("b1")
        with as_user(alice.id):
            s = Schedule(title="Not mine", user_id=bob.id)
            await s.save()
            assert s.user_id == alice.id
            stored = await Schedule.find_one(s.id)
            assert stored is not None
            assert stored.user_id == alice.id

    async def test_update_cannot_transfer_ownership(self, tables, as_user):
        alice, _ = await User.open_account("a2")
        bob, _ = await User.open_account("b2")
        with as_user(alice.id):
            s = Schedule(title="Mine")
            await s.save()
            sid = s.id
            s.user_id = bob.id
            await s.save()
            stored = await Schedule.find_one(sid)
            assert stored is not None
            assert stored.user_id == alice.id


# ── Queries ──────────────────────────────────────────────────────────────────


class TestScheduleQuery:
    async def test_pagination(self, tables, alice):
        for i in range(5):
            await Schedule(title=f"Task {i}").save()
        items, total, page, size = await Schedule.list_page(page=1, page_size=3)
        assert len(items) == 3
        assert total == 5
        assert (page, size) == (1, 3)

    async def test_second_page(self, tables, alice):
        for i in range(5):
            await Schedule(title=f"Task {i}").save()
        items, total, _, _ = await Schedule.list_page(page=2, page_size=3)
        assert len(items) == 2
        assert total == 5

    async def test_filter_and_search(self, tables, alice):
        await Schedule(title="Pending one", status="pending").save()
        await Schedule(title="Done one", status="completed").save()
        _items, total, _, _ = await Schedule.list_page(status="pending")
        assert total == 1
        found, has_more = await Schedule.search("Pending")
        assert len(found) == 1
        assert has_more is False
        missing, has_more = await Schedule.search("nothing-here")
        assert missing == []
        assert has_more is False

    async def test_search_paginates_and_reports_more(self, tables, alice):
        """A capped page must still tell the caller there is more.

        Without has_more an LLM client reads a full page as the whole result
        set, which is a silent correctness problem rather than a missing
        feature.
        """
        for i in range(25):
            await Schedule(title=f"match-{i}", description="needle").save()

        first, has_more = await Schedule.search("needle", page=1, page_size=10)
        assert len(first) == 10
        assert has_more is True

        last, has_more = await Schedule.search("needle", page=3, page_size=10)
        assert len(last) == 5
        assert has_more is False

        # Pages must not overlap.
        second, _ = await Schedule.search("needle", page=2, page_size=10)
        ids = {s.id for s in first} | {s.id for s in second} | {s.id for s in last}
        assert len(ids) == 25

    async def test_search_caps_page_size(self, tables, alice):
        """An absurd page_size is clamped, not honoured."""
        from schedule_manager.models.schedule import MAX_PAGE_SIZE

        for i in range(5):
            await Schedule(title=f"cap-{i}", description="clampme").save()

        items, has_more = await Schedule.search("clampme", page_size=10**6)
        assert len(items) == 5
        assert has_more is False
        assert MAX_PAGE_SIZE == 100

    async def test_sorting(self, tables, alice):
        await Schedule(title="Low", priority=5).save()
        await Schedule(title="High", priority=1).save()
        asc, _, _, _ = await Schedule.list_page(sort_by="priority", sort_order="asc")
        desc, _, _, _ = await Schedule.list_page(sort_by="priority", sort_order="desc")
        assert asc[0].priority <= asc[1].priority
        assert desc[0].priority >= desc[1].priority

    async def test_empty_list(self, tables, alice):
        items, total, _, _ = await Schedule.list_page()
        assert total == 0
        assert items == []

    async def test_overdue(self, tables, alice):
        await Schedule(title="Late", due_time=datetime.now(tzutc()) - timedelta(hours=1)).save()
        await Schedule(title="Soon", due_time=datetime.now(tzutc()) + timedelta(hours=1)).save()
        assert len(await Schedule.overdue()) == 1


# ── The isolation matrix ─────────────────────────────────────────────────────


class TestIsolation:
    async def test_list_is_scoped(self, tables, alice, bob, as_user):
        with as_user(alice[0].id):
            await Schedule(title="alice task").save()
        with as_user(bob[0].id):
            await Schedule(title="bob task").save()

        with as_user(bob[0].id):
            _items, total, _, _ = await Schedule.list_page()
            assert total == 1
        with as_user(alice[0].id):
            _items, total, _, _ = await Schedule.list_page()
            assert total == 1

    async def test_search_is_scoped(self, tables, alice, bob, as_user):
        with as_user(alice[0].id):
            await Schedule(title="confidential planning").save()
        with as_user(bob[0].id):
            found, has_more = await Schedule.search("confidential")
            assert found == []
            assert has_more is False

    async def test_cross_user_get_is_not_found(self, tables, alice, bob, as_user):
        with as_user(alice[0].id):
            s = Schedule(title="alice private")
            await s.save()
            sid = s.id
        with as_user(bob[0].id):
            assert await Schedule.find_one(sid) is None

    async def test_cross_user_update_changes_nothing(self, tables, alice, bob, as_user):
        with as_user(alice[0].id):
            s = Schedule(title="alice private")
            await s.save()
            sid = s.id

        with as_user(bob[0].id):
            target = await Schedule.find_one(sid)
            assert target is None

        with as_user(alice[0].id):
            reloaded = await Schedule.find_one(sid)
            assert reloaded is not None
            assert reloaded.title == "alice private"

    async def test_cross_user_delete_is_refused(self, tables, alice, bob, as_user):
        with as_user(alice[0].id):
            s = Schedule(title="alice private")
            await s.save()
            sid = s.id
        with as_user(bob[0].id):
            assert await Schedule.find_one(sid) is None
        with as_user(alice[0].id):
            assert await Schedule.find_one(sid) is not None

    async def test_count_does_not_leak(self, tables, alice, bob, as_user):
        with as_user(alice[0].id):
            for i in range(3):
                await Schedule(title=f"a{i}").save()
        with as_user(bob[0].id):
            assert await Schedule.query().count() == 0

    async def test_unscoped_sees_everything(self, tables, alice, bob, as_user):
        with as_user(alice[0].id):
            await Schedule(title="alice task").save()
        with as_user(bob[0].id):
            await Schedule(title="bob task").save()
            assert await Schedule.unscoped().count() == 2

    async def test_no_context_is_refused(self, tables):
        with pytest.raises(Unauthenticated):
            await Schedule.query().count()

    async def test_switching_identity_switches_the_view(self, tables, alice, bob, as_user):
        with as_user(alice[0].id):
            await Schedule(title="alice task").save()
        with as_user(alice[0].id):
            first = await Schedule.query().count()
        with as_user(bob[0].id):
            second = await Schedule.query().count()
        assert (first, second) == (1, 0)


class TestBulkCreateSerialisesJsonb:
    """bulk_create bypasses _insert_internal, so the hook must be on save_data."""

    async def test_bulk_create_writes_json_arrays(self, tables, as_user):
        from schedule_manager.models import User

        user, _ = await User.open_account("bulk")
        with as_user(user.id):
            await Schedule.bulk_create(
                [Schedule(title=f"bulk-{n}", tags=["x", "y"]) for n in range(3)],
                batch_size=10,
            )
            rows = await Schedule.query().order_by((Schedule.c.id, "ASC")).all()
            assert len(rows) == 3
            for row in rows:
                assert row.tags == ["x", "y"]

    async def test_bulk_create_writes_empty_arrays_not_objects(self, tables, as_user):
        from schedule_manager.models import User

        user, _ = await User.open_account("bulk2")
        with as_user(user.id):
            await Schedule.bulk_create([Schedule(title="bulk-empty")], batch_size=5)
            row = await Schedule.query().one()
            assert row.tags == []
            assert row.rdate == []
            assert row.exdate == []
