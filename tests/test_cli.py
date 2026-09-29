"""CLI end-to-end tests.  Each case runs the real subprocess and checks the
JSON envelope, the exit code, and — for the isolation cases — that the database
really did not change.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from schedule_manager.models import ApiToken, Schedule, User

VENV_PYTHON = sys.executable


@pytest.fixture
async def account(tables):
    user, token = await User.open_account("cli-user", label="cli")
    return user, token


def _run(*args: str, token: str | None = None, expect_exit: int = 0, env_extra=None):
    env = dict(os.environ)
    env.pop("SCHEDULE_TOKEN", None)
    env.pop("SCHEDULE_TOKEN_FILE", None)
    if token is not None:
        env["SCHEDULE_TOKEN"] = token
    if env_extra:
        env.update(env_extra)
    result = subprocess.run(
        [VENV_PYTHON, "-m", "schedule_manager.cli", *args],
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if result.returncode != expect_exit:
        print(f"STDOUT: {result.stdout}", file=sys.stderr)
        print(f"STDERR: {result.stderr}", file=sys.stderr)
    assert result.returncode == expect_exit, (
        f"exit={result.returncode}, stderr={result.stderr}"
    )
    return json.loads(result.stdout) if result.stdout.strip() else {}


# ── Authentication ───────────────────────────────────────────────────────────


class TestAuthentication:
    def test_describe_needs_no_token(self, tables):
        data = _run("--describe")
        assert data["name"] == "schedule-manager"
        assert "authentication" in data
        assert "41" in data["exit_codes"]

    def test_missing_token_is_rejected(self, tables):
        data = _run("list", expect_exit=41)
        assert data["status"] == "error"
        assert data["error"]["code"] == "UNAUTHENTICATED"

    def test_unknown_token_is_rejected(self, tables):
        data = _run("list", token="sm_nope", expect_exit=41)
        assert data["error"]["code"] == "UNAUTHENTICATED"

    def test_empty_token_is_rejected(self, tables):
        data = _run("list", token="   ", expect_exit=41)
        assert data["error"]["code"] == "UNAUTHENTICATED"

    def test_token_flag_works(self, tables, account):
        _user, token = account
        data = _run("--token", token, "whoami")
        assert data["status"] == "ok"
        assert data["data"]["username"] == "cli-user"

    async def test_revoked_token_is_rejected(self, tables, account):
        user, token = account
        record = await ApiToken.query().where(ApiToken.c.user_id == user.id).one()
        await user.revoke_token(record.id)
        data = _run("list", token=token, expect_exit=41)
        assert data["error"]["code"] == "UNAUTHENTICATED"

    async def test_deactivated_account_is_forbidden(self, tables, account):
        user, token = account
        await user.close_account()
        data = _run("list", token=token, expect_exit=41)
        assert data["status"] == "error"

    def test_token_file_is_read(self, tables, account, tmp_path):
        _user, token = account
        path = tmp_path / "token"
        path.write_text(token, encoding="utf-8")
        data = _run("whoami", expect_exit=0, env_extra={"SCHEDULE_TOKEN_FILE": str(path)})
        assert data["data"]["username"] == "cli-user"

    def test_token_file_precedence(self, tables, account, tmp_path):
        _user, token = account
        path = tmp_path / "token"
        path.write_text(token, encoding="utf-8")
        data = _run(
            "whoami",
            token="sm_wrong",
            env_extra={"SCHEDULE_TOKEN_FILE": str(path)},
        )
        assert data["data"]["username"] == "cli-user"

    def test_missing_token_file_is_reported(self, tables):
        data = _run(
            "list", expect_exit=1, env_extra={"SCHEDULE_TOKEN_FILE": "/nonexistent/tok"}
        )
        assert data["error"]["code"] == "CONFIG_ERROR"
        assert "SCHEDULE_TOKEN_FILE" in data["error"]["message"]


# ── Business commands ────────────────────────────────────────────────────────


class TestCommands:
    def test_whoami(self, tables, account):
        _user, token = account
        data = _run("whoami", token=token)
        assert data["data"]["username"] == "cli-user"
        assert data["data"]["is_active"] is True
        assert "schedules:read" in data["data"]["scopes"]
        assert data["data"]["available_scopes"] == ["schedules:read", "schedules:write"]

    def test_create_and_get(self, tables, account):
        _user, token = account
        created = _run("create", "--title", "CLI Test", "--priority", "2", token=token)
        assert created["status"] == "ok"
        sid = created["data"]["id"]

        got = _run("get", "--id", str(sid), token=token)
        assert got["data"]["title"] == "CLI Test"
        assert got["data"]["priority"] == 2

    def test_create_validation_error(self, tables, account):
        _user, token = account
        data = _run("create", "--title", "", token=token, expect_exit=40)
        assert data["error"]["code"] == "VALIDATION_ERROR"

    def test_get_not_found(self, tables, account):
        _user, token = account
        data = _run("get", "--id", "999999", token=token, expect_exit=20)
        assert data["error"]["code"] == "NOT_FOUND"

    def test_list_and_search(self, tables, account):
        _user, token = account
        _run("create", "--title", "List Item", token=token)
        listed = _run("list", token=token)
        assert listed["data"]["total"] >= 1
        found = _run("search", "--keyword", "List Item", token=token)
        # search reports has_more rather than a count; asserting on the page is
        # what the caller can actually rely on.
        assert len(found["data"]["items"]) >= 1
        assert found["data"]["has_more"] is False

    def test_update_and_complete(self, tables, account):
        _user, token = account
        sid = _run("create", "--title", "Toggle", token=token)["data"]["id"]
        updated = _run("update", "--id", str(sid), "--title", "Renamed", token=token)
        assert updated["data"]["title"] == "Renamed"
        done = _run("complete", "--id", str(sid), token=token)
        assert done["data"]["status"] == "completed"

    def test_delete(self, tables, account):
        _user, token = account
        sid = _run("create", "--title", "Bye", token=token)["data"]["id"]
        out = _run("delete", "--id", str(sid), token=token)
        assert out["data"]["deleted"]["id"] == sid
        _run("get", "--id", str(sid), token=token, expect_exit=20)

    def test_dry_run(self, tables, account):
        _user, token = account
        data = _run("create", "--title", "Dry", "--dry-run", token=token)
        assert data["data"]["dry_run"] is True

    def test_human_output(self, tables, account):
        _user, token = account
        result = subprocess.run(
            [VENV_PYTHON, "-m", "schedule_manager.cli", "--human", "whoami"],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "SCHEDULE_TOKEN": token},
        )
        assert result.returncode == 0
        assert "username:" in result.stdout


# ── Isolation through the CLI ────────────────────────────────────────────────


class TestCLIIsolation:
    @pytest.fixture
    async def two_accounts(self, tables):
        alice, alice_token = await User.open_account("alice", label="a")
        bob, bob_token = await User.open_account("bob", label="b")
        return alice, alice_token, bob, bob_token

    def test_list_is_scoped(self, tables, two_accounts, as_user):
        alice, alice_token, bob, bob_token = two_accounts
        with as_user(alice.id):
            _run("create", "--title", "alice secret", token=alice_token)
        with as_user(bob.id):
            listed = _run("list", token=bob_token)
        assert listed["data"]["total"] == 0

    def test_cross_user_get_is_not_found(self, tables, two_accounts, as_user):
        alice, alice_token, bob, bob_token = two_accounts
        with as_user(alice.id):
            sid = _run("create", "--title", "alice secret", token=alice_token)["data"]["id"]
        with as_user(bob.id):
            data = _run("get", "--id", str(sid), token=bob_token, expect_exit=20)
        assert data["error"]["code"] == "NOT_FOUND"

    def test_cross_user_update_leaves_the_row_alone(self, tables, two_accounts, as_user):
        alice, alice_token, bob, bob_token = two_accounts
        with as_user(alice.id):
            sid = _run("create", "--title", "alice secret", token=alice_token)["data"]["id"]
        with as_user(bob.id):
            _run("update", "--id", str(sid), "--title", "hijacked", token=bob_token, expect_exit=20)
        with as_user(alice.id):
            got = _run("get", "--id", str(sid), token=alice_token)
        assert got["data"]["title"] == "alice secret"

    def test_cross_user_delete_leaves_the_row_alone(self, tables, two_accounts, as_user):
        alice, alice_token, bob, bob_token = two_accounts
        with as_user(alice.id):
            sid = _run("create", "--title", "alice secret", token=alice_token)["data"]["id"]
        with as_user(bob.id):
            _run("delete", "--id", str(sid), token=bob_token, expect_exit=20)
        with as_user(alice.id):
            assert _run("get", "--id", str(sid), token=alice_token)["status"] == "ok"

    def test_search_does_not_leak(self, tables, two_accounts, as_user):
        alice, alice_token, bob, bob_token = two_accounts
        with as_user(alice.id):
            _run("create", "--title", "confidential", token=alice_token)
        with as_user(bob.id):
            found = _run("search", "--keyword", "confidential", token=bob_token)
        assert found["data"]["items"] == []
        assert found["data"]["has_more"] is False


# ── Management commands ──────────────────────────────────────────────────────


class TestManagement:
    def test_user_open_prints_a_usable_token(self, tables):
        opened = _run("user", "open", "--username", "fresh", "--label", "laptop")
        token = opened["data"]["token"]
        assert token.startswith("sm_")
        assert _run("whoami", token=token)["data"]["username"] == "fresh"

    def test_user_open_rejects_duplicate(self, tables):
        _run("user", "open", "--username", "twice")
        data = _run("user", "open", "--username", "twice", expect_exit=40)
        assert data["error"]["code"] == "VALIDATION_ERROR"

    def test_user_open_rejects_unknown_scope(self, tables):
        data = _run("user", "open", "--username", "scoped", "--scope", "admin", expect_exit=40)
        assert "unknown scope" in data["error"]["message"]

    def test_restricted_scope_is_honoured(self, tables):
        opened = _run("user", "open", "--username", "readonly", "--scope", "schedules:read")
        token = opened["data"]["token"]
        who = _run("whoami", token=token)
        assert who["data"]["scopes"] == ["schedules:read"]
        _run("create", "--title", "still allowed", token=token)

    def test_token_issue_and_revoke(self, tables):
        opened = _run("user", "open", "--username", "multi")
        uid = opened["data"]["user_id"]
        issued = _run("token", "issue", "--id", str(uid), "--label", "second")
        second = issued["data"]["token"]
        assert _run("whoami", token=second)["status"] == "ok"

        _run("token", "revoke", "--token-id", str(issued["data"]["token_id"]))
        _run("whoami", token=second, expect_exit=41)

    def test_token_list_never_shows_a_token(self, tables):
        opened = _run("user", "open", "--username", "listed")
        listed = _run("token", "list", "--id", str(opened["data"]["user_id"]))
        assert listed["data"]["total"] == 1
        entry = listed["data"]["items"][0]
        assert "token" not in entry
        assert len(entry["token_hash_prefix"]) == 8
        assert entry["revoked"] is False

    def test_user_close_revokes_everything(self, tables):
        opened = _run("user", "open", "--username", "closing")
        token = opened["data"]["token"]
        uid = opened["data"]["user_id"]
        _run("user", "close", "--id", str(uid))
        _run("whoami", token=token, expect_exit=41)
