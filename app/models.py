from __future__ import annotations
from datetime import datetime, timezone
from dotenv import load_dotenv
from sqlalchemy import (
    Column,
    Integer,
    DateTime,
    String,
    Text,
    ForeignKey,
    UniqueConstraint,
    Boolean,
    func,
    inspect as sa_inspect,
    text,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import relationship

# Base, engine, and session setup live in app.db so every entry point resolves DB
# configuration the same way.
from .db import Base, make_engine, make_session_factory, resolve_db_url

load_dotenv()

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
    # Extra scraper columns are stored as JSON text so site-specific references
    # like Location, JobSummary, OpenDate, and CloseDate are not discarded.
    reference_fields = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=True)
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
# Keep these public names stable for scripts that import app.models directly.
DATABASE_URL = resolve_db_url()
engine = make_engine(DATABASE_URL)
SessionLocal = make_session_factory(engine)


def ensure_job_reference_fields_column(bind=None) -> None:
    """Add the lightweight reference-fields column on existing databases."""
    target = bind or engine
    inspector = sa_inspect(target)
    if "jobs" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("jobs")}
    if "reference_fields" in columns:
        return

    # SQLAlchemy create_all does not ALTER existing tables, so keep this one
    # additive migration here instead of introducing a migration framework.
    column_type = "LONGTEXT" if target.dialect.name == "mysql" else "TEXT"
    with target.begin() as conn:
        conn.execute(text(f"ALTER TABLE jobs ADD COLUMN reference_fields {column_type}"))


def init_db() -> None:
    """Create tables and apply the small additive runtime schema updates."""
    Base.metadata.create_all(bind=engine)
    ensure_job_reference_fields_column(engine)
