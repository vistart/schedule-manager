"""Tests for schedule manager — async CRUD, queries, validation, and CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from dateutil.tz import tzutc
from pydantic import ValidationError

from rhosocial.activerecord.backend.impl.postgres import AsyncPostgresBackend

from schedule_manager.config import get_db_config
from schedule_manager.model import Schedule

VENV_PYTHON = sys.executable


@pytest.fixture(autouse=True)
async def setup_db():
    config = get_db_config()
    await Schedule.configure(config, AsyncPostgresBackend)
    expr = Schedule.generate_create_table(if_not_exists=True)
    sql, params = expr.to_sql()
    await Schedule.backend().execute(sql, params)
    yield
    expr = Schedule.generate_drop_table(if_exists=True)
    sql, params = expr.to_sql()
    await Schedule.backend().execute(sql, params)


# ── CRUD Tests ──────────────────────────────────────────────────────────────


class TestScheduleCRUD:
    async def test_create_schedule(self):
        s = Schedule(title="Test Task", description="A test task")
        await s.save()
        assert s.id is not None
        assert s.title == "Test Task"
        assert s.status == "pending"
        assert s.priority == 3

    async def test_get_schedule(self):
        s = Schedule(title="Get Me")
        await s.save()
        found = await Schedule.find_one(s.id)
        assert found is not None
        assert found.title == "Get Me"

    async def test_get_not_found(self):
        found = await Schedule.find_one(999999)
        assert found is None

    async def test_update_schedule(self):
        s = Schedule(title="Original")
        await s.save()
        s.title = "Updated"
        await s.save()
        found = await Schedule.find_one(s.id)
        assert found.title == "Updated"

    async def test_delete_schedule(self):
        s = Schedule(title="Delete Me")
        await s.save()
        sid = s.id
        await s.delete()
        assert s.deleted_at is not None

    async def test_complete_schedule(self):
        s = Schedule(title="Complete Me")
        await s.save()
        await s.complete()
        await s.save()
        found = await Schedule.find_one(s.id)
        assert found.status == "completed"
        assert found.completed_at is not None

    async def test_reopen_schedule(self):
        s = Schedule(title="Reopen Me")
        await s.save()
        await s.complete()
        await s.save()
        await s.reopen()
        await s.save()
        found = await Schedule.find_one(s.id)
        assert found.status == "pending"
        assert found.completed_at is None

    async def test_find_all(self):
        for i in range(3):
            await Schedule(title=f"Item {i}").save()
        all_items = await Schedule.find_all()
        assert len(all_items) >= 3


# ── Query / Pagination Tests ────────────────────────────────────────────────


class TestScheduleQuery:
    async def test_list_page(self):
        for i in range(5):
            await Schedule(title=f"Task {i}").save()
        items, total, page, page_size = await Schedule.list_page(page=1, page_size=3)
        assert len(items) == 3
        assert total == 5
        assert page == 1
        assert page_size == 3

    async def test_list_page_second(self):
        for i in range(5):
            await Schedule(title=f"Task {i}").save()
        items, total, page, page_size = await Schedule.list_page(page=2, page_size=3)
        assert len(items) == 2
        assert total == 5

    async def test_list_filter_status(self):
        await Schedule(title="Pending", status="pending").save()
        await Schedule(title="Done", status="completed").save()
        items, total, _, _ = await Schedule.list_page(status="pending")
        assert total == 1
        assert items[0].title == "Pending"

    async def test_list_filter_priority(self):
        await Schedule(title="High", priority=1).save()
        await Schedule(title="Low", priority=5).save()
        items, total, _, _ = await Schedule.list_page(priority=1)
        assert total == 1
        assert items[0].title == "High"

    async def test_list_keyword_search(self):
        await Schedule(title="Meeting with Bob").save()
        await Schedule(title="Lunch").save()
        items, total, _, _ = await Schedule.list_page(keyword="meeting")
        assert total == 1
        assert "Bob" in items[0].title

    async def test_search(self):
        await Schedule(title="Project deadline").save()
        await Schedule(title="Team standup").save()
        items = await Schedule.search("deadline")
        assert len(items) == 1
        assert "deadline" in items[0].title.lower()

    async def test_sort_by_priority_asc(self):
        await Schedule(title="Low", priority=5).save()
        await Schedule(title="High", priority=1).save()
        items, _, _, _ = await Schedule.list_page(sort_by="priority", sort_order="asc")
        assert items[0].priority <= items[1].priority

    async def test_sort_by_priority_desc(self):
        await Schedule(title="Low", priority=5).save()
        await Schedule(title="High", priority=1).save()
        items, _, _, _ = await Schedule.list_page(sort_by="priority", sort_order="desc")
        assert items[0].priority >= items[1].priority

    async def test_empty_list(self):
        items, total, _, _ = await Schedule.list_page()
        assert total == 0
        assert items == []


# ── Properties Tests ────────────────────────────────────────────────────────


class TestScheduleProperties:
    async def test_is_overdue(self):
        s = Schedule(
            title="Overdue",
            due_time=datetime.now(tzutc()) - timedelta(hours=1),
        )
        await s.save()
        assert s.is_overdue is True

    async def test_not_overdue_without_due_time(self):
        s = Schedule(title="No due")
        await s.save()
        assert s.is_overdue is False

    async def test_not_overdue_when_completed(self):
        s = Schedule(
            title="Done",
            due_time=datetime.now(tzutc()) - timedelta(hours=1),
        )
        await s.save()
        await s.complete()
        await s.save()
        assert s.is_overdue is False

    async def test_not_overdue_when_cancelled(self):
        s = Schedule(
            title="Cancelled",
            due_time=datetime.now(tzutc()) - timedelta(hours=1),
            status="cancelled",
        )
        await s.save()
        assert s.is_overdue is False

    async def test_tags_jsonb(self):
        s = Schedule(title="Tagged", tags=["work", "urgent"])
        await s.save()
        found = await Schedule.find_one(s.id)
        assert found.tags == ["work", "urgent"]

    async def test_tags_empty(self):
        s = Schedule(title="No tags", tags=[])
        await s.save()
        found = await Schedule.find_one(s.id)
        assert found.tags == []

    async def test_next_occurrence_daily(self):
        s = Schedule(
            title="Daily standup",
            rrule="FREQ=DAILY",
            start_time=datetime(2026, 1, 1, 9, 0),
        )
        await s.save()
        found = await Schedule.find_one(s.id)
        assert found.rrule == "FREQ=DAILY"
        nxt = found.next_occurrence(from_dt=datetime(2026, 1, 2, 0, 0, tzinfo=tzutc()))
        assert nxt is not None
        assert nxt.day == 2

    async def test_next_occurrence_none_without_rrule(self):
        s = Schedule(title="No recurrence")
        await s.save()
        found = await Schedule.find_one(s.id)
        assert found.next_occurrence() is None


# ── Validation Tests ────────────────────────────────────────────────────────


class TestValidation:
    def test_empty_title_rejected(self):
        with pytest.raises(ValidationError, match="title must not be empty"):
            Schedule(title="")

    def test_whitespace_title_rejected(self):
        with pytest.raises(ValidationError, match="title must not be empty"):
            Schedule(title="   ")

    def test_invalid_status_rejected(self):
        with pytest.raises(ValidationError, match="status must be one of"):
            Schedule(title="Test", status="bogus")

    def test_priority_too_low_rejected(self):
        with pytest.raises(ValidationError, match="priority must be between 1 and 5"):
            Schedule(title="Test", priority=0)

    def test_priority_too_high_rejected(self):
        with pytest.raises(ValidationError, match="priority must be between 1 and 5"):
            Schedule(title="Test", priority=6)

    def test_invalid_rrule_rejected(self):
        with pytest.raises(ValidationError, match="invalid RRULE"):
            Schedule(title="Test", rrule="NOT_AN_RRULE")

    def test_valid_rrule_accepted(self):
        s = Schedule(title="Test", rrule="FREQ=WEEKLY;BYDAY=MO")
        assert s.rrule == "FREQ=WEEKLY;BYDAY=MO"

    def test_start_after_due_rejected(self):
        with pytest.raises(ValidationError, match="start_time must be before due_time"):
            Schedule(
                title="Test",
                start_time=datetime(2026, 12, 31, tzinfo=tzutc()),
                due_time=datetime(2026, 1, 1, tzinfo=tzutc()),
            )

    def test_valid_priority_boundary_1(self):
        s = Schedule(title="Test", priority=1)
        assert s.priority == 1

    def test_valid_priority_boundary_5(self):
        s = Schedule(title="Test", priority=5)
        assert s.priority == 5

    def test_valid_statuses(self):
        for status in ["pending", "in_progress", "completed", "cancelled"]:
            s = Schedule(title="Test", status=status)
            assert s.status == status

    def test_title_trimmed(self):
        s = Schedule(title="  Hello  ")
        assert s.title == "Hello"

    def test_valid_model_with_rrule(self):
        s = Schedule(
            title="Recurring",
            rrule="FREQ=MONTHLY;BYMONTHDAY=1",
            start_time=datetime(2026, 1, 1, tzinfo=tzutc()),
            due_time=datetime(2026, 1, 1, 23, 59, tzinfo=tzutc()),
        )
        assert s.title == "Recurring"


# ── Malicious / Edge-Case Input Tests ───────────────────────────────────────


class TestMaliciousInput:
    def test_sql_injection_in_title(self):
        s = Schedule(title="'; DROP TABLE schedules; --")
        assert s.title == "'; DROP TABLE schedules; --"

    def test_xss_in_description(self):
        s = Schedule(title="Test", description="<script>alert('xss')</script>")
        assert s.description == "<script>alert('xss')</script>"

    def test_very_long_title(self):
        long_title = "A" * 10000
        s = Schedule(title=long_title)
        assert s.title == long_title

    def test_unicode_title(self):
        s = Schedule(title="测试日程 🎉ünchen")
        assert s.title == "测试日程 🎉ünchen"

    def test_null_bytes_in_title(self):
        s = Schedule(title="hello\x00world")
        assert "\x00" in s.title

    def test_newlines_in_title(self):
        s = Schedule(title="line1\nline2\ttab")
        assert "\n" in s.title

    def test_negative_priority(self):
        with pytest.raises(ValidationError):
            Schedule(title="Test", priority=-1)

    def test_zero_priority(self):
        with pytest.raises(ValidationError):
            Schedule(title="Test", priority=0)

    def test_priority_six(self):
        with pytest.raises(ValidationError):
            Schedule(title="Test", priority=6)

    def test_empty_status(self):
        with pytest.raises(ValidationError):
            Schedule(title="Test", status="")

    def test_rrule_injection(self):
        with pytest.raises(ValidationError):
            Schedule(title="Test", rrule="FREQ=BAD;EVIL=true")


# ── CLI Tests ────────────────────────────────────────────────────────────────


class TestCLI:
    def _run_cli(self, *args: str, expect_exit: int = 0) -> dict:
        cmd = [VENV_PYTHON, "-m", "schedule_manager.cli"] + list(args)
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode != expect_exit:
            print(f"STDOUT: {result.stdout}", file=sys.stderr)
            print(f"STDERR: {result.stderr}", file=sys.stderr)
        assert result.returncode == expect_exit, f"exit={result.returncode}, stderr={result.stderr}"
        if result.stdout.strip():
            return json.loads(result.stdout)
        return {}

    def test_describe(self):
        data = self._run_cli("--describe")
        assert data["name"] == "schedule-manager"
        assert "commands" in data
        assert "create" in data["commands"]

    def test_create_and_get(self):
        created = self._run_cli("create", "--title", "CLI Test", "--priority", "2")
        assert created["status"] == "ok"
        sid = created["data"]["id"]

        got = self._run_cli("get", "--id", str(sid))
        assert got["status"] == "ok"
        assert got["data"]["title"] == "CLI Test"
        assert got["data"]["priority"] == 2

    def test_create_validation_error(self):
        result = self._run_cli("create", "--title", "", expect_exit=40)
        assert result["status"] == "error"
        assert result["error"]["code"] == "VALIDATION_ERROR"

    def test_get_not_found(self):
        result = self._run_cli("get", "--id", "999999", expect_exit=20)
        assert result["status"] == "error"
        assert result["error"]["code"] == "NOT_FOUND"

    def test_list(self):
        self._run_cli("create", "--title", "List Item")
        data = self._run_cli("list")
        assert data["status"] == "ok"
        assert data["data"]["total"] >= 1

    def test_search(self):
        self._run_cli("create", "--title", "Unique Keyword XYZ")
        data = self._run_cli("search", "--keyword", "XYZ")
        assert data["status"] == "ok"
        assert any("XYZ" in item["title"] for item in data["data"]["items"])

    def test_human_output(self):
        result = subprocess.run(
            [VENV_PYTHON, "-m", "schedule_manager.cli", "--human", "list"],
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0
        assert "items:" in result.stdout or "total:" in result.stdout

    def test_dry_run(self):
        data = self._run_cli("create", "--title", "Dry Run", "--dry-run")
        assert data["status"] == "ok"
        assert data["data"]["dry_run"] is True
        assert "would_create" in data["data"]
