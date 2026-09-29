"""Shared fixtures.

Two database modes, deliberately separate:

* **offline** — an ``AsyncPostgresBackend`` constructed but never connected, with
  the dialect version pinned.  Enough to render SQL and exercise validation, so
  the isolation contract can be asserted without a database.
* **live** — a real PostgreSQL connection.  Skipped, not failed, when the host is
  unreachable, so the offline suite stays runnable anywhere.
"""

from __future__ import annotations

import socket
from typing import AsyncIterator, Iterator

import pytest
from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

from schedule_manager.config import get_db_config
from schedule_manager.models import ApiToken, ApiTokenScope, Schedule, User

import os

os.environ.setdefault("SCHEDULE_POOL_MIN", "0")

FAKE_CONNECTION = PostgresConnectionConfig(
    host="127.0.0.1", port=1, database="offline", username="offline", password="offline"
)

MODELS = (Schedule, User, ApiToken, ApiTokenScope)


@pytest.fixture
def offline_backend() -> Iterator[AsyncPostgresBackend]:
    """A dialect-only backend, attached for the duration of one test."""
    backend = AsyncPostgresBackend(connection_config=FAKE_CONNECTION)
    backend.dialect._version = (16, 0, 0)
    saved = {model: model.__backend__ for model in MODELS}
    for model in MODELS:
        model.__backend__ = backend
    try:
        yield backend
    finally:
        for model, previous in saved.items():
            model.__backend__ = previous


def _reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


@pytest.fixture(scope="session")
def db_config() -> PostgresConnectionConfig:
    config = get_db_config()
    if not _reachable(config.host, config.port):
        pytest.skip(f"PostgreSQL at {config.host}:{config.port} is unreachable")
    return config


@pytest.fixture
async def tables(db_config) -> AsyncIterator[AsyncPostgresBackend]:
    """Create every table for one test, then drop them, on a pooled connection.

    Drops before creating, not just after: a table left over from an earlier
    schema would survive ``CREATE TABLE IF NOT EXISTS`` and then break the
    ``user_id`` index.
    """
    from schedule_manager.db import close_pool, connection, create_pool
    from schedule_manager.schema import create_all, drop_all

    pool = await create_pool(db_config)
    try:
        async with connection(pool) as backend:
            await drop_all(backend)
            await create_all(backend)
            try:
                yield backend
            finally:
                await drop_all(backend)
    finally:
        await close_pool(pool)


@pytest.fixture
def as_user():
    """Bind an identity for one test: ``with as_user(3): ...``."""
    from contextlib import contextmanager

    from schedule_manager.identity import reset_cli_user, set_cli_user

    @contextmanager
    def _bind(user_id: int):
        token = set_cli_user(user_id, "sm_test")
        try:
            yield user_id
        finally:
            reset_cli_user(token)

    return _bind
