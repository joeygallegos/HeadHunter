from __future__ import annotations
from dotenv import load_dotenv
from sqlalchemy import (
    Column,
    Integer,
    Numeric,
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
from sqlalchemy.orm import foreign, relationship

# Base, engine, and session setup live in app.db so every entry point resolves DB
# configuration the same way.
from .db import Base, make_engine, make_session_factory, resolve_db_url, utc_now_naive

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
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )
    finished_at = Column(DateTime(timezone=False), nullable=True)

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
        DateTime(timezone=False),
        default=utc_now_naive,
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
    ai_analyzed_at = Column(DateTime(timezone=False), nullable=True)

    # Structured compensation remains separate from the legacy Pay/AI Salary
    # strings so numeric filtering never depends on display formatting.
    base_pay_low = Column(Numeric(14, 2), nullable=True)
    base_pay_high = Column(Numeric(14, 2), nullable=True)
    pay_currency = Column(String(3), nullable=True)
    pay_period = Column(String(16), nullable=True)
    ote_low = Column(Numeric(14, 2), nullable=True)
    ote_high = Column(Numeric(14, 2), nullable=True)
    bonus_offered = Column(Boolean, nullable=True)
    equity_offered = Column(Boolean, nullable=True)
    commission_offered = Column(Boolean, nullable=True)
    multiple_pay_ranges = Column(Boolean, nullable=False, server_default="0")
    compensation_text = Column(Text, nullable=True)
    compensation_notes = Column(Text, nullable=True)
    compensation_source = Column(String(32), nullable=True)
    compensation_analyzed_at = Column(DateTime(timezone=False), nullable=True)
    compensation_schema_version = Column(Integer, nullable=True)

    # Delta tracking on the job itself
    content_hash = Column(String(64))  # sha256 of canonical fields
    is_active = Column(Boolean, nullable=False, server_default="1")
    first_seen_run_id = Column(Integer)
    last_seen_run_id = Column(Integer)
    updated_at = Column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
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
    fit_brief = relationship(
        "JobFitBrief", back_populates="job", uselist=False, cascade="all, delete-orphan"
    )
    application_prep = relationship(
        "JobApplicationPrep",
        primaryjoin=lambda: Job.id == foreign(JobApplicationPrep.job_pk),
        back_populates="job",
        uselist=False,
        cascade="all, delete-orphan",
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
        DateTime(timezone=False), server_default=func.now(), nullable=False
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
    change_details = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=True)
    created_at = Column(
        DateTime(timezone=False), server_default=func.now(), nullable=False
    )

    run = relationship("IntegrationRun", back_populates="changes")
    job = relationship("Job", back_populates="changes")


