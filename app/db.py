from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import settings


SCHEMA_SQL = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")

pool = ConnectionPool(
    conninfo=settings.database_url,
    min_size=1,
    max_size=12,
    timeout=20,
    kwargs={"autocommit": True, "row_factory": dict_row},
    open=False,
)


def open_pool() -> None:
    pool.open(wait=True, timeout=60)
    with pool.connection() as conn:
        conn.execute(SCHEMA_SQL)


def close_pool() -> None:
    pool.close()


def fetch_one(query: str, params: Iterable[Any] | None = None) -> dict[str, Any] | None:
    with pool.connection() as conn:
        return conn.execute(query, params or ()).fetchone()


def fetch_all(query: str, params: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    with pool.connection() as conn:
        return list(conn.execute(query, params or ()).fetchall())

