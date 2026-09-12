from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import dashboard
from app.google_resume import (
    GOOGLE_DOC_MIME_TYPE,
    GOOGLE_AUTH_MODE_SERVICE_ACCOUNT,
    GOOGLE_RESUME_SERVICE_ACCOUNT_SCOPES,
    GoogleResumeStore,
    build_baseline_snapshot,
    extract_google_doc_blocks,
    extract_google_doc_bullets,
    google_config_status,
    normalize_google_config,
)
from app.models import Base


def _google_doc_fixture() -> dict:
    return {
        "title": "Baseline Resume",
        "revisionId": "rev-1",
        "body": {
            "content": [
                {
                    "paragraph": {
                        "elements": [
                            {"startIndex": 1, "endIndex": 12, "textRun": {"content": "Experience\n"}}
                        ]
                    }
                },
                {
                    "paragraph": {
                        "bullet": {"listId": "list-1"},
                        "elements": [
                            {
                                "startIndex": 12,
                                "endIndex": 75,
                                "textRun": {"content": "Built security automation that reduced manual review time.\n"},
                            }
                        ],
                    }
                },
            ]
        },
    }


def _paragraph_element(text: str, start: int, bullet: bool = False) -> dict:
    paragraph = {
        "elements": [
            {
                "startIndex": start,
                "endIndex": start + len(text) + 1,
                "textRun": {"content": f"{text}\n"},
            }
        ]
    }
    if bullet:
        paragraph["bullet"] = {"listId": "list-1"}
    return {"paragraph": paragraph}


def _google_doc_with_table_fixture() -> dict:
    return {
        "title": "Baseline Resume",
        "revisionId": "rev-1",
        "body": {
            "content": [
                _paragraph_element("Joseph Gallegos", 1),
                {
                    "table": {
                        "tableRows": [
                            {
                                "tableCells": [
                                    {
                                        "content": [
                                            _paragraph_element("CERTIFICATIONS", 20),
                                            _paragraph_element("CompTIA Security+, CySA+", 40, bullet=True),
                                            _paragraph_element("AZ-900", 80, bullet=True),
                                        ]
                                    },
                                    {
                                        "content": [
                                            _paragraph_element("AWARDS AND ACCOLADES", 100),
                                            _paragraph_element("Hackathon Finalist, Wolters Kluwer - 2023 & 2024", 130, bullet=True),
                                        ]
                                    },
                                ]
                            }
                        ]
                    }
                },
                _paragraph_element("EXPERIENCE", 200),
                _paragraph_element("Wolters Kluwer - Governance, Risk and Compliance Houston, Texas", 220),
            ]
        },
    }


def test_google_doc_bullet_extraction_keeps_section_and_indices() -> None:
    bullets = extract_google_doc_bullets(_google_doc_fixture())

    assert len(bullets) == 1
    assert bullets[0]["section"] == "Experience"
    assert bullets[0]["bullet"] == "Built security automation that reduced manual review time."
    assert bullets[0]["start_index"] == 12
    assert bullets[0]["end_index"] == 75
    assert bullets[0]["text_start_index"] == 12
    assert bullets[0]["text_end_index"] == 74


def test_google_doc_block_extraction_keeps_paragraphs_and_bullets_in_order() -> None:
    blocks = extract_google_doc_blocks(_google_doc_fixture())

    assert [block["type"] for block in blocks] == ["paragraph", "bullet"]
    assert blocks[0]["text"] == "Experience"
    assert blocks[1]["text"] == "Built security automation that reduced manual review time."


def test_google_doc_block_extraction_preserves_paragraph_and_run_styles() -> None:
    document = {
        "body": {
            "content": [
                {
                    "paragraph": {
                        "paragraphStyle": {"namedStyleType": "HEADING_2"},
                        "elements": [
                            {
                                "textRun": {
                                    "content": "IT Cybersecurity Analyst\n",
                                    "textStyle": {
                                        "fontSize": {"magnitude": 9, "unit": "PT"},
                                        "weightedFontFamily": {
                                            "fontFamily": "Arial",
                                            "weight": 400,
                                        },
                                    },
                                }
                            }
                        ],
                    }
                }
            ]
        }
    }

    blocks = extract_google_doc_blocks(document)

    assert blocks[0]["paragraph_style"]["namedStyleType"] == "HEADING_2"
    assert blocks[0]["text_runs"] == [
        {
            "text": "IT Cybersecurity Analyst",
            "text_style": {
                "fontSize": {"magnitude": 9, "unit": "PT"},
                "weightedFontFamily": {"fontFamily": "Arial", "weight": 400},
            },
        }
    ]


