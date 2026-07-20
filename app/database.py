from __future__ import annotations

from collections.abc import Generator

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings


class Base(DeclarativeBase):
    pass


engine = (
    create_engine(settings.database_url, pool_pre_ping=True, future=True)
    if settings.database_url
    else None
)
SessionLocal = (
    sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
    if engine is not None
    else None
)


def init_db() -> None:
    if engine is not None:
        Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    if SessionLocal is None:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL is not configured. Set it to enable conversation history.",
        )

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()