class JobFitBrief(Base):
    __tablename__ = "job_fit_briefs"
    __table_args__ = (
        UniqueConstraint("job_pk", name="uq_job_fit_brief_job_pk"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    job_pk = Column(Integer, ForeignKey("jobs.id", ondelete="CASCADE"), nullable=False)
    brief_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=False)
    resume_hash = Column(String(64), nullable=False)
    job_content_hash = Column(String(64), nullable=False)
    generated_at = Column(DateTime(timezone=False), nullable=False, default=utc_now_naive)
    schema_version = Column(Integer, nullable=False)

    job = relationship("Job", back_populates="fit_brief")


class JobApplicationPrep(Base):
    __tablename__ = "job_application_preps"
    __table_args__ = (
        UniqueConstraint("job_pk", name="uq_job_application_prep_job_pk"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    # Keep this dashboard-owned table creatable for DB users without REFERENCES
    # privilege; the app still joins it to jobs.id and enforces one row per job.
    job_pk = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, server_default="queued")
    prep_json = Column(LONGTEXT().with_variant(Text, "sqlite"), nullable=True)
    resume_hash = Column(String(64), nullable=True)
    job_content_hash = Column(String(64), nullable=True)
    schema_version = Column(Integer, nullable=True)
    queued_at = Column(DateTime(timezone=False), nullable=False, default=utc_now_naive)
    started_at = Column(DateTime(timezone=False), nullable=True)
    generated_at = Column(DateTime(timezone=False), nullable=True)
    error_text = Column(Text, nullable=True)

    job = relationship(
        "Job",
        primaryjoin=lambda: foreign(JobApplicationPrep.job_pk) == Job.id,
        back_populates="application_prep",
    )


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


def ensure_job_compensation_columns(bind=None) -> None:
    """Add structured compensation columns to existing databases."""
    target = bind or engine
    inspector = sa_inspect(target)
    if "jobs" not in inspector.get_table_names():
        return
    existing = {col["name"] for col in inspector.get_columns("jobs")}
    text_type = "LONGTEXT" if target.dialect.name == "mysql" else "TEXT"
    definitions = {
        "base_pay_low": "DECIMAL(14,2)",
        "base_pay_high": "DECIMAL(14,2)",
        "pay_currency": "VARCHAR(3)",
        "pay_period": "VARCHAR(16)",
        "ote_low": "DECIMAL(14,2)",
        "ote_high": "DECIMAL(14,2)",
        "bonus_offered": "BOOLEAN",
        "equity_offered": "BOOLEAN",
        "commission_offered": "BOOLEAN",
        "multiple_pay_ranges": "BOOLEAN NOT NULL DEFAULT 0",
        "compensation_text": text_type,
        "compensation_notes": text_type,
        "compensation_source": "VARCHAR(32)",
        "compensation_analyzed_at": "DATETIME",
        "compensation_schema_version": "INTEGER",
    }
    with target.begin() as conn:
        for name, column_type in definitions.items():
            if name not in existing:
                conn.execute(text(f"ALTER TABLE jobs ADD COLUMN {name} {column_type}"))


def ensure_job_change_details_column(bind=None) -> None:
    """Add rich change details for future dashboard diffs."""
    target = bind or engine
    inspector = sa_inspect(target)
    if "job_changes" not in inspector.get_table_names():
        return
    columns = {col["name"] for col in inspector.get_columns("job_changes")}
    if "change_details" in columns:
        return

    column_type = "LONGTEXT" if target.dialect.name == "mysql" else "TEXT"
    with target.begin() as conn:
        conn.execute(text(f"ALTER TABLE job_changes ADD COLUMN change_details {column_type}"))


def ensure_job_fit_briefs_table(bind=None) -> None:
    """Create and lightly repair the applicant-facing fit brief table."""
    target = bind or engine
    Base.metadata.tables["job_fit_briefs"].create(bind=target, checkfirst=True)
    inspector = sa_inspect(target)
    columns = {col["name"] for col in inspector.get_columns("job_fit_briefs")}
    text_type = "LONGTEXT" if target.dialect.name == "mysql" else "TEXT"
    definitions = {
        "brief_json": f"{text_type} NOT NULL",
        "resume_hash": "VARCHAR(64) NOT NULL",
        "job_content_hash": "VARCHAR(64) NOT NULL",
        "generated_at": "DATETIME NOT NULL",
        "schema_version": "INTEGER NOT NULL",
    }
    with target.begin() as conn:
        for name, column_type in definitions.items():
            if name not in columns:
                conn.execute(text(f"ALTER TABLE job_fit_briefs ADD COLUMN {name} {column_type}"))


def ensure_job_application_preps_table(bind=None) -> None:
    """Create and lightly repair the job-specific application prep table."""
    target = bind or engine
    Base.metadata.tables["job_application_preps"].create(bind=target, checkfirst=True)
    inspector = sa_inspect(target)
    columns = {col["name"] for col in inspector.get_columns("job_application_preps")}
    text_type = "LONGTEXT" if target.dialect.name == "mysql" else "TEXT"
    definitions = {
        "status": "VARCHAR(16) NOT NULL DEFAULT 'queued'",
        "prep_json": text_type,
        "resume_hash": "VARCHAR(64)",
        "job_content_hash": "VARCHAR(64)",
        "schema_version": "INTEGER",
        "queued_at": "DATETIME",
        "started_at": "DATETIME",
        "generated_at": "DATETIME",
        "error_text": text_type,
    }
    with target.begin() as conn:
        for name, column_type in definitions.items():
            if name not in columns:
                conn.execute(text(f"ALTER TABLE job_application_preps ADD COLUMN {name} {column_type}"))


def init_db() -> None:
    """Create tables and apply the small additive runtime schema updates."""
    Base.metadata.create_all(bind=engine)
    ensure_job_reference_fields_column(engine)
    ensure_job_compensation_columns(engine)
    ensure_job_change_details_column(engine)
    ensure_job_fit_briefs_table(engine)
    ensure_job_application_preps_table(engine)
