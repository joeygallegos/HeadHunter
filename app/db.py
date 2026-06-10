from __future__ import annotations
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, DeclarativeBase


class Base(DeclarativeBase):
    """Global metadata container."""


def make_engine(db_url: str):
    connect_args = {"check_same_thread": False} if db_url.startswith("sqlite") else {}
    return create_engine(
        db_url, future=True, pool_pre_ping=True, connect_args=connect_args
    )


def make_session_factory(engine):
    return sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )


def init_db(engine):
    # Why: single place to create all tables when running without migrations.
    from . import models  # ensures models are imported & mapped

    Base.metadata.create_all(engine)
