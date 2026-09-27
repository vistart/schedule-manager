"""Database connection configuration.

Loads settings from environment variables, with .env file support.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

from rhosocial.activerecord.backend.errors import DatabaseError
from rhosocial.activerecord.backend.impl.postgres import AsyncPostgresBackend
from rhosocial.activerecord.backend.impl.postgres.config import PostgresConnectionConfig

_ENV_LOADED = False


def _ensure_env_loaded() -> None:
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
    _ENV_LOADED = True


DB_ENV_VARS = (
    "SCHEDULE_DB_HOST",
    "SCHEDULE_DB_PORT",
    "SCHEDULE_DB_NAME",
    "SCHEDULE_DB_USER",
    "SCHEDULE_DB_PASSWORD",
)


def get_db_config() -> PostgresConnectionConfig:
    """Get database connection config from environment variables."""
    _ensure_env_loaded()
    missing = [name for name in DB_ENV_VARS if not os.environ.get(name)]
    if missing:
        raise DatabaseError(
            "Missing database configuration: "
            + ", ".join(missing)
            + ". Set them in the environment, or in the .env file next to the "
            "project root. When opencode injects them via mcp.environment, "
            "a {env:VAR} entry is only substituted when VAR is exported in "
            "the shell that launched opencode."
        )
    return PostgresConnectionConfig(
        host=os.environ["SCHEDULE_DB_HOST"],
        port=int(os.environ["SCHEDULE_DB_PORT"]),
        database=os.environ["SCHEDULE_DB_NAME"],
        username=os.environ["SCHEDULE_DB_USER"],
        password=os.environ["SCHEDULE_DB_PASSWORD"],
    )
