"""Create only the dedicated *_test database. Existing data is reset by test fixtures."""

import asyncio
import os

import asyncpg


async def setup():
    url = os.getenv(
        "GATEWAY_TEST_DATABASE_URL", "postgresql+asyncpg://gateway:gateway@localhost:55432/gateway_test"
    )
    parent, name = url.rsplit("/", 1)
    if not name.endswith("_test") or not name.replace("_", "").isalnum():
        raise RuntimeError("Refusing to create a database without the isolated _test suffix")
    connection = await asyncpg.connect(parent.replace("postgresql+asyncpg:", "postgresql:") + "/postgres")
    try:
        if not await connection.fetchval("SELECT 1 FROM pg_database WHERE datname=$1", name):
            try:
                await connection.execute(f'CREATE DATABASE "{name}"')
            except asyncpg.DuplicateDatabaseError:
                pass
    finally:
        await connection.close()


if __name__ == "__main__":
    asyncio.run(setup())
