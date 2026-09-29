"""Runtime configuration, loaded from environment variables with .env fallback.

Three groups:

* database connection — ``SCHEDULE_DB_*``
* bearer token for local callers — ``SCHEDULE_TOKEN`` / ``SCHEDULE_TOKEN_FILE``
* remote server — ``SCHEDULE_BIND_*``, ``SCHEDULE_PUBLIC_URL``
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from dotenv import load_dotenv
from rhosocial.activerecord.backend.errors import DatabaseError
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

_ENV_LOADED = False

DB_ENV_VARS = (
    "SCHEDULE_DB_HOST",
    "SCHEDULE_DB_PORT",
    "SCHEDULE_DB_NAME",
    "SCHEDULE_DB_USER",
    "SCHEDULE_DB_PASSWORD",
)

TOKEN_ENV_VARS = ("SCHEDULE_TOKEN", "SCHEDULE_TOKEN_FILE")

SERVER_ENV_VARS = (
    "SCHEDULE_BIND_HOST",
    "SCHEDULE_BIND_PORT",
    "SCHEDULE_PUBLIC_URL",
    "SCHEDULE_ALLOWED_HOSTS",
    "SCHEDULE_ALLOWED_ORIGINS",
)


def _ensure_env_loaded() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    _ENV_LOADED = True


class _QuietOrm(logging.Filter):
    """Drop low-severity records from the ORM, whatever level it sets for itself.

    Filtering on the root logger is deliberate: the backend resets its own
    logger level during ``configure()``, so setting that level is not enough.
    Records still reach here because the check happens at the handler stage.
    """

    def __init__(self, level: int) -> None:
        super().__init__()
        self._level = level

    def filter(self, record: logging.LogRecord) -> bool:
        if not record.name.startswith("rhosocial"):
            return True
        return record.levelno >= self._level


def configure_logging() -> None:
    """Quiet the ORM down.

    Its default level is DEBUG and it logs every statement, which would bury the
    JSON envelope the CLI contract promises on stdout.  ``SCHEDULE_LOG_LEVEL``
    overrides.
    """
    raw = os.environ.get("SCHEDULE_LOG_LEVEL", "WARNING").upper()
    level = getattr(logging, raw, None)
    if not isinstance(level, int):
        level = logging.WARNING

    logging.getLogger("rhosocial").setLevel(level)
    logging.getLogger("schedule_manager").setLevel(level)

    root = logging.getLogger()
    marker = "schedule_manager.orm_filter"
    if not any(getattr(f, "marker", None) == marker for f in root.filters):
        quiet = _QuietOrm(level)
        quiet.marker = marker
        root.addFilter(quiet)



def _require(names: tuple[str, ...], *, what: str) -> None:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise DatabaseError(
            f"Missing {what}: "
            + ", ".join(missing)
            + ". Set them in the environment, or in the .env file next to the "
            "project root. When opencode injects them via mcp.environment, "
            "a {env:VAR} entry is only substituted when VAR is exported in "
            "the shell that launched opencode."
        )


def get_db_config() -> PostgresConnectionConfig:
    """Get database connection config from environment variables."""
    _ensure_env_loaded()
    _require(DB_ENV_VARS, what="database configuration")
    return PostgresConnectionConfig(
        host=os.environ["SCHEDULE_DB_HOST"],
        port=int(os.environ["SCHEDULE_DB_PORT"]),
        database=os.environ["SCHEDULE_DB_NAME"],
        username=os.environ["SCHEDULE_DB_USER"],
        password=os.environ["SCHEDULE_DB_PASSWORD"],
    )


def get_token() -> Optional[str]:
    """Return the bearer token for a local caller, or ``None`` if none is set.

    ``SCHEDULE_TOKEN_FILE`` is preferred over ``SCHEDULE_TOKEN`` when both are
    present: the file can be chmod 600, whereas a value exported into the
    environment is visible in ``/proc/<pid>/environ`` and to anything that can
    read the shell.  Neither should ever be committed — put the token in the
    client's own config, and have opencode substitute ``{env:VAR}``.
    """
    _ensure_env_loaded()
    path = os.environ.get("SCHEDULE_TOKEN_FILE")
    if path:
        resolved = Path(path).expanduser()
        if not resolved.exists():
            raise DatabaseError(f"SCHEDULE_TOKEN_FILE points at a missing file: {resolved}")
        token = resolved.read_text(encoding="utf-8").strip()
        if not token:
            raise DatabaseError(f"SCHEDULE_TOKEN_FILE is empty: {resolved}")
        return token
    token = (os.environ.get("SCHEDULE_TOKEN") or "").strip()
    return token or None


@dataclass(frozen=True)
class PoolConfigValues:
    """Connection pool sizing.

    ``min_size`` is the warmup count: 0 skips the warmup connections entirely,
    which is what a short-lived CLI process wants.  A long-running server should
    keep a small positive value so the first requests do not pay for a connect.
    """

    min_size: int = 1
    max_size: int = 10
    #: Note: with ``SCHEDULE_WORKERS > 1`` every worker opens its own pool, so
    #: the server needs ``workers * max_size`` connections in total.


def get_pool_config() -> "PoolConfigValues":
    _ensure_env_loaded()
    try:
        return PoolConfigValues(
            min_size=int(os.environ.get("SCHEDULE_POOL_MIN", "1")),
            max_size=int(os.environ.get("SCHEDULE_POOL_MAX", "10")),
        )
    except ValueError as e:
        raise DatabaseError(f"Invalid SCHEDULE_POOL_MIN/MAX: {e}") from e


@dataclass(frozen=True)
class ServerConfig:
    """Everything needed to expose the MCP server over Streamable HTTP."""

    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    public_url: str = "http://127.0.0.1:8000"
    allowed_hosts: tuple[str, ...] = ()
    allowed_origins: tuple[str, ...] = ()

    @property
    def mcp_path(self) -> str:
        return f"{self.public_url.rstrip('/')}/mcp"

    @property
    def hostname(self) -> str:
        """Host component of :attr:`public_url`, without the port."""
        return urlsplit(self.public_url).hostname or "127.0.0.1"

    @property
    def scheme(self) -> str:
        return urlsplit(self.public_url).scheme or "http"

    @property
    def is_loopback(self) -> bool:
        return self.hostname in {"127.0.0.1", "localhost", "::1"}

    def effective_allowed_hosts(self) -> list[str]:
        """Host allowlist for DNS-rebinding protection.

        The SDK matches entries exactly, except for a trailing ``:*`` which means
        "any port" (``server/transport_security.py:50-68``).  A bare ``*`` is
        *not* a wildcard — it only matches the literal string, so using one as a
        default would reject every request including the server's own.
        """
        if self.allowed_hosts:
            return list(self.allowed_hosts)
        return [f"{self.hostname}:*", "localhost:*", "127.0.0.1:*"]

    def effective_allowed_origins(self) -> list[str]:
        """Origin allowlist; an absent Origin header is allowed regardless."""
        if self.allowed_origins:
            return list(self.allowed_origins)
        return [f"{self.scheme}://{self.hostname}:*"]


def _split_list(raw: str) -> tuple[str, ...]:
    return tuple(item.strip() for item in raw.split(",") if item.strip())


def get_server_config() -> ServerConfig:
    """Read the remote-server settings, falling back to loopback defaults.

    Nothing here is required to import the package; ``run_server`` is what needs
    them.  ``SCHEDULE_PUBLIC_URL`` must be the externally reachable URL — it is
    what gets published as the resource identifier and therefore what access
    tokens are bound to.
    """
    _ensure_env_loaded()
    return ServerConfig(
        bind_host=os.environ.get("SCHEDULE_BIND_HOST", "127.0.0.1"),
        bind_port=int(os.environ.get("SCHEDULE_BIND_PORT", "8000")),
        public_url=os.environ.get("SCHEDULE_PUBLIC_URL", "http://127.0.0.1:8000"),
        allowed_hosts=_split_list(os.environ.get("SCHEDULE_ALLOWED_HOSTS", "")),
        allowed_origins=_split_list(os.environ.get("SCHEDULE_ALLOWED_ORIGINS", "")),
    )
