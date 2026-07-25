from __future__ import annotations

from collections.abc import Generator

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy import inspect, text
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
    if engine is None:
        raise RuntimeError("DATABASE_URL is required. Coder Agent no longer supports running without a database.")
    Base.metadata.create_all(bind=engine)
    _ensure_active_file_column("messages")
    _ensure_active_file_column("conversation_tasks")


def _ensure_active_file_column(table_name: str) -> None:
    if engine is None:
        return
    inspector = inspect(engine)
    if table_name not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    if "active_file" in columns:
        return
    index_name = f"ix_{table_name}_active_file"
    with engine.begin() as connection:
        connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN active_file VARCHAR(260)"))
        try:
            connection.execute(text(f"CREATE INDEX {index_name} ON {table_name} (active_file)"))
        except Exception:
            pass

def get_db() -> Generator[Session, None, None]:
    if SessionLocal is None:
        raise HTTPException(
            status_code=503,
            detail="DATABASE_URL is required. Coder Agent no longer supports running without a database.",
        )

    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()