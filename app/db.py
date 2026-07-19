from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote_plus

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker, DeclarativeBase


class Base(DeclarativeBase):
    """Global metadata container."""


def utc_now_naive() -> datetime:
    """Return UTC without tzinfo for the app's database DATETIME convention."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _set_mysql_session_utc(dbapi_connection, _connection_record) -> None:
    """Make MySQL server defaults such as NOW() persist UTC wall-clock values."""
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("SET SESSION time_zone = '+00:00'")
    finally:
        cursor.close()


def _repo_root(script_dir: str | os.PathLike[str] | None = None) -> Path:
    # Allow legacy callers to pass the script root while keeping imports self-contained.
    if script_dir:
        return Path(script_dir).resolve()
    return Path(__file__).resolve().parents[1]


def _load_config_json(root: Path) -> dict:
    cfg_path = root / "config.json"
    if not cfg_path.exists():
        return {}
    with cfg_path.open("r", encoding="utf-8") as f:
        cfg = json.load(f)
    return cfg if isinstance(cfg, dict) else {}


def _config_db_url(root: Path) -> str | None:
    # Preserve the old config.json DB_URL override as a fallback after env vars.
    cfg = _load_config_json(root)
    value = cfg.get("DB_URL")
    return str(value) if value else None


def resolve_display_timezone(
    script_dir: str | os.PathLike[str] | None = None,
    default: str = "America/Chicago",
) -> str:
    """Resolve the user-facing timezone used for rendering timestamps."""
    root = _repo_root(script_dir)
    value = os.getenv("DISPLAY_TIMEZONE")
    if value:
        return str(value)
    cfg = _load_config_json(root)
    value = cfg.get("DISPLAY_TIMEZONE")
    return str(value) if value else default


def _with_mysql_charset(db_url: str) -> str:
    # MySQL needs utf8mb4 explicitly so scraped job text does not corrupt Unicode.
    if db_url.startswith("mysql+pymysql://") and "charset=" not in db_url:
        sep = "&" if "?" in db_url else "?"
        return f"{db_url}{sep}charset=utf8mb4"
    return db_url


def resolve_db_url(script_dir: str | os.PathLike[str] | None = None) -> str:
    """Resolve the app database URL from env, config.json, or local SQLite."""
    root = _repo_root(script_dir)
    # Keep one precedence order for CLI scripts, dashboard, and tests.
    direct = os.getenv("DB_URL") or _config_db_url(root)
    if direct:
        return _with_mysql_charset(direct)

    username = os.getenv("DB_USER", "")
    password = quote_plus(os.getenv("DB_PASS", ""))
    host = os.getenv("DB_HOST", "")
    port = os.getenv("DB_PORT", "3306")
    database = os.getenv("DB_NAME", "")
    if username and host and database:
        # Support split DB_* secrets for deployments that avoid one full URL value.
        return (
            f"mysql+pymysql://{username}:{password}@{host}:{port}/{database}"
            "?charset=utf8mb4"
        )

    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    # Local SQLite keeps a first run usable without provisioning MySQL.
    return f"sqlite:///{data_dir / 'jobs.db'}"


def make_engine(db_url: str):
    connect_args = {}
    if db_url.startswith("mysql+pymysql://"):
        # Mirror the URL charset in driver args for PyMySQL connections.
        connect_args["charset"] = "utf8mb4"
        connect_args["use_unicode"] = True
    elif db_url.startswith("sqlite"):
        # The dashboard and tests can share SQLite connections across threads.
        connect_args["check_same_thread"] = False
    engine = create_engine(
        db_url,
        future=True,
        pool_pre_ping=True,
        pool_size=10 if not db_url.startswith("sqlite") else None,
        max_overflow=5 if not db_url.startswith("sqlite") else None,
        pool_timeout=30 if not db_url.startswith("sqlite") else None,
        pool_recycle=1800 if not db_url.startswith("sqlite") else None,
        connect_args=connect_args,
    )
    if db_url.startswith("mysql+pymysql://"):
        # MySQL DATETIME does not retain timezone metadata, so every connection
        # must use UTC before server defaults or func.now() are evaluated.
        event.listen(engine, "connect", _set_mysql_session_utc)
    return engine


def make_session_factory(engine):
    # expire_on_commit=False keeps existing scripts from reloading rows after commits.
    return sessionmaker(
        bind=engine, autoflush=False, expire_on_commit=False, future=True
    )


def init_db(engine):
    # Why: single place to create all tables when running without migrations.
    from . import models  # ensures models are imported & mapped

    Base.metadata.create_all(engine)
