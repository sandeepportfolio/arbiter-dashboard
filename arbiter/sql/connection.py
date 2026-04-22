"""Shared asyncpg connection helpers.

Local sandboxes sometimes deny TCP connects to ``localhost`` even when Postgres
is listening. When that happens, retry through the local Unix-domain socket so
repo-local runtime validation can still use the configured DATABASE_URL.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import asyncpg

logger = logging.getLogger("arbiter.sql.connection")

_LOCALHOSTS = {"localhost", "127.0.0.1", "::1"}
_SOCKET_DIRS = (Path("/tmp"), Path("/private/tmp"))


def _local_socket_kwargs(database_url: str) -> dict[str, Any] | None:
    parsed = urlparse(database_url)
    if parsed.scheme not in {"postgres", "postgresql"}:
        return None
    if (parsed.hostname or "").lower() not in _LOCALHOSTS:
        return None

    port = parsed.port or 5432
    socket_dir = next(
        (
            candidate
            for candidate in _SOCKET_DIRS
            if (candidate / f".s.PGSQL.{port}").exists()
        ),
        None,
    )
    if socket_dir is None:
        return None

    database = parsed.path.lstrip("/")
    if not database:
        return None

    kwargs: dict[str, Any] = {
        "host": str(socket_dir),
        "port": port,
        "database": database,
    }
    if parsed.username:
        kwargs["user"] = unquote(parsed.username)
    if parsed.password:
        kwargs["password"] = unquote(parsed.password)
    return kwargs


async def connect(database_url: str, **kwargs):
    try:
        return await asyncpg.connect(database_url, **kwargs)
    except PermissionError as exc:
        socket_kwargs = _local_socket_kwargs(database_url)
        if socket_kwargs is None:
            raise
        logger.warning(
            "Postgres TCP connect was denied for %s; retrying via unix socket %s",
            parsed_db_name(database_url),
            socket_kwargs["host"],
        )
        return await asyncpg.connect(**socket_kwargs, **kwargs)


async def create_pool(database_url: str, **kwargs):
    try:
        return await asyncpg.create_pool(database_url, **kwargs)
    except PermissionError:
        socket_kwargs = _local_socket_kwargs(database_url)
        if socket_kwargs is None:
            raise
        logger.warning(
            "Postgres TCP pool connect was denied for %s; retrying via unix socket %s",
            parsed_db_name(database_url),
            socket_kwargs["host"],
        )
        return await asyncpg.create_pool(**socket_kwargs, **kwargs)


def parsed_db_name(database_url: str) -> str:
    parsed = urlparse(database_url)
    return parsed.path.lstrip("/") or "<unknown>"
