import asyncio
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from tradio.config import get_settings


@lru_cache
def get_engine() -> Engine:
    return create_engine(get_settings().database_url, pool_pre_ping=True)


def session() -> Session:
    return sessionmaker(get_engine(), expire_on_commit=False)()


async def in_session(fn, *args):
    """Run a blocking database call off the event loop with its own session."""

    def call():
        with session() as s:
            return fn(s, *args)

    return await asyncio.to_thread(call)
