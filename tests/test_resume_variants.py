from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import dashboard
from app import models
from app.models import Base, IntegrationRun, Job, JobApplicationPrep, ResumeSourceSettings
from app.resume_pdf import (
    _document_geometry,
    _effective_google_styles,
    render_resume_pdf_bytes,
    resolve_resume_blocks,
)
from app.resume_variants import (
    RESUME_SOURCE_MODE_GOOGLE_DOC,
    RESUME_SOURCE_MODE_RESUME_TXT,
    RESUME_VARIANT_STAGE_COMPLETE,
    RESUME_VARIANT_STATUS_DONE,
    stable_json_hash,
    validate_approved_pairs,
    validate_swap_analysis,
)


def _valid_analysis() -> dict:
    return {
        "baseline_candidates": [
            {
                "anchor_id": "work-1",
                "bullet": "Maintained legacy reporting dashboards for internal teams.",
                "section": "Experience",
                "value_band": "lower_for_job",
                "confidence": "medium",
                "lower_value_rationale": "Useful, but less aligned to the role's security automation focus.",
            }
        ],
        "replacement_candidates": [
            {
                "candidate_id": "draft-1",
                "bullet": "Built security automation that reduced manual review time by 30%.",
                "evidence_sources": [
                    {"source": "resume", "evidence": "Built security automation"},
                    {"source": "responsibilities_inventory", "evidence": "reduced manual review time by 30%"},
                ],
                "job_requirement": "Security automation and operational scale.",
                "confidence": "high",
                "value_rationale": "Better supports the posting's automation requirement with a quantified outcome.",
            }
        ],
        "suggested_pairs": [
            {
                "anchor_id": "work-1",
                "candidate_id": "draft-1",
                "comparative_rationale": "The replacement is more directly tied to security automation.",
            }
        ],
        "coverage_summary": "One swap improves security automation positioning.",
        "remaining_gaps": [],
    }


class _FakeFreshBaselineGoogleClient:
    def document_snapshot(self, document_id: str) -> dict:
        assert document_id == "doc-1"
        return {
            "snapshot": {
                "baseline_hash": "n" * 64,
                # Simulate an unchanged baseline that was synced before
                # style metadata became part of the primary hash.
                "legacy_render_hash": "b" * 64,
            }
        }


def test_swap_analysis_contract_accepts_grounded_candidate_shape() -> None:
    analysis = _valid_analysis()

    ok, why = validate_swap_analysis(analysis)

    assert ok is True
    assert why == ""
    assert analysis["replacement_candidates"][0]["confidence"] == "high"


def test_local_resume_renderer_preserves_blocks_and_swaps_one_bullet() -> None:
    snapshot = {
        "title": "Baseline Resume",
        "blocks": [
            {"type": "paragraph", "text": "Joey Resume"},
            {"type": "paragraph", "text": "joey@example.com | Chicago, IL"},
            {"type": "paragraph", "text": "Experience"},
            {"type": "paragraph", "text": "Security Engineer | Example Co | 2024"},
            {"type": "bullet", "anchor_id": "keep-1", "text": "Kept existing bullet.", "bullet": "Kept existing bullet."},
            {"type": "bullet", "anchor_id": "swap-1", "text": "Old lower-value bullet.", "bullet": "Old lower-value bullet."},
        ],
        "bullets": [
            {"anchor_id": "keep-1", "bullet": "Kept existing bullet."},
            {"anchor_id": "swap-1", "bullet": "Old lower-value bullet."},
        ],
    }

    blocks = resolve_resume_blocks(
        snapshot,
        [{"anchor_id": "swap-1", "approved_bullet": "New stronger bullet."}],
    )
    pdf_bytes = render_resume_pdf_bytes(
        snapshot,
        [{"anchor_id": "swap-1", "approved_bullet": "New stronger bullet."}],
    )

    assert [block.get("text") for block in blocks] == [
        "Joey Resume",
        "joey@example.com | Chicago, IL",
        "Experience",
        "Security Engineer | Example Co | 2024",
        "Kept existing bullet.",
        "New stronger bullet.",
    ]
    assert pdf_bytes.startswith(b"%PDF")


