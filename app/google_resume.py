from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .db import utc_now_naive


# The service account only needs read scopes. PDF creation is local so personal
# Gmail accounts do not hit service-account Drive ownership/quota limits.
GOOGLE_RESUME_SERVICE_ACCOUNT_SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/documents.readonly",
]
GOOGLE_RESUME_SCOPES = GOOGLE_RESUME_SERVICE_ACCOUNT_SCOPES
GOOGLE_AUTH_MODE_SERVICE_ACCOUNT = "service_account"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
GOOGLE_RESUME_DATA_DIR = Path(__file__).resolve().parents[1] / "data" / "google_resume"


class GoogleResumeError(RuntimeError):
    """User-facing setup or Google API failure for resume source operations."""


class GoogleResumeDependencyError(GoogleResumeError):
    """Raised when Google integration packages are not installed."""


def _google_imports() -> Dict[str, Any]:
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover - exercised when deps absent in runtime
        raise GoogleResumeDependencyError(
            "Google resume integration packages are not installed. Run pip install -r requirements.txt."
        ) from exc
    return {
        "ServiceAccountCredentials": service_account.Credentials,
        "build": build,
    }


def _crypto_imports() -> Dict[str, Any]:
    try:
        from cryptography.fernet import Fernet, InvalidToken
    except ImportError as exc:  # pragma: no cover - exercised when deps absent in runtime
        raise GoogleResumeDependencyError(
            "The cryptography package is not installed. Run pip install -r requirements.txt."
        ) from exc
    return {"Fernet": Fernet, "InvalidToken": InvalidToken}


def _encryption_secret() -> str:
    value = os.getenv("GOOGLE_RESUME_ENCRYPTION_KEY") or os.getenv("DASHBOARD_SECRET_KEY")
    if not value:
        raise GoogleResumeError(
            "Set GOOGLE_RESUME_ENCRYPTION_KEY or DASHBOARD_SECRET_KEY before saving Google resume credentials."
        )
    return value


def _fernet_key(secret: str) -> bytes:
    # Fernet needs a urlsafe base64-encoded 32-byte key; derive one from the
    # configured secret so users do not have to hand-craft a Fernet value.
    return base64.urlsafe_b64encode(hashlib.sha256(secret.encode("utf-8")).digest())


