import logging
from pathlib import Path

from sqlalchemy import Connection, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.database.models import Base, Investigation

logger = logging.getLogger(__name__)


def database_backend(database_url: str) -> str:
    """Backend name only (e.g. "sqlite"), safe to log: the URL itself may contain a password."""
    return make_url(database_url).get_backend_name()


def create_engine(database_url: str) -> AsyncEngine:
    url = make_url(database_url)
    connect_args = {}
    if url.get_backend_name() == "sqlite":
        # The server's worker and the CLI can write at the same time; wait for the lock.
        connect_args["timeout"] = 30
        if url.database and url.database != ":memory:":
            Path(url.database).parent.mkdir(parents=True, exist_ok=True)
    return create_async_engine(database_url, connect_args=connect_args)


def _add_missing_columns(connection: Connection) -> list[str]:
    """Add nullable columns introduced after this database was created.

    A stand-in for full migrations: additive only, so it can never lose data. Columns that
    are not nullable are reported instead of guessed at.
    """
    table = Investigation.__table__
    existing = {column["name"] for column in inspect(connection).get_columns(table.name)}
    added = []
    for column in table.columns:
        if column.name in existing:
            continue
        if not column.nullable:
            logger.error("Column %s.%s is missing and not nullable; recreate the database", table.name, column.name)
            continue
        column_type = column.type.compile(connection.dialect)
        connection.execute(text(f"ALTER TABLE {table.name} ADD COLUMN {column.name} {column_type}"))
        added.append(column.name)
    return added


async def init_db(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        added = await connection.run_sync(_add_missing_columns)
    if added:
        logger.info("Added new database columns: %s", ", ".join(added))


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False)
