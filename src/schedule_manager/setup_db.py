"""Database schema setup. Run as: schedule-manager-setup-db"""

from __future__ import annotations

import asyncio

from rhosocial.activerecord.backend.impl.postgres import AsyncPostgresBackend

from .config import get_db_config
from .model import Schedule


async def _async_main() -> None:
    config = get_db_config()
    await Schedule.configure(config, AsyncPostgresBackend)
    backend = Schedule.backend()
    expr = Schedule.generate_create_table(if_not_exists=True)
    sql, params = expr.to_sql()
    await backend.execute(sql, params)
    print(f"Table 'schedules' ensured in database '{config.database}'.")
    await backend.disconnect()


def main() -> None:
    asyncio.run(_async_main())


if __name__ == "__main__":
    main()
