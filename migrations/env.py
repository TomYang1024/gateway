import asyncio
import os

from alembic import context
from llm_gateway_infrastructure.storage import metadata
from sqlalchemy.ext.asyncio import create_async_engine


def run(connection):
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


async def main():
    engine = create_async_engine(
        os.environ.get("GATEWAY_DATABASE_URL", "postgresql+asyncpg://gateway:gateway@localhost:55432/gateway")
    )
    async with engine.connect() as connection:
        await connection.run_sync(run)
    await engine.dispose()


asyncio.run(main())
