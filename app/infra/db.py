"""Database engine and session management.

The engine is created from a single ``DATABASE_URL`` environment variable so
the storage backend is a deployment concern, not a code concern.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.domain.models import Base


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Copy .env.example to .env and fill in "
            "your Postgres connection string."
        )
    # Accept the postgres:// form some providers hand out.
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql+psycopg://", 1)
    elif url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


# pool_pre_ping guards against connections dropped by the provider between
# calls - without it the first request after an idle period fails.
engine = create_engine(
    _database_url(),
    pool_pre_ping=True,
    pool_size=5,
    max_overflow=5,
    future=True,
)

SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables if they do not exist.

    Trade-off: for a 3-hour build this replaces Alembic. Documented in the
    README - a real deployment wants versioned migrations.
    """
    Base.metadata.create_all(engine)


def get_session() -> Iterator[Session]:
    """FastAPI dependency yielding a request-scoped session."""
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@contextmanager
def session_scope() -> Iterator[Session]:
    """Standalone transactional scope, for scripts and background work."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
