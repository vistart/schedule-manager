"""Database schema setup. Run as: schedule-manager-setup-db"""

from __future__ import annotations

import asyncio
import sys

from .config import configure_logging, get_db_config
from .db import close_pool, connection, create_pool
from .schema import create_all


async def _async_main() -> None:
    configure_logging()
    config = get_db_config()
    pool = await create_pool(config)
    configure_logging()
    try:
        async with connection(pool) as backend:
            tables = await create_all(backend)
        print(f"Tables {', '.join(tables)} ensured in database '{config.database}'.")
        print("Next: schedule-manager user open --username <name>   (prints a token once)")
    finally:
        await close_pool(pool)


def main() -> None:
    try:
        asyncio.run(_async_main())
    except Exception as exc:
        print(f"schedule-manager-setup-db failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