def test_google_doc_block_extraction_preserves_table_sections() -> None:
    blocks = extract_google_doc_blocks(_google_doc_with_table_fixture())

    assert [block["type"] for block in blocks] == ["paragraph", "table", "paragraph", "paragraph"]
    table = blocks[1]
    left_cell = table["rows"][0][0]["blocks"]
    right_cell = table["rows"][0][1]["blocks"]
    assert left_cell[0]["text"] == "CERTIFICATIONS"
    assert left_cell[1]["text"] == "CompTIA Security+, CySA+"
    assert right_cell[0]["text"] == "AWARDS AND ACCOLADES"
    assert right_cell[1]["text"].startswith("Hackathon Finalist")


def test_google_doc_text_end_index_preserves_newline_with_utf16_offsets() -> None:
    doc = _google_doc_fixture()
    doc["body"]["content"][1]["paragraph"]["elements"][0] = {
        "startIndex": 12,
        "endIndex": 27,
        "textRun": {"content": "Built 🚀 auth\n"},
    }

    bullets = extract_google_doc_bullets(doc)

    assert bullets[0]["bullet"] == "Built 🚀 auth"
    assert bullets[0]["text_start_index"] == 12
    assert bullets[0]["text_end_index"] == 26


def test_baseline_snapshot_hashes_doc_revision_and_bullets() -> None:
    metadata = {
        "id": "doc-1",
        "name": "Baseline Resume",
        "mimeType": GOOGLE_DOC_MIME_TYPE,
        "modifiedTime": "2026-09-11T12:00:00Z",
    }

    snapshot = build_baseline_snapshot(metadata, _google_doc_fixture())

    assert snapshot["document_id"] == "doc-1"
    assert snapshot["revision"] == "rev-1"
    assert snapshot["baseline_hash"]
    assert len(snapshot["bullets"]) == 1


def test_baseline_snapshot_keeps_google_page_named_and_list_styles() -> None:
    document = _google_doc_fixture()
    document["documentStyle"] = {
        "marginLeft": {"magnitude": 43.2, "unit": "PT"},
        "pageSize": {
            "width": {"magnitude": 612, "unit": "PT"},
            "height": {"magnitude": 792, "unit": "PT"},
        },
    }
    document["namedStyles"] = {
        "styles": [
            {
                "namedStyleType": "HEADING_2",
                "textStyle": {"bold": True},
            }
        ]
    }
    document["lists"] = {"list-1": {"listProperties": {"nestingLevels": []}}}

    snapshot = build_baseline_snapshot(
        {"id": "doc-1", "name": "Baseline Resume"}, document
    )

    assert snapshot["document_style"]["marginLeft"]["magnitude"] == 43.2
    assert snapshot["named_styles"]["styles"][0]["textStyle"]["bold"] is True
    assert "list-1" in snapshot["lists"]


def test_style_capture_keeps_pre_style_render_hash_compatible() -> None:
    metadata = {"id": "doc-1", "name": "Baseline Resume"}
    unstyled_document = _google_doc_fixture()
    styled_document = _google_doc_fixture()
    styled_document["documentStyle"] = {
        "marginLeft": {"magnitude": 43.2, "unit": "PT"}
    }
    styled_document["namedStyles"] = {
        "styles": [
            {"namedStyleType": "NORMAL_TEXT", "textStyle": {"bold": False}}
        ]
    }
    styled_document["body"]["content"][0]["paragraph"]["paragraphStyle"] = {
        "namedStyleType": "HEADING_1"
    }
    styled_document["body"]["content"][0]["paragraph"]["elements"][0][
        "textRun"
    ]["textStyle"] = {"bold": True}

    unstyled = build_baseline_snapshot(metadata, unstyled_document)
    styled = build_baseline_snapshot(metadata, styled_document)

    assert unstyled["legacy_render_hash"] == styled["legacy_render_hash"]
    assert unstyled["baseline_hash"] != styled["baseline_hash"]