def test_local_resume_renderer_preserves_table_sections() -> None:
    snapshot = {
        "title": "Baseline Resume",
        "blocks": [
            {"type": "paragraph", "text": "Joseph Gallegos"},
            {
                "type": "table",
                "rows": [
                    [
                        {
                            "blocks": [
                                {"type": "paragraph", "text": "CERTIFICATIONS"},
                                {"type": "bullet", "anchor_id": "cert-1", "text": "CompTIA Security+, CySA+", "bullet": "CompTIA Security+, CySA+"},
                            ]
                        },
                        {
                            "blocks": [
                                {"type": "paragraph", "text": "AWARDS AND ACCOLADES"},
                                {"type": "bullet", "anchor_id": "award-1", "text": "Hackathon Finalist, Wolters Kluwer - 2023 & 2024", "bullet": "Hackathon Finalist, Wolters Kluwer - 2023 & 2024"},
                            ]
                        },
                    ]
                ],
            },
            {"type": "paragraph", "text": "EXPERIENCE"},
        ],
    }

    blocks = resolve_resume_blocks(snapshot, [])
    pdf_bytes = render_resume_pdf_bytes(snapshot, [])

    assert blocks[1]["type"] == "table"
    assert blocks[1]["rows"][0][0]["blocks"][0]["text"] == "CERTIFICATIONS"
    assert blocks[1]["rows"][0][1]["blocks"][1]["text"].startswith("Hackathon Finalist")
    assert pdf_bytes.startswith(b"%PDF")


def test_local_resume_renderer_accepts_job_header_lines_with_right_side_details() -> None:
    snapshot = {
        "blocks": [
            {"type": "paragraph", "text": "Joseph Gallegos"},
            {"type": "paragraph", "text": "EXPERIENCE"},
            {"type": "paragraph", "text": "Wolters Kluwer - Governance, Risk and Compliance Houston, Texas"},
            {"type": "paragraph", "text": "IT Incident Response Analyst APR 2019 - JUN 2022"},
            {"type": "bullet", "anchor_id": "job-1", "text": "Provided real-time restoration.", "bullet": "Provided real-time restoration."},
        ]
    }

    pdf_bytes = render_resume_pdf_bytes(snapshot, [])

    assert pdf_bytes.startswith(b"%PDF")


def test_local_resume_renderer_resolves_inherited_google_heading_bold() -> None:
    role_block = {
        "type": "paragraph",
        "text": "IT Cybersecurity Analyst    JUN 2022 - PRESENT",
        "paragraph_style": {"namedStyleType": "HEADING_2"},
        "text_runs": [
            {
                "text": "IT Cybersecurity Analyst    JUN 2022 - PRESENT",
                "text_style": {
                    "fontSize": {"magnitude": 9, "unit": "PT"},
                    "weightedFontFamily": {"fontFamily": "Arial", "weight": 400},
                },
            }
        ],
    }
    snapshot = {
        "blocks": [role_block],
        "named_styles": {
            "styles": [
                {
                    "namedStyleType": "NORMAL_TEXT",
                    "textStyle": {
                        "bold": False,
                        "fontSize": {"magnitude": 9, "unit": "PT"},
                    },
                },
                {
                    "namedStyleType": "HEADING_2",
                    "textStyle": {"bold": True, "fontSize": {"magnitude": 11, "unit": "PT"}},
                },
            ]
        },
    }

    _paragraph_style, text_style = _effective_google_styles(
        role_block, snapshot, role_block["text_runs"][0]["text_style"]
    )
    pdf_bytes = render_resume_pdf_bytes(snapshot, [])

    # The run overrides the inherited size, but not the inherited bold flag.
    assert text_style["bold"] is True
    assert text_style["fontSize"]["magnitude"] == 9
    assert pdf_bytes.startswith(b"%PDF")