class GoogleResumeStore:
    """Encrypted local file store for the Google service-account key."""

    def __init__(self, data_dir: Path | str = GOOGLE_RESUME_DATA_DIR) -> None:
        self.data_dir = Path(data_dir)

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.enc"

    def _fernet(self) -> Any:
        imports = _crypto_imports()
        return imports["Fernet"](_fernet_key(_encryption_secret()))

    def _read(self, path: Path) -> Optional[Dict[str, Any]]:
        if not path.exists():
            return None
        imports = _crypto_imports()
        try:
            plaintext = self._fernet().decrypt(path.read_bytes())
            payload = json.loads(plaintext.decode("utf-8"))
        except imports["InvalidToken"] as exc:
            raise GoogleResumeError("Could not decrypt stored Google resume credentials.") from exc
        except Exception as exc:
            raise GoogleResumeError("Stored Google resume credentials are not readable.") from exc
        return payload if isinstance(payload, dict) else None

    def _write(self, path: Path, payload: Dict[str, Any]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        path.write_bytes(self._fernet().encrypt(body))

    def save_config(self, payload: Dict[str, Any]) -> None:
        self._write(self.config_path, payload)

    def load_config(self) -> Optional[Dict[str, Any]]:
        return self._read(self.config_path)

    def clear_config(self) -> None:
        if self.config_path.exists():
            self.config_path.unlink()

    def configured(self) -> bool:
        return self.config_path.exists()


def normalize_service_account_config(value: Any) -> Dict[str, Any]:
    """Accept a downloaded Google service-account key as object or string."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise GoogleResumeError("Service account JSON is not valid JSON.") from exc
    if not isinstance(value, dict):
        raise GoogleResumeError("Service account JSON must be an object.")
    if value.get("type") != "service_account":
        raise GoogleResumeError("Service account JSON must have type service_account.")
    if not value.get("client_email") or not value.get("private_key"):
        raise GoogleResumeError("Service account JSON must include client_email and private_key.")
    if not value.get("token_uri"):
        raise GoogleResumeError("Service account JSON must include token_uri.")
    return value


def normalize_google_config(data: Dict[str, Any]) -> Dict[str, Any]:
    service_account_config = data.get("service_account_config")
    service_account_info = normalize_service_account_config(service_account_config)
    return {
        "auth_mode": GOOGLE_AUTH_MODE_SERVICE_ACCOUNT,
        "service_account_config": service_account_info,
        "service_account_email": str(service_account_info.get("client_email") or ""),
        "configured_at": utc_now_naive().isoformat(timespec="seconds") + "Z",
    }


def google_config_status(store: GoogleResumeStore) -> Dict[str, Any]:
    config = store.load_config() if store.configured() else {}
    return {
        "configured": bool(config),
        "connected": bool(config),
        "auth_mode": GOOGLE_AUTH_MODE_SERVICE_ACCOUNT if config else "",
        "service_account_email": str((config or {}).get("service_account_email") or ""),
        "scopes": GOOGLE_RESUME_SERVICE_ACCOUNT_SCOPES,
    }


def extract_google_doc_bullets(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract ordinary body list paragraphs as stable-ish review anchors."""
    return [
        block
        for block in extract_google_doc_blocks(document)
        if block.get("type") == "bullet"
    ]


def extract_google_doc_blocks(document: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract readable body paragraphs and bullets in document order."""
    body = document.get("body") if isinstance(document, dict) else {}
    content = body.get("content") if isinstance(body, dict) else []
    if not isinstance(content, list):
        return []

    blocks, _current_section, _bullet_index = _extract_blocks_from_content(content, "", 0)
    return blocks


def _extract_structural_blocks(document: Dict[str, Any], key: str) -> Dict[str, List[Dict[str, Any]]]:
    """Extract header/footer content using the same style-aware block shape."""
    structures = document.get(key) if isinstance(document, dict) else {}
    if not isinstance(structures, dict):
        return {}
    extracted: Dict[str, List[Dict[str, Any]]] = {}
    for structure_id, structure in structures.items():
        content = structure.get("content") if isinstance(structure, dict) else []
        if not isinstance(content, list):
            continue
        blocks, _section, _bullet_index = _extract_blocks_from_content(content, "", 0)
        extracted[str(structure_id)] = blocks
    return extracted


def _extract_blocks_from_content(
    content: List[Dict[str, Any]],
    current_section: str,
    bullet_index: int,
) -> Tuple[List[Dict[str, Any]], str, int]:
    """Walk Google Docs content while preserving top-level tables for rendering."""
    blocks: List[Dict[str, Any]] = []
    for element in content:
        if not isinstance(element, dict):
            continue

        table = element.get("table")
        if isinstance(table, dict):
            table_block, bullet_index = _table_block(table, current_section, bullet_index)
            if table_block:
                blocks.append(table_block)
            continue

        paragraph = element.get("paragraph")
        if not isinstance(paragraph, dict):
            continue
        text = _paragraph_text(paragraph)
        if not text:
            continue
        if isinstance(paragraph.get("bullet"), dict):
            start_index, end_index = _paragraph_indices(paragraph, element)
            text_start_index, text_end_index = _replacement_text_indices(
                paragraph, start_index, end_index
            )
            anchor_basis = f"{bullet_index}|{current_section}|{text}|{start_index}|{end_index}"
            bullet_index += 1
            blocks.append(
                {
                    "type": "bullet",
                    "anchor_id": hashlib.sha256(anchor_basis.encode("utf-8")).hexdigest()[:16],
                    "bullet": text,
                    "text": text,
                    "section": current_section,
                    "start_index": start_index,
                    "end_index": end_index,
                    "text_start_index": text_start_index,
                    "text_end_index": text_end_index,
                    "text_hash": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    # Keep the Google Docs formatting inputs alongside the
                    # replacement indices. The PDF renderer can then resolve
                    # inherited named styles instead of guessing from text.
                    "paragraph_style": dict(paragraph.get("paragraphStyle") or {}),
                    "bullet_style": dict(paragraph.get("bullet") or {}),
                    "text_runs": _paragraph_runs(paragraph),
                }
            )
        else:
            current_section = text[:200]
            blocks.append(
                {
                    "type": "paragraph",
                    "text": text,
                    "section": current_section,
                    "paragraph_style": dict(paragraph.get("paragraphStyle") or {}),
                    "text_runs": _paragraph_runs(paragraph),
                }
            )
    return blocks, current_section, bullet_index


def _table_block(
    table: Dict[str, Any],
    current_section: str,
    bullet_index: int,
) -> Tuple[Optional[Dict[str, Any]], int]:
    rows: List[List[Dict[str, Any]]] = []
    row_styles: List[Dict[str, Any]] = []
    for row in table.get("tableRows") or []:
        if not isinstance(row, dict):
            continue
        rendered_cells: List[Dict[str, Any]] = []
        for cell in row.get("tableCells") or []:
            if not isinstance(cell, dict):
                continue
            cell_content = cell.get("content") if isinstance(cell.get("content"), list) else []
            cell_blocks, _cell_section, bullet_index = _extract_blocks_from_content(
                cell_content,
                current_section,
                bullet_index,
            )
            rendered_cells.append(
                {
                    "blocks": cell_blocks,
                    "cell_style": dict(cell.get("tableCellStyle") or {}),
                }
            )
        if rendered_cells:
            rows.append(rendered_cells)
            row_styles.append(dict(row.get("tableRowStyle") or {}))
    if not rows:
        return None, bullet_index
    return {
        "type": "table",
        "rows": rows,
        "row_styles": row_styles,
        "table_style": dict(table.get("tableStyle") or {}),
        # Column widths live inside tableStyle in the Docs API.
        "column_properties": list(
            (table.get("tableStyle") or {}).get("tableColumnProperties") or []
        ),
        "section": current_section,
    }, bullet_index


def _paragraph_text(paragraph: Dict[str, Any]) -> str:
    parts: List[str] = []
    for element in paragraph.get("elements") or []:
        if not isinstance(element, dict):
            continue
        text_run = element.get("textRun")
        content = text_run.get("content") if isinstance(text_run, dict) else ""
        if content:
            parts.append(str(content))
    return "".join(parts).strip()


def _paragraph_runs(paragraph: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Keep run boundaries because bold/italic/links can change mid-line."""
    runs: List[Dict[str, Any]] = []
    for element in paragraph.get("elements") or []:
        if not isinstance(element, dict):
            continue
        text_run = element.get("textRun")
        if not isinstance(text_run, dict):
            continue
        content = str(text_run.get("content") or "").rstrip("\n")
        if not content:
            continue
        runs.append(
            {
                "text": content,
                "text_style": dict(text_run.get("textStyle") or {}),
            }
        )
    return runs


def _paragraph_indices(paragraph: Dict[str, Any], element: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    starts: List[int] = []
    ends: List[int] = []
    for part in paragraph.get("elements") or []:
        if not isinstance(part, dict):
            continue
        if isinstance(part.get("startIndex"), int):
            starts.append(part["startIndex"])
        if isinstance(part.get("endIndex"), int):
            ends.append(part["endIndex"])
    if not starts and isinstance(element.get("startIndex"), int):
        starts.append(element["startIndex"])
    if not ends and isinstance(element.get("endIndex"), int):
        ends.append(element["endIndex"])
    return (min(starts) if starts else None, max(ends) if ends else None)


def _utf16_length(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _replacement_text_indices(
    paragraph: Dict[str, Any], start_index: Optional[int], end_index: Optional[int]
) -> Tuple[Optional[int], Optional[int]]:
    """Return bounds for replacing paragraph text while leaving the newline."""
    if start_index is None or end_index is None:
        return None, None
    raw_text = "".join(
        str((part.get("textRun") or {}).get("content") or "")
        for part in (paragraph.get("elements") or [])
        if isinstance(part, dict)
    )
    trailing_newline_units = _utf16_length(raw_text) - _utf16_length(raw_text.rstrip("\n"))
    text_end_index = max(start_index, end_index - trailing_newline_units)
    return start_index, text_end_index


def _legacy_render_blocks(blocks: Any) -> List[Dict[str, Any]]:
    """Rebuild the pre-style block shape for saved-baseline compatibility."""
    if not isinstance(blocks, list):
        return []
    legacy: List[Dict[str, Any]] = []
    style_keys = {
        "paragraph_style",
        "bullet_style",
        "text_runs",
        "row_styles",
        "table_style",
        "column_properties",
        "cell_style",
    }
    for block in blocks:
        if not isinstance(block, dict):
            continue
        copied = {key: value for key, value in block.items() if key not in style_keys}
        if copied.get("type") == "table":
            rows: List[List[Dict[str, Any]]] = []
            for row in block.get("rows") or []:
                if not isinstance(row, list):
                    continue
                cells: List[Dict[str, Any]] = []
                for cell in row:
                    if not isinstance(cell, dict):
                        continue
                    cells.append({"blocks": _legacy_render_blocks(cell.get("blocks"))})
                if cells:
                    rows.append(cells)
            copied["rows"] = rows
        legacy.append(copied)
    return legacy


def build_baseline_snapshot(metadata: Dict[str, Any], document: Dict[str, Any]) -> Dict[str, Any]:
    blocks = extract_google_doc_blocks(document)
    bullets = [block for block in blocks if block.get("type") == "bullet"]
    revision = str(document.get("revisionId") or metadata.get("headRevisionId") or metadata.get("modifiedTime") or "")
    title = str(document.get("title") or metadata.get("name") or "")
    snapshot = {
        "document_id": str(metadata.get("id") or ""),
        "title": title,
        "revision": revision,
        "modified_time": str(metadata.get("modifiedTime") or ""),
        # These are the style sheets and page tokens used by Google Docs.
        # Keeping the raw API values makes the snapshot forward-compatible
        # with renderer improvements without another schema migration.
        "document_style": dict(document.get("documentStyle") or {}),
        "named_styles": dict(document.get("namedStyles") or {}),
        "lists": dict(document.get("lists") or {}),
        "headers": _extract_structural_blocks(document, "headers"),
        "footers": _extract_structural_blocks(document, "footers"),
        "blocks": blocks,
        "bullets": bullets,
    }
    legacy_bullets = _legacy_render_blocks(snapshot["bullets"])
    legacy_hash_basis = {
        "document_id": snapshot["document_id"],
        "title": snapshot["title"],
        "revision": snapshot["revision"],
        "modified_time": snapshot["modified_time"],
        "bullets": legacy_bullets,
    }
    # Keep a compatibility hash so older synced snapshots can still pass the
    # freshness check until the user re-syncs with full render blocks.
    snapshot["legacy_bullet_hash"] = hashlib.sha256(
        json.dumps(legacy_hash_basis, ensure_ascii=False, sort_keys=True).encode("utf-8", "ignore")
    ).hexdigest()
    legacy_render_basis = dict(legacy_hash_basis)
    legacy_render_basis["blocks"] = _legacy_render_blocks(snapshot["blocks"])
    snapshot["legacy_render_hash"] = hashlib.sha256(
        json.dumps(legacy_render_basis, ensure_ascii=False, sort_keys=True).encode("utf-8", "ignore")
    ).hexdigest()
    hash_basis = dict(legacy_hash_basis)
    hash_basis["blocks"] = snapshot["blocks"]
    hash_basis["document_style"] = snapshot["document_style"]
    hash_basis["named_styles"] = snapshot["named_styles"]
    hash_basis["lists"] = snapshot["lists"]
    hash_basis["headers"] = snapshot["headers"]
    hash_basis["footers"] = snapshot["footers"]
    snapshot["baseline_hash"] = hashlib.sha256(
        json.dumps(hash_basis, ensure_ascii=False, sort_keys=True).encode("utf-8", "ignore")
    ).hexdigest()
    return snapshot


class GoogleResumeClient:
    """Small Google API wrapper for service-account resume operations."""

    def __init__(self, store: GoogleResumeStore) -> None:
        self.store = store

    def _config(self) -> Dict[str, Any]:
        config = self.store.load_config()
        if not config:
            raise GoogleResumeError("Google resume credentials are not configured.")
        return config

    def credentials(self) -> Any:
        imports = _google_imports()
        config = self._config()
        service_account_info = config.get("service_account_config")
        if not isinstance(service_account_info, dict):
            raise GoogleResumeError("Stored service account credentials are not readable.")
        return imports["ServiceAccountCredentials"].from_service_account_info(
            service_account_info,
            scopes=GOOGLE_RESUME_SERVICE_ACCOUNT_SCOPES,
        )

    def _service(self, name: str, version: str) -> Any:
        imports = _google_imports()
        return imports["build"](name, version, credentials=self.credentials(), cache_discovery=False)

    def document_snapshot(self, document_id: str) -> Dict[str, Any]:
        document_id = str(document_id or "").strip()
        if not document_id:
            raise GoogleResumeError("document_id is required.")
        try:
            drive = self._service("drive", "v3")
            metadata = (
                drive.files()
                .get(
                    fileId=document_id,
                    fields="id,name,mimeType,webViewLink,parents,modifiedTime,headRevisionId",
                    supportsAllDrives=True,
                )
                .execute()
            )
            if metadata.get("mimeType") != GOOGLE_DOC_MIME_TYPE:
                raise GoogleResumeError("Selected baseline must be a Google Docs document.")
            docs = self._service("docs", "v1")
            document = docs.documents().get(documentId=document_id).execute()
        except GoogleResumeError:
            raise
        except Exception as exc:
            raise GoogleResumeError(f"Could not read selected Google Docs baseline: {exc}") from exc

        snapshot = build_baseline_snapshot(metadata, document)
        return {
            "metadata": metadata,
            "snapshot": snapshot,
        }
