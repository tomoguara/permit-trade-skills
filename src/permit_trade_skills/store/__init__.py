"""Canonical data store: session factory, repositories, query model, doctor, retention.

The repositories (``store.repos``) are the API surface for new code. Modules
outside ``store/`` and ``ingest/`` should not import SQLAlchemy directly (see
tests/unit/test_import_boundary.py for the enforced rule and its allowlist).
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator, Optional

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from permit_trade_skills.config import get_db_url


@lru_cache(maxsize=8)
def get_engine(db_url: Optional[str] = None) -> Engine:
    """Process-wide engine per URL (defaults to PERMIT_TRADE_SKILLS_DB_URL / .env)."""
    return create_engine(db_url or get_db_url(), pool_pre_ping=True)


def session_factory(db_url: Optional[str] = None) -> sessionmaker:
    return sessionmaker(bind=get_engine(db_url), autoflush=False, expire_on_commit=False)


@contextmanager
def transaction(db_url: Optional[str] = None) -> Iterator[Session]:
    """One transaction: commit on success, roll back on any exception."""
    session = session_factory(db_url)()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()