def test_baseline_snapshot_preserves_footer_blocks() -> None:
    document = _google_doc_fixture()
    document["documentStyle"] = {
        "useFirstPageHeaderFooter": True,
        "firstPageFooterId": "footer-1",
    }
    document["footers"] = {
        "footer-1": {
            "content": [
                _paragraph_element("Experience continued on the next page..", 90)
            ]
        }
    }

    snapshot = build_baseline_snapshot(
        {"id": "doc-1", "name": "Baseline Resume"}, document
    )

    assert snapshot["footers"]["footer-1"][0]["text"] == (
        "Experience continued on the next page.."
    )


def test_google_config_rejects_oauth_client_json() -> None:
    with pytest.raises(Exception, match="Service account JSON"):
        normalize_google_config({"client_config": {"installed": {}}})


def test_google_config_accepts_service_account_json() -> None:
    config = normalize_google_config(
        {
            "auth_mode": "service_account",
            "service_account_config": {
                "type": "service_account",
                "client_email": "resume-bot@example.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                "token_uri": "https://oauth2.googleapis.com/token",
            },
        }
    )

    assert config["auth_mode"] == GOOGLE_AUTH_MODE_SERVICE_ACCOUNT
    assert config["service_account_email"] == "resume-bot@example.iam.gserviceaccount.com"


def test_google_config_rejects_non_service_account_json() -> None:
    with pytest.raises(Exception, match="type service_account"):
        normalize_google_config(
            {
                "auth_mode": "service_account",
                "service_account_config": {"type": "authorized_user"},
            }
        )


def test_service_account_config_counts_as_connected(monkeypatch, tmp_path) -> None:
    store = GoogleResumeStore(tmp_path)
    monkeypatch.setenv("GOOGLE_RESUME_ENCRYPTION_KEY", "test-secret")
    store.save_config(
        normalize_google_config(
            {
                "auth_mode": "service_account",
                "service_account_config": {
                    "type": "service_account",
                    "client_email": "resume-bot@example.iam.gserviceaccount.com",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                    "token_uri": "https://oauth2.googleapis.com/token",
                },
            }
        )
    )

    status = google_config_status(store)

    assert status["configured"] is True
    assert status["connected"] is True
    assert status["auth_mode"] == "service_account"
    assert status["service_account_email"] == "resume-bot@example.iam.gserviceaccount.com"
    assert status["scopes"] == GOOGLE_RESUME_SERVICE_ACCOUNT_SCOPES


def test_service_account_disconnect_clears_saved_key(monkeypatch, tmp_path) -> None:
    store = GoogleResumeStore(tmp_path)
    monkeypatch.setattr(dashboard, "_google_resume_store", lambda: store)
    monkeypatch.setenv("GOOGLE_RESUME_ENCRYPTION_KEY", "test-secret")
    store.save_config(
        normalize_google_config(
            {
                "auth_mode": "service_account",
                "service_account_config": {
                    "type": "service_account",
                    "client_email": "resume-bot@example.iam.gserviceaccount.com",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                    "token_uri": "https://oauth2.googleapis.com/token",
                },
            }
        )
    )

    with dashboard.app.test_request_context("/api/resume-source/google/disconnect", base_url="http://localhost:5000"):
        result = dashboard.disconnect_google_resume()

    assert result["configured"] is False
    assert result["connected"] is False
    assert not store.config_path.exists()


class _FakeGoogleResumeClient:
    def document_snapshot(self, document_id: str) -> dict:
        assert document_id == "doc-1"
        metadata = {
            "id": "doc-1",
            "name": "Baseline Resume",
            "mimeType": GOOGLE_DOC_MIME_TYPE,
            "webViewLink": "https://docs.google.com/document/d/doc-1/edit",
            "parents": ["folder-1"],
            "modifiedTime": "2026-09-11T12:00:00Z",
        }
        return {"metadata": metadata, "snapshot": build_baseline_snapshot(metadata, _google_doc_fixture())}