def test_local_resume_renderer_uses_google_page_geometry() -> None:
    snapshot = {
        "document_style": {
            "pageSize": {
                "width": {"magnitude": 612, "unit": "PT"},
                "height": {"magnitude": 792, "unit": "PT"},
            },
            "marginLeft": {"magnitude": 43.2, "unit": "PT"},
            "marginRight": {"magnitude": 55.5, "unit": "PT"},
            "marginTop": {"magnitude": 28.8, "unit": "PT"},
            "marginBottom": {"magnitude": 43.2, "unit": "PT"},
        }
    }

    page_size, margins = _document_geometry(snapshot, (100, 200), 72)

    assert page_size == (612, 792)
    assert margins == (43.2, 55.5, 28.8, 43.2)


def test_local_resume_renderer_rejects_unknown_anchor() -> None:
    snapshot = {
        "blocks": [
            {"type": "paragraph", "text": "Experience"},
            {"type": "bullet", "anchor_id": "known", "text": "Known bullet.", "bullet": "Known bullet."},
        ]
    }

    try:
        resolve_resume_blocks(snapshot, [{"anchor_id": "missing", "approved_bullet": "Replacement."}])
    except ValueError as exc:
        assert "not in the synced baseline" in str(exc)
    else:
        raise AssertionError("expected unknown anchor to fail")


def test_swap_analysis_contract_rejects_duplicate_baseline_anchor() -> None:
    analysis = _valid_analysis()
    analysis["baseline_candidates"].append(dict(analysis["baseline_candidates"][0]))

    ok, why = validate_swap_analysis(analysis)

    assert ok is False
    assert "anchor_id values must be unique" in why


def test_swap_analysis_contract_requires_remaining_gaps_list() -> None:
    analysis = _valid_analysis()
    analysis["remaining_gaps"] = "missing IAM"

    ok, why = validate_swap_analysis(analysis)

    assert ok is False
    assert "remaining_gaps must be a list" in why


def test_approved_pairs_require_acknowledgement_for_edited_bullets() -> None:
    analysis = _valid_analysis()
    ok, why = validate_swap_analysis(analysis)
    assert ok, why

    pairs = [
        {
            "anchor_id": "work-1",
            "candidate_id": "draft-1",
            "approved_bullet": "Built security automation across cloud operations.",
            "edited": True,
            "grounding_acknowledged": False,
        }
    ]

    ok, why, normalized = validate_approved_pairs(pairs, analysis)

    assert ok is False
    assert normalized == []
    assert "grounding acknowledgment" in why


def test_approved_pairs_detect_changed_text_even_when_client_edited_flag_is_false() -> None:
    analysis = _valid_analysis()
    ok, why = validate_swap_analysis(analysis)
    assert ok, why

    pairs = [
        {
            "anchor_id": "work-1",
            "candidate_id": "draft-1",
            "approved_bullet": "Built security automation across cloud operations.",
            "edited": False,
            "grounding_acknowledged": False,
        }
    ]

    ok, why, normalized = validate_approved_pairs(pairs, analysis)

    assert ok is False
    assert normalized == []
    assert "grounding acknowledgment" in why


def test_approved_pairs_marks_changed_text_as_edited_after_acknowledgement() -> None:
    analysis = _valid_analysis()
    ok, why = validate_swap_analysis(analysis)
    assert ok, why

    pairs = [
        {
            "anchor_id": "work-1",
            "candidate_id": "draft-1",
            "approved_bullet": "Built security automation across cloud operations.",
            "edited": False,
            "grounding_acknowledged": True,
        }
    ]

    ok, why, normalized = validate_approved_pairs(pairs, analysis)

    assert ok is True
    assert why == ""
    assert normalized[0]["edited"] is True


