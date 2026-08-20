from pathlib import Path

import asyncpg

from app.common.settings import get_settings


async def run_migrations() -> None:
    settings = get_settings()
    connection = await asyncpg.connect(dsn=settings.database_dsn)
    try:
        await connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
        migrations_path = Path("migrations/versions")
        for path in sorted(migrations_path.glob("*.sql")):
            version = path.name
            already_applied = await connection.fetchval(
                "SELECT 1 FROM schema_migrations WHERE version = $1",
                version,
            )
            if already_applied:
                continue
            sql = path.read_text(encoding="utf-8")
            async with connection.transaction():
                await connection.execute(sql)
                await connection.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1)",
                    version,
                )
    finally:
        await connection.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(run_migrations())