class _EmptyGoogleResumeClient:
    def document_snapshot(self, document_id: str) -> dict:
        metadata = {
            "id": document_id,
            "name": "Empty Resume",
            "mimeType": GOOGLE_DOC_MIME_TYPE,
            "webViewLink": "https://docs.google.com/document/d/doc-1/edit",
            "modifiedTime": "2026-09-11T12:00:00Z",
        }
        document = {"title": "Empty Resume", "revisionId": "rev-1", "body": {"content": []}}
        return {"metadata": metadata, "snapshot": build_baseline_snapshot(metadata, document)}


def test_dashboard_syncs_google_baseline_snapshot_with_fake_client(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(dashboard, "SessionLocal", Session)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", False)
    monkeypatch.setattr(dashboard, "_google_resume_client", lambda: _FakeGoogleResumeClient())
    monkeypatch.setenv("RESUME_VARIANT_HOST_ID", "localhost")

    with dashboard.app.test_request_context("/api/resume-source/google/select", base_url="http://localhost:5000"):
        result = dashboard.sync_google_resume_source("doc-1")

    assert result["source_mode"] == "google_doc"
    assert result["can_apply"] is True
    assert result["baseline_bullet_count"] == 1
    assert result["google_document_name"] == "Baseline Resume"

    with Session() as session:
        settings = dashboard._get_or_create_resume_source_settings(session)
        snapshot = json.loads(settings.baseline_snapshot_json)

    assert settings.google_folder_id == "folder-1"
    assert snapshot["bullets"][0]["section"] == "Experience"


def test_dashboard_rejects_google_baseline_without_body_list_bullets(monkeypatch) -> None:
    test_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(test_engine)
    Session = sessionmaker(bind=test_engine, expire_on_commit=False)
    monkeypatch.setattr(dashboard, "SessionLocal", Session)
    monkeypatch.setattr(dashboard, "_REFERENCE_FIELDS_COLUMN_READY", False)
    monkeypatch.setattr(dashboard, "_google_resume_client", lambda: _EmptyGoogleResumeClient())
    monkeypatch.setenv("RESUME_VARIANT_HOST_ID", "localhost")

    with dashboard.app.test_request_context("/api/resume-source/google/select", base_url="http://localhost:5000"):
        with pytest.raises(Exception, match="ordinary body-list resume bullets"):
            dashboard.sync_google_resume_source("doc-1")


def test_google_resume_setup_is_localhost_only() -> None:
    with dashboard.app.test_client() as client:
        response = client.put(
            "/api/resume-source/google/config",
            json={
                "service_account_config": {
                    "type": "service_account",
                    "client_email": "resume-bot@example.iam.gserviceaccount.com",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            },
            base_url="http://fruitsalad:5000",
        )

    assert response.status_code == 400
    assert "localhost" in response.get_json()["error"]


def test_saving_google_config_replaces_old_service_account_key(monkeypatch, tmp_path) -> None:
    store = GoogleResumeStore(tmp_path)
    monkeypatch.setattr(dashboard, "_google_resume_store", lambda: store)
    monkeypatch.setenv("GOOGLE_RESUME_ENCRYPTION_KEY", "test-secret")

    store.save_config(
        normalize_google_config(
            {
                "service_account_config": {
                    "type": "service_account",
                    "client_email": "old@example.iam.gserviceaccount.com",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )

    with dashboard.app.test_request_context("/api/resume-source/google/config", base_url="http://localhost:5000"):
        result = dashboard.save_google_resume_config(
            {
                "service_account_config": {
                    "type": "service_account",
                    "client_email": "new@example.iam.gserviceaccount.com",
                    "private_key": "-----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY-----\n",
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )

    assert result["configured"] is True
    assert result["connected"] is True
    assert result["service_account_email"] == "new@example.iam.gserviceaccount.com"