def test_init_db_creates_resume_variant_phase_one_tables(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    monkeypatch.setattr(models, "engine", test_engine)

    models.init_db()

    tables = set(sa_inspect(test_engine).get_table_names())
    assert "resume_source_settings" in tables
    assert "job_resume_variants" in tables


def test_dashboard_resume_source_settings_are_per_host_and_resume_txt_review_only(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(dashboard, "SessionLocal", Session)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", False)
    monkeypatch.setenv("RESUME_VARIANT_HOST_ID", "phase-one-host")

    saved = dashboard.save_resume_source_settings({"source_mode": RESUME_SOURCE_MODE_RESUME_TXT})
    fetched = dashboard.fetch_resume_source_settings()

    assert saved["host_id"] == "phase-one-host"
    assert fetched["source_mode"] == RESUME_SOURCE_MODE_RESUME_TXT
    assert fetched["resume_txt_review_only"] is True
    assert fetched["can_apply"] is False


def test_dashboard_creates_resume_variant_pdf_with_validated_pairs(monkeypatch, tmp_path) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(dashboard, "SessionLocal", Session)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", False)
    monkeypatch.setattr(dashboard, "_current_resume_hash", lambda: "resume-hash")
    monkeypatch.setattr(dashboard, "_google_resume_client", lambda: _FakeFreshBaselineGoogleClient())
    monkeypatch.setattr(dashboard, "OUTPUT_DIR", str(tmp_path))
    monkeypatch.setenv("RESUME_VARIANT_HOST_ID", "localhost")

    prep_payload = {
        "draft_resume_bullets": [
            {
                "bullet": "Built security automation that reduced manual review time by 30%.",
                "evidence_sources": [{"source": "resume", "evidence": "Built security automation"}],
                "job_requirement": "Security automation",
                "confidence": "high",
            }
        ],
        "fit_gaps": [],
    }
    baseline_snapshot = {
        "baseline_hash": "b" * 64,
        "blocks": [
            {"type": "paragraph", "text": "Joey Resume", "section": "Joey Resume"},
            {"type": "paragraph", "text": "Experience", "section": "Experience"},
            {
                "type": "bullet",
                "anchor_id": "work-1",
                "bullet": "Maintained legacy reporting dashboards for internal teams.",
                "text": "Maintained legacy reporting dashboards for internal teams.",
                "section": "Experience",
                "start_index": 10,
                "end_index": 70,
                "text_start_index": 10,
                "text_end_index": 69,
                "text_hash": "c" * 64,
            },
        ],
        "bullets": [
            {
                "anchor_id": "work-1",
                "bullet": "Maintained legacy reporting dashboards for internal teams.",
                "section": "Experience",
                "start_index": 10,
                "end_index": 70,
                "text_start_index": 10,
                "text_end_index": 69,
                "text_hash": "c" * 64,
            }
        ],
    }
    with Session() as session:
        run = IntegrationRun(user="tester", mode="test")
        session.add(run)
        session.flush()
        job = Job(
            site="site",
            job_id="job-1",
            title="Security Engineer",
            desc="Build security automation",
            run_id=run.id,
            content_hash="job-hash",
            ai_match_percentage=90,
        )
        session.add(job)
        session.flush()
        session.add(
            JobApplicationPrep(
                job_pk=job.id,
                status="done",
                prep_json=dashboard.json.dumps(prep_payload),
                resume_hash="resume-hash",
                responsibilities_hash="",
                job_content_hash="job-hash",
                schema_version=dashboard.APPLICATION_PREP_SCHEMA_VERSION,
            )
        )
        session.add(
            ResumeSourceSettings(
                host_id="localhost",
                source_mode=RESUME_SOURCE_MODE_GOOGLE_DOC,
                google_document_id="doc-1",
                google_document_name="Baseline Resume",
                baseline_hash="b" * 64,
                baseline_snapshot_json=dashboard.json.dumps(baseline_snapshot),
            )
        )
        session.commit()
        job_pk = job.id

    analysis_result = dashboard.fetch_resume_swap_analysis(job_pk)
    result = dashboard.create_resume_variant_draft(
        job_pk,
        {
            "analysis_hash": analysis_result["analysis_hash"],
            "replacements": [
                {
                    "anchor_id": "work-1",
                    "candidate_id": "draft-1",
                    "approved_bullet": "Built security automation that reduced manual review time by 30%.",
                    "edited": False,
                    "grounding_acknowledged": False,
                }
            ],
        },
    )

    assert result["found"] is True
    assert result["can_apply"] is True
    assert result["variant"]["status"] == RESUME_VARIANT_STATUS_DONE
    assert result["variant"]["stage"] == RESUME_VARIANT_STAGE_COMPLETE
    assert result["variant"]["has_pdf"] is True
    assert result["variant"]["baseline_hash"] == "n" * 64
    assert result["variant"]["copied_document_url"] == ""
    assert result["variant"]["application_prep_hash"] == stable_json_hash(prep_payload)
    assert result["variant"]["replacements"][0]["original_bullet"].startswith("Maintained legacy")

    path, filename = dashboard.download_resume_variant_pdf(result["variant"]["id"])
    assert filename.endswith(".pdf")
    assert tmp_path in tmp_path.__class__(path).parents


def test_dashboard_reports_google_doc_source_as_apply_ready_after_snapshot(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(dashboard, "SessionLocal", Session)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", False)
    monkeypatch.setenv("RESUME_VARIANT_HOST_ID", "localhost")

    with Session() as session:
        settings = dashboard._get_or_create_resume_source_settings(session)
        settings.source_mode = RESUME_SOURCE_MODE_GOOGLE_DOC
        settings.google_document_id = "doc-1"
        settings.google_document_name = "Baseline Resume"
        settings.baseline_hash = "a" * 64
        session.commit()

    fetched = dashboard.fetch_resume_source_settings()

    assert fetched["source_mode"] == RESUME_SOURCE_MODE_GOOGLE_DOC
    assert fetched["can_apply"] is True
    assert fetched["resume_txt_review_only"] is False


def test_dashboard_builds_swap_analysis_from_synced_baseline_and_prep(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(dashboard, "SessionLocal", Session)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", False)
    monkeypatch.setattr(dashboard, "_current_resume_hash", lambda: "resume-hash")
    monkeypatch.setenv("RESUME_VARIANT_HOST_ID", "localhost")

    prep_payload = {
        "draft_resume_bullets": [
            {
                "bullet": "Built security automation that reduced manual review time by 30%.",
                "evidence_sources": [{"source": "resume", "evidence": "Built security automation"}],
                "job_requirement": "Security automation",
                "confidence": "high",
            }
        ],
        "fit_gaps": ["IAM depth"],
    }
    baseline_snapshot = {
        "baseline_hash": "b" * 64,
        "bullets": [
            {
                "anchor_id": "baseline-1",
                "bullet": "Maintained legacy reporting dashboards for internal teams.",
                "section": "Experience",
                "start_index": 10,
                "end_index": 70,
                "text_start_index": 10,
                "text_end_index": 69,
                "text_hash": "c" * 64,
            }
        ],
    }

    with Session() as session:
        run = IntegrationRun(user="tester", mode="test")
        session.add(run)
        session.flush()
        job = Job(
            site="site",
            job_id="job-1",
            title="Security Engineer",
            desc="Build security automation",
            run_id=run.id,
            content_hash="job-hash",
            ai_match_percentage=90,
        )
        session.add(job)
        session.flush()
        session.add(
            JobApplicationPrep(
                job_pk=job.id,
                status="done",
                prep_json=dashboard.json.dumps(prep_payload),
                resume_hash="resume-hash",
                responsibilities_hash="",
                job_content_hash="job-hash",
                schema_version=dashboard.APPLICATION_PREP_SCHEMA_VERSION,
            )
        )
        session.add(
            ResumeSourceSettings(
                host_id="localhost",
                source_mode=RESUME_SOURCE_MODE_GOOGLE_DOC,
                google_document_id="doc-1",
                google_document_name="Baseline Resume",
                baseline_hash="b" * 64,
                baseline_snapshot_json=dashboard.json.dumps(baseline_snapshot),
            )
        )
        session.commit()
        job_pk = job.id

    result = dashboard.fetch_resume_swap_analysis(job_pk)

    assert result["found"] is True
    assert result["analysis"]["baseline_candidates"][0]["anchor_id"] == "baseline-1"
    assert result["analysis"]["replacement_candidates"][0]["candidate_id"] == "draft-1"
    assert result["analysis"]["suggested_pairs"][0] == {
        "anchor_id": "baseline-1",
        "candidate_id": "draft-1",
        "comparative_rationale": "This replacement more directly supports: Security automation",
    }


def test_dashboard_swap_analysis_keeps_already_strong_baseline_bullet(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(dashboard, "SessionLocal", Session)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", False)
    monkeypatch.setattr(dashboard, "_current_resume_hash", lambda: "resume-hash")
    monkeypatch.setenv("RESUME_VARIANT_HOST_ID", "localhost")

    prep_payload = {
        "draft_resume_bullets": [
            {
                "bullet": "Built security automation for cloud services.",
                "evidence_sources": [{"source": "resume", "evidence": "Built security automation"}],
                "job_requirement": "Security automation for cloud services",
                "confidence": "medium",
            }
        ],
        "fit_gaps": [],
    }
    baseline_snapshot = {
        "baseline_hash": "b" * 64,
        "bullets": [
            {
                "anchor_id": "baseline-strong",
                "bullet": "Built security automation for cloud services that reduced manual review by 45%.",
                "section": "Experience",
            }
        ],
    }

    with Session() as session:
        run = IntegrationRun(user="tester", mode="test")
        session.add(run)
        session.flush()
        job = Job(
            site="site",
            job_id="job-1",
            title="Security Engineer",
            desc="Security automation for cloud services",
            run_id=run.id,
            content_hash="job-hash",
            ai_match_percentage=90,
        )
        session.add(job)
        session.flush()
        session.add(
            JobApplicationPrep(
                job_pk=job.id,
                status="done",
                prep_json=dashboard.json.dumps(prep_payload),
                resume_hash="resume-hash",
                responsibilities_hash="",
                job_content_hash="job-hash",
                schema_version=dashboard.APPLICATION_PREP_SCHEMA_VERSION,
            )
        )
        session.add(
            ResumeSourceSettings(
                host_id="localhost",
                source_mode=RESUME_SOURCE_MODE_GOOGLE_DOC,
                google_document_id="doc-1",
                baseline_hash="b" * 64,
                baseline_snapshot_json=dashboard.json.dumps(baseline_snapshot),
            )
        )
        session.commit()
        job_pk = job.id

    result = dashboard.fetch_resume_swap_analysis(job_pk)

    assert result["analysis"]["baseline_candidates"] == []
    assert result["analysis"]["suggested_pairs"] == []


def test_dashboard_rejects_stale_resume_swap_analysis_hash(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(dashboard, "SessionLocal", Session)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", False)
    monkeypatch.setattr(dashboard, "_current_resume_hash", lambda: "resume-hash")
    monkeypatch.setenv("RESUME_VARIANT_HOST_ID", "localhost")

    prep_payload = {
        "draft_resume_bullets": [
            {
                "bullet": "Built security automation that reduced manual review time by 30%.",
                "evidence_sources": [{"source": "resume", "evidence": "Built security automation"}],
                "job_requirement": "Security automation",
                "confidence": "high",
            }
        ],
        "fit_gaps": [],
    }
    baseline_snapshot = {
        "baseline_hash": "b" * 64,
        "bullets": [
            {
                "anchor_id": "baseline-1",
                "bullet": "Maintained legacy reporting dashboards for internal teams.",
                "section": "Experience",
            }
        ],
    }

    with Session() as session:
        run = IntegrationRun(user="tester", mode="test")
        session.add(run)
        session.flush()
        job = Job(
            site="site",
            job_id="job-1",
            title="Security Engineer",
            desc="Build security automation",
            run_id=run.id,
            content_hash="job-hash",
            ai_match_percentage=90,
        )
        session.add(job)
        session.flush()
        session.add(
            JobApplicationPrep(
                job_pk=job.id,
                status="done",
                prep_json=dashboard.json.dumps(prep_payload),
                resume_hash="resume-hash",
                responsibilities_hash="",
                job_content_hash="job-hash",
                schema_version=dashboard.APPLICATION_PREP_SCHEMA_VERSION,
            )
        )
        session.add(
            ResumeSourceSettings(
                host_id="localhost",
                source_mode=RESUME_SOURCE_MODE_GOOGLE_DOC,
                google_document_id="doc-1",
                baseline_hash="b" * 64,
                baseline_snapshot_json=dashboard.json.dumps(baseline_snapshot),
            )
        )
        session.commit()
        job_pk = job.id

    try:
        dashboard.create_resume_variant_draft(
            job_pk,
            {
                "analysis_hash": "stale",
                "replacements": [
                    {
                        "anchor_id": "baseline-1",
                        "candidate_id": "draft-1",
                        "approved_bullet": "Built security automation that reduced manual review time by 30%.",
                        "edited": False,
                        "grounding_acknowledged": False,
                    }
                ],
            },
        )
    except ValueError as exc:
        assert "analysis changed" in str(exc)
    else:
        raise AssertionError("expected stale analysis hash guard")


def test_dashboard_requires_resume_swap_analysis_hash_before_saving(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(dashboard, "SessionLocal", Session)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", False)
    monkeypatch.setattr(dashboard, "_current_resume_hash", lambda: "resume-hash")
    monkeypatch.setenv("RESUME_VARIANT_HOST_ID", "localhost")

    prep_payload = {
        "draft_resume_bullets": [
            {
                "bullet": "Built security automation that reduced manual review time by 30%.",
                "evidence_sources": [{"source": "resume", "evidence": "Built security automation"}],
                "job_requirement": "Security automation",
                "confidence": "high",
            }
        ],
        "fit_gaps": [],
    }
    baseline_snapshot = {
        "baseline_hash": "b" * 64,
        "bullets": [
            {
                "anchor_id": "baseline-1",
                "bullet": "Maintained legacy reporting dashboards for internal teams.",
                "section": "Experience",
            }
        ],
    }

    with Session() as session:
        run = IntegrationRun(user="tester", mode="test")
        session.add(run)
        session.flush()
        job = Job(
            site="site",
            job_id="job-1",
            title="Security Engineer",
            desc="Build security automation",
            run_id=run.id,
            content_hash="job-hash",
            ai_match_percentage=90,
        )
        session.add(job)
        session.flush()
        session.add(
            JobApplicationPrep(
                job_pk=job.id,
                status="done",
                prep_json=dashboard.json.dumps(prep_payload),
                resume_hash="resume-hash",
                responsibilities_hash="",
                job_content_hash="job-hash",
                schema_version=dashboard.APPLICATION_PREP_SCHEMA_VERSION,
            )
        )
        session.add(
            ResumeSourceSettings(
                host_id="localhost",
                source_mode=RESUME_SOURCE_MODE_GOOGLE_DOC,
                google_document_id="doc-1",
                baseline_hash="b" * 64,
                baseline_snapshot_json=dashboard.json.dumps(baseline_snapshot),
            )
        )
        session.commit()
        job_pk = job.id

    try:
        dashboard.create_resume_variant_draft(
            job_pk,
            {
                "replacements": [
                    {
                        "anchor_id": "baseline-1",
                        "candidate_id": "draft-1",
                        "approved_bullet": "Built security automation that reduced manual review time by 30%.",
                        "edited": False,
                        "grounding_acknowledged": False,
                    }
                ],
            },
        )
    except ValueError as exc:
        assert "analysis_hash is required" in str(exc)
    else:
        raise AssertionError("expected required analysis hash guard")


def test_dashboard_swap_analysis_requires_current_application_prep(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(dashboard, "SessionLocal", Session)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", False)
    monkeypatch.setenv("RESUME_VARIANT_HOST_ID", "localhost")

    with Session() as session:
        run = IntegrationRun(user="tester", mode="test")
        session.add(run)
        session.flush()
        job = Job(site="site", job_id="job-1", title="Security Engineer", desc="Security automation", run_id=run.id)
        session.add(job)
        session.commit()
        job_pk = job.id

    try:
        dashboard.fetch_resume_swap_analysis(job_pk)
    except ValueError as exc:
        assert "Application Prep must be ready" in str(exc)
    else:
        raise AssertionError("expected current Application Prep guard")
