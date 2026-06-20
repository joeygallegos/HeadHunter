from __future__ import annotations
from datetime import datetime, timezone
import os
from urllib.parse import quote_plus
from dotenv import load_dotenv
from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    DateTime,
    String,
    Text,
    ForeignKey,
    UniqueConstraint,
    Boolean,
    func,
    Enum,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker

load_dotenv()
Base = declarative_base()

# ---------- ORM MODELS (utf8mb4 safe defaults for MySQL) ----------


class IntegrationRun(Base):
    __tablename__ = "integration_runs"
    __table_args__ = {
        "mysql_charset": "utf8mb4",
        "mysql_collate": "utf8mb4_unicode_ci",
    }

    id = Column(Integer, primary_key=True, autoincrement=True)
    started_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    finished_at = Column(DateTime(timezone=True), nullable=True)

    user = Column(String(255), nullable=False)
    mode = Column(String(64), nullable=False)
    notes = Column(Text, nullable=True)

    # Delta counters
    total_seen = Column(Integer, server_default="0", nullable=False)
    inserted_count = Column(Integer, server_default="0", nullable=False)
    updated_count = Column(Integer, server_default="0", nullable=False)
    missing_count = Column(Integer, server_default="0", nullable=False)
    unchanged_count = Column(Integer, server_default="0", nullable=False)
    error_count = Column(Integer, server_default="0", nullable=False)

    jobs = relationship("Job", back_populates="run", cascade="all, delete-orphan")
    changes = relationship(
        "JobChange", back_populates="run", cascade="all, delete-orphan"
    )


class Job(Base):
    __tablename__ = "jobs"
    __table_args__ = (
        UniqueConstraint("job_id", "site", name="uq_job_site"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True, index=True)
    job_id = Column(String(255), nullable=False)
    site = Column(String(255), nullable=False)

    title = Column(String(512))
    url = Column(String(1024))
    desc = Column(LONGTEXT().with_variant(Text, "sqlite"))
    keywords = Column(LONGTEXT().with_variant(Text, "sqlite"))
    level = Column(String(64))
    pay = Column(String(255))
    discovery_date = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )

    ai_analysis = Column(Text, nullable=True)
    ai_match_percentage = Column(Integer, nullable=True)
    ai_salary = Column(String(255), nullable=True)
    ai_fit_summary = Column(Text, nullable=True)
    ai_keywords_overlap = Column(Text, nullable=True)
    ai_missing_keywords = Column(Text, nullable=True)
    ai_experience_match = Column(String(32), nullable=True)
    ai_location_policy_match = Column(String(32), nullable=True)
    ai_analyzed_at = Column(DateTime(timezone=True), nullable=True)

    # Delta tracking on the job itself
    content_hash = Column(String(64))  # sha256 of canonical fields
    is_active = Column(Boolean, nullable=False, server_default="1")
    first_seen_run_id = Column(Integer)
    last_seen_run_id = Column(Integer)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    run_id = Column(
        Integer, ForeignKey("integration_runs.id", ondelete="CASCADE"), nullable=False
    )
    run = relationship("IntegrationRun", back_populates="jobs")
    changes = relationship(
        "JobChange", back_populates="job", cascade="all, delete-orphan"
    )
    swipe = relationship(
        "JobSwipe", back_populates="job", uselist=False, cascade="all, delete-orphan"
    )


class JobSwipe(Base):
    __tablename__ = "job_swipes"
    __table_args__ = (
        UniqueConstraint("job_pk", name="uq_job_swipe_job_pk"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_pk = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    action = Column(String(16), nullable=False)
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    job = relationship("Job", back_populates="swipe")


class JobChange(Base):
    __tablename__ = "job_changes"
    __table_args__ = {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"}

    id = Column(Integer, primary_key=True, autoincrement=True)
    run_id = Column(
        Integer, ForeignKey("integration_runs.id", ondelete="CASCADE"), nullable=False
    )
    job_id_text = Column(String(255), nullable=False)  # the natural job id (not PK)
    site = Column(String(255), nullable=False)
    job_pk = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=True)

    change_type = Column(String(16), nullable=False)  # 'insert' | 'update' | 'missing'
    change_source = Column(String(32), nullable=True)  # 'site' | 'ai'
    old_hash = Column(String(64))
    new_hash = Column(String(64))
    changed_fields = Column(Text)  # comma-separated list or JSON
    created_at = Column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    run = relationship("IntegrationRun", back_populates="changes")
    job = relationship("Job", back_populates="changes")


# ---------- DB URL resolution (env-first, enforced utf8mb4 for MySQL) ----------
def _resolve_db_url() -> str:
    direct = os.getenv("DB_URL")
    if direct:
        if direct.startswith("mysql+pymysql://") and "charset=" not in direct:
            sep = "&" if "?" in direct else "?"
            direct = f"{direct}{sep}charset=utf8mb4"
        return direct

    username = os.getenv("DB_USER", "")
    password = quote_plus(os.getenv("DB_PASS", ""))
    host = os.getenv("DB_HOST", "")
    port = os.getenv("DB_PORT", "3306")
    database = os.getenv("DB_NAME", "")
    if username and host and database:
        return f"mysql+pymysql://{username}:{password}@{host}:{port}/{database}?charset=utf8mb4"
    os.makedirs("data", exist_ok=True)
    return "sqlite:///data/jobs.db"


DATABASE_URL = _resolve_db_url()

_connect_args = {}
if DATABASE_URL.startswith("mysql+pymysql://"):
    _connect_args["charset"] = "utf8mb4"
    _connect_args["use_unicode"] = True
elif DATABASE_URL.startswith("sqlite"):
    _connect_args["check_same_thread"] = False

engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_pre_ping=True,
    pool_size=10 if not DATABASE_URL.startswith("sqlite") else None,
    max_overflow=5 if not DATABASE_URL.startswith("sqlite") else None,
    pool_timeout=30 if not DATABASE_URL.startswith("sqlite") else None,
    pool_recycle=1800 if not DATABASE_URL.startswith("sqlite") else None,
    connect_args=_connect_args,
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def init_db() -> None:
    """Create tables if they don't exist. (Does not ALTER existing tables.)"""
    Base.metadata.create_all(bind=engine)
