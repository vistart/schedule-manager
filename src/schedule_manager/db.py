"""Backend wiring: one connection pool, one transaction scope per task.

Two problems this solves.

**``Model.configure()`` is per-class.** Each call constructs a new backend
instance and opens its own connection, so configuring four models yields four
connections and a transaction opened through one model does not cover
statements issued through another — they sit on different sessions and cannot
see each other's uncommitted work.

**A single shared connection is not enough either.** A public server handles
concurrent requests; one connection would interleave them and their transactions
would collide.

The answer is ``AsyncBackendPool``. ``pool.connection()`` acquires a backend for
the current task and publishes it on a ContextVar, and ``Model.backend()``
resolves that ContextVar *before* falling back to ``__backend__``
(``base/base.py:806-822``). So inside the context every model — ``Schedule``,
``User``, ``ApiToken`` — resolves to the same connection, different tasks get
different connections, and the whole scope is released on exit.

``Model.__backend__`` is still populated as the fallback for code that runs
outside any pool context (``setup_db``, mostly).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from rhosocial.activerecord.backend.impl.postgres.backend import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig
from rhosocial.activerecord.connection.pool import AsyncBackendPool, PoolConfig
from rhosocial.activerecord.connection.pool import (
    get_current_async_pool as _current_pool,
)

from .config import get_db_config, get_pool_config
from .models import ApiToken, ApiTokenScope, Schedule, User

MODELS = (Schedule, User, ApiToken, ApiTokenScope)


async def create_pool(
    config: Optional[PostgresConnectionConfig] = None,
) -> AsyncBackendPool:
    """Configure every model and return a warmed-up connection pool.

    Configuration happens here rather than at acquire time because
    ``configure()`` is what triggers dialect introspection, and the models need
    their fallback backend regardless of whether a pool is in play.
    """
    connection_config = config or get_db_config()
    await Schedule.configure(connection_config, AsyncPostgresBackend)
    fallback = Schedule.backend()
    for model in MODELS[1:]:
        model.__backend__ = fallback

    sizing = get_pool_config()
    return await AsyncBackendPool.create(
        PoolConfig(
            min_size=sizing.min_size,
            max_size=sizing.max_size,
            backend_factory=lambda: AsyncPostgresBackend(
                connection_config=connection_config
            ),
        )
    )


def current_pool() -> Optional[AsyncBackendPool]:
    """Return the pool active in this task, if any."""
    return _current_pool()


@asynccontextmanager
async def connection(pool: AsyncBackendPool) -> AsyncIterator[AsyncPostgresBackend]:
    """Bind one pooled connection to this task for the duration of the block.

    Reentrant: a nested ``connection()`` reuses the outer one rather than
    acquiring a second.
    """
    async with pool.connection() as backend:
        yield backend


async def close_pool(pool: Optional[AsyncBackendPool]) -> None:
    """Close the pool, if there is one."""
    if pool is not None and not pool.is_closed:
        await pool.close()
