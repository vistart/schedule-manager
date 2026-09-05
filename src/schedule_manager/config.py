"""Database connection configuration.

Loads settings from environment variables, with .env file support.
"""

import os
from pathlib import Path

from dotenv import load_dotenv

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


def get_db_config() -> PostgresConnectionConfig:
    """Get database connection config from environment variables."""
    _ensure_env_loaded()
    return PostgresConnectionConfig(
        host=os.environ["SCHEDULE_DB_HOST"],
        port=int(os.environ["SCHEDULE_DB_PORT"]),
        database=os.environ["SCHEDULE_DB_NAME"],
        username=os.environ["SCHEDULE_DB_USER"],
        password=os.environ["SCHEDULE_DB_PASSWORD"],
    )
