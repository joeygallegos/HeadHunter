from __future__ import annotations

import html
import re
from io import BytesIO
from pathlib import Path
from typing import Any, Dict, List, Tuple


_ARIAL_REGISTERED = False


class ResumePdfError(ValueError):
    """Raised when a synced resume snapshot cannot be rendered safely."""


def render_resume_pdf_bytes(baseline_snapshot: Dict[str, Any], replacements: List[Dict[str, Any]]) -> bytes:
    """Render a local PDF from synced Google Docs content and approved swaps."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:  # pragma: no cover - exercised only in incomplete envs
        raise ResumePdfError("reportlab is required to render local resume PDFs.") from exc

    blocks = resolve_resume_blocks(baseline_snapshot, replacements)
    if not blocks:
        raise ResumePdfError("Synced baseline snapshot does not contain renderable resume content.")

    buffer = BytesIO()
    _register_resume_fonts(pdfmetrics, TTFont)
    page_size, page_margins = _document_geometry(baseline_snapshot, LETTER, inch)
    doc = SimpleDocTemplate(
        buffer,
        pagesize=page_size,
        leftMargin=page_margins[0],
        rightMargin=page_margins[1],
        topMargin=page_margins[2],
        bottomMargin=page_margins[3],
        title=str(baseline_snapshot.get("title") or "Tailored Resume"),
    )

    base = getSampleStyleSheet()
    styles = {
        "name": ParagraphStyle(
            "ResumeName",
            parent=base["Title"],
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=18,
            alignment=1,
            spaceAfter=4,
            textColor=colors.HexColor("#0f172a"),
        ),
        "contact": ParagraphStyle(
            "ResumeContact",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8.5,
            leading=10.5,
            alignment=1,
            spaceAfter=5,
            textColor=colors.HexColor("#334155"),
        ),
        "section": ParagraphStyle(
            "ResumeSection",
            parent=base["Heading2"],
            fontName="Helvetica-Bold",
            fontSize=10.5,
            leading=12,
            spaceBefore=7,
            spaceAfter=3,
            borderPadding=(0, 0, 2, 0),
            borderColor=colors.HexColor("#cbd5e1"),
            borderWidth=0,
            textColor=colors.HexColor("#0f172a"),
        ),
        "body": ParagraphStyle(
            "ResumeBody",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=11,
            spaceAfter=2,
            textColor=colors.HexColor("#111827"),
        ),
        "bullet": ParagraphStyle(
            "ResumeBullet",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8.8,
            leading=10.8,
            leftIndent=13,
            firstLineIndent=-7,
            spaceAfter=2,
            textColor=colors.HexColor("#111827"),
        ),
        "table_heading": ParagraphStyle(
            "ResumeTableHeading",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=10.5,
            spaceAfter=2,
            textColor=colors.HexColor("#111827"),
        ),
        "table_heading_right": ParagraphStyle(
            "ResumeTableHeadingRight",
            parent=base["BodyText"],
            fontName="Helvetica-Bold",
            fontSize=8.5,
            leading=10.5,
            alignment=2,
            spaceAfter=2,
            textColor=colors.HexColor("#111827"),
        ),
        "table_body": ParagraphStyle(
            "ResumeTableBody",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8.3,
            leading=10.2,
            spaceAfter=1,
            textColor=colors.HexColor("#111827"),
        ),
        "table_bullet": ParagraphStyle(
            "ResumeTableBullet",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=8.2,
            leading=10,
            leftIndent=10,
            firstLineIndent=-6,
            spaceAfter=1,
            textColor=colors.HexColor("#111827"),
        ),
        "body_right": ParagraphStyle(
            "ResumeBodyRight",
            parent=base["BodyText"],
            fontName="Helvetica",
            fontSize=9,
            leading=11,
            alignment=2,
            spaceAfter=2,
            textColor=colors.HexColor("#111827"),
        ),
    }

    story: List[Any] = []
    paragraph_count = 0
    for block in blocks:
        if block.get("type") == "table":
            table = _table_flowable(
                block,
                styles,
                doc.width,
                Paragraph,
                Table,
                TableStyle,
                colors,
                baseline_snapshot,
            )
            if table:
                story.append(table)
                story.append(Spacer(1, 5))
            continue

        plain_text = str(block.get("text") or block.get("bullet") or "").strip()
        if not plain_text:
            continue
        text = _styled_block_text(block, baseline_snapshot, colors)
        if block.get("type") == "bullet":
            style = _source_paragraph_style(
                block, baseline_snapshot, styles["bullet"], colors
            )
            bullet_text = _source_bullet_text(block, baseline_snapshot)
            story.append(Paragraph(text, style, bulletText=bullet_text))
            continue

        split_line = _split_resume_detail_line(plain_text)
        if split_line:
            left, right, emphasis = split_line
            source_style = _source_paragraph_style(
                block,
                baseline_snapshot,
                styles["table_heading"] if emphasis else styles["body"],
                colors,
            )
            story.append(
                _two_column_line(
                    left,
                    right,
                    styles,
                    doc.width,
                    Paragraph,
                    Table,
                    TableStyle,
                    colors,
                    emphasis,
                    source_style=source_style,
                )
            )
            paragraph_count += 1
            continue

        style_name = _paragraph_style_name(plain_text, paragraph_count)
        if story and style_name == "section":
            story.append(Spacer(1, 2))
        style = _source_paragraph_style(
            block, baseline_snapshot, styles[style_name], colors
        )
        story.append(Paragraph(text, style))
        paragraph_count += 1

    doc.build(
        story,
        onFirstPage=_page_decoration_callback(
            baseline_snapshot, styles, Paragraph, colors, first_page=True
        ),
        onLaterPages=_page_decoration_callback(
            baseline_snapshot, styles, Paragraph, colors, first_page=False
        ),
    )
    return buffer.getvalue()


def _register_resume_fonts(pdfmetrics: Any, TTFont: Any) -> None:
    """Register the Arial family used by the source resume when available."""
    global _ARIAL_REGISTERED
    font_dir = Path("C:/Windows/Fonts")
    candidates = {
        "Arial": font_dir / "arial.ttf",
        "Arial-Bold": font_dir / "arialbd.ttf",
        "Arial-Italic": font_dir / "ariali.ttf",
        "Arial-BoldItalic": font_dir / "arialbi.ttf",
    }
    registered = set(pdfmetrics.getRegisteredFontNames())
    for name, path in candidates.items():
        if name not in registered and path.exists():
            pdfmetrics.registerFont(TTFont(name, str(path)))
    if all(name in pdfmetrics.getRegisteredFontNames() for name in candidates):
        pdfmetrics.registerFontFamily(
            "Arial",
            normal="Arial",
            bold="Arial-Bold",
            italic="Arial-Italic",
            boldItalic="Arial-BoldItalic",
        )
        _ARIAL_REGISTERED = True


def _document_geometry(
    baseline_snapshot: Dict[str, Any],
    default_page_size: Tuple[float, float],
    inch: float,
) -> Tuple[Tuple[float, float], Tuple[float, float, float, float]]:
    """Use Google Docs page size and margins, with legacy export fallbacks."""
    style = baseline_snapshot.get("document_style")
    if not isinstance(style, dict) or not style:
        return default_page_size, (0.55 * inch, 0.55 * inch, 0.45 * inch, 0.45 * inch)

    page_size = style.get("pageSize") if isinstance(style.get("pageSize"), dict) else {}
    width = _dimension_points(page_size.get("width"), default_page_size[0])
    height = _dimension_points(page_size.get("height"), default_page_size[1])
    margins = (
        _dimension_points(style.get("marginLeft"), 0.55 * inch),
        _dimension_points(style.get("marginRight"), 0.55 * inch),
        _dimension_points(style.get("marginTop"), 0.45 * inch),
        _dimension_points(style.get("marginBottom"), 0.45 * inch),
    )
    return (width, height), margins


def _page_decoration_callback(
    snapshot: Dict[str, Any],
    styles: Dict[str, Any],
    Paragraph: Any,
    colors: Any,
    first_page: bool,
):
    """Draw the Google Docs header/footer selected for this page type."""
    document_style = snapshot.get("document_style")
    document_style = document_style if isinstance(document_style, dict) else {}
    use_first = first_page and bool(document_style.get("useFirstPageHeaderFooter"))
    header_id = str(
        document_style.get("firstPageHeaderId" if use_first else "defaultHeaderId") or ""
    )
    footer_id = str(
        document_style.get("firstPageFooterId" if use_first else "defaultFooterId") or ""
    )

    def draw_page_decorations(canvas: Any, doc_template: Any) -> None:
        page_width, page_height = canvas._pagesize
        header_y = page_height - _dimension_points(
            document_style.get("marginHeader"), 36.0
        )
        footer_y = _dimension_points(document_style.get("marginFooter"), 36.0)
        _draw_structural_blocks(
            canvas,
            doc_template,
            snapshot,
            snapshot.get("headers"),
            header_id,
            styles,
            Paragraph,
            colors,
            header_y,
            from_top=True,
        )
        _draw_structural_blocks(
            canvas,
            doc_template,
            snapshot,
            snapshot.get("footers"),
            footer_id,
            styles,
            Paragraph,
            colors,
            footer_y,
            from_top=False,
        )

    return draw_page_decorations


def _draw_structural_blocks(
    canvas: Any,
    doc_template: Any,
    snapshot: Dict[str, Any],
    structures: Any,
    structure_id: str,
    styles: Dict[str, Any],
    Paragraph: Any,
    colors: Any,
    y_position: float,
    from_top: bool,
) -> None:
    """Render simple styled header/footer paragraphs inside page margins."""
    if not structure_id or not isinstance(structures, dict):
        return
    blocks = structures.get(structure_id)
    if not isinstance(blocks, list):
        return
    current_y = y_position
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "paragraph":
            continue
        text = _styled_block_text(block, snapshot, colors)
        if not text:
            continue
        style = _source_paragraph_style(block, snapshot, styles["contact"], colors)
        paragraph = Paragraph(text, style)
        _width, height = paragraph.wrap(doc_template.width, doc_template.height)
        draw_y = current_y - height if from_top else current_y
        paragraph.drawOn(canvas, doc_template.leftMargin, draw_y)
        current_y = draw_y if from_top else current_y + height


def _dimension_points(value: Any, default: float = 0.0) -> float:
    """Convert Google Docs Dimension objects to ReportLab points."""
    if not isinstance(value, dict) or not isinstance(value.get("magnitude"), (int, float)):
        return float(default)
    magnitude = float(value["magnitude"])
    unit = str(value.get("unit") or "PT").upper()
    if unit == "IN":
        return magnitude * 72.0
    if unit == "CM":
        return magnitude * 72.0 / 2.54
    return magnitude


def _named_style(snapshot: Dict[str, Any], style_type: str) -> Dict[str, Any]:
    """Return one Google Docs named style from the saved style sheet."""
    named_styles = snapshot.get("named_styles")
    styles = named_styles.get("styles") if isinstance(named_styles, dict) else []
    for item in styles or []:
        if isinstance(item, dict) and item.get("namedStyleType") == style_type:
            return item
    return {}


def _effective_google_styles(
    block: Dict[str, Any],
    snapshot: Dict[str, Any],
    run_style: Dict[str, Any] | None = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Resolve Normal -> named -> paragraph/bullet -> run inheritance."""
    normal = _named_style(snapshot, "NORMAL_TEXT")
    direct_paragraph = block.get("paragraph_style")
    direct_paragraph = direct_paragraph if isinstance(direct_paragraph, dict) else {}
    style_type = str(direct_paragraph.get("namedStyleType") or "NORMAL_TEXT")
    named = _named_style(snapshot, style_type)

    paragraph_style: Dict[str, Any] = {}
    paragraph_style.update(normal.get("paragraphStyle") or {})
    paragraph_style.update(named.get("paragraphStyle") or {})
    paragraph_style.update(direct_paragraph)

    text_style: Dict[str, Any] = {}
    text_style.update(normal.get("textStyle") or {})
    text_style.update(named.get("textStyle") or {})
    bullet_style = block.get("bullet_style")
    if isinstance(bullet_style, dict):
        text_style.update(bullet_style.get("textStyle") or {})
    if isinstance(run_style, dict):
        text_style.update(run_style)
    return paragraph_style, text_style


def _source_paragraph_style(
    block: Dict[str, Any],
    snapshot: Dict[str, Any],
    fallback: Any,
    colors: Any,
) -> Any:
    """Translate preserved Google paragraph settings to a ReportLab style."""
    if not block.get("paragraph_style") and not block.get("text_runs"):
        return fallback
    from reportlab.lib.styles import ParagraphStyle

    runs = block.get("text_runs")
    first_run_style: Dict[str, Any] | None = None
    if isinstance(runs, list) and runs and isinstance(runs[0], dict):
        candidate = runs[0].get("text_style")
        if isinstance(candidate, dict):
            first_run_style = candidate
    paragraph_style, text_style = _effective_google_styles(
        block, snapshot, first_run_style
    )
    font_size = _dimension_points(text_style.get("fontSize"), fallback.fontSize)
    line_spacing = paragraph_style.get("lineSpacing")
    leading = font_size * float(line_spacing) / 100.0 if isinstance(line_spacing, (int, float)) else fallback.leading
    indent_start = _dimension_points(paragraph_style.get("indentStart"), fallback.leftIndent)
    indent_end = _dimension_points(paragraph_style.get("indentEnd"), fallback.rightIndent)
    first_line = _dimension_points(paragraph_style.get("indentFirstLine"), indent_start)
    alignment = {
        "START": 0,
        "CENTER": 1,
        "END": 2,
        "JUSTIFIED": 4,
    }.get(str(paragraph_style.get("alignment") or "").upper(), fallback.alignment)
    return ParagraphStyle(
        "GoogleResumeParagraph",
        parent=fallback,
        fontName=_font_name(text_style),
        fontSize=font_size,
        leading=leading,
        textColor=_text_color(text_style, colors, fallback.textColor),
        alignment=alignment,
        leftIndent=indent_start,
        rightIndent=indent_end,
        firstLineIndent=first_line - indent_start,
        spaceBefore=_dimension_points(paragraph_style.get("spaceAbove"), fallback.spaceBefore),
        spaceAfter=_dimension_points(paragraph_style.get("spaceBelow"), fallback.spaceAfter),
        keepWithNext=bool(
            paragraph_style.get("keepWithNext", getattr(fallback, "keepWithNext", False))
        ),
        allowWidows=(
            not bool(paragraph_style["avoidWidowAndOrphan"])
            if "avoidWidowAndOrphan" in paragraph_style
            else bool(getattr(fallback, "allowWidows", True))
        ),
        backColor=_background_color(paragraph_style.get("shading"), colors),
    )


def _font_name(text_style: Dict[str, Any]) -> str:
    """Map Google font family/weight flags to registered ReportLab faces."""
    family_value = text_style.get("weightedFontFamily")
    family = str((family_value or {}).get("fontFamily") or "Arial")
    weight = (family_value or {}).get("weight")
    bold = bool(text_style.get("bold")) or isinstance(weight, (int, float)) and weight >= 600
    italic = bool(text_style.get("italic"))
    if family.lower() == "arial" and _ARIAL_REGISTERED:
        if bold and italic:
            return "Arial-BoldItalic"
        if bold:
            return "Arial-Bold"
        if italic:
            return "Arial-Italic"
        return "Arial"
    if bold and italic:
        return "Helvetica-BoldOblique"
    if bold:
        return "Helvetica-Bold"
    if italic:
        return "Helvetica-Oblique"
    return "Helvetica"


def _text_color(text_style: Dict[str, Any], colors: Any, default: Any) -> Any:
    return _google_color(text_style.get("foregroundColor"), colors, default)


def _background_color(value: Any, colors: Any) -> Any:
    if not isinstance(value, dict):
        return None
    return _google_color(value.get("backgroundColor"), colors, None)


def _google_color(value: Any, colors: Any, default: Any) -> Any:
    """Convert Google RGB float components; an empty RGB object means black."""
    if not isinstance(value, dict):
        return default
    color = value.get("color") if isinstance(value.get("color"), dict) else value
    rgb = color.get("rgbColor") if isinstance(color, dict) else None
    if not isinstance(rgb, dict):
        return default
    return colors.Color(
        float(rgb.get("red", 0.0)),
        float(rgb.get("green", 0.0)),
        float(rgb.get("blue", 0.0)),
    )


def _styled_block_text(block: Dict[str, Any], snapshot: Dict[str, Any], colors: Any) -> str:
    """Convert Google text runs to safe ReportLab paragraph markup."""
    runs = block.get("text_runs")
    if not isinstance(runs, list) or not runs:
        return _escape(block.get("text") or block.get("bullet") or "")
    fragments: List[str] = []
    for run in runs:
        if not isinstance(run, dict) or not str(run.get("text") or ""):
            continue
        _paragraph_style, text_style = _effective_google_styles(
            block,
            snapshot,
            run.get("text_style") if isinstance(run.get("text_style"), dict) else {},
        )
        fragment = html.escape(str(run.get("text") or "")).replace("\n", "<br/>")
        attrs = [f'name="{_font_name(text_style)}"']
        size = _dimension_points(text_style.get("fontSize"), 0.0)
        if size:
            attrs.append(f'size="{size:g}"')
        color = _text_color(text_style, colors, None)
        if color is not None:
            attrs.append(f'color="{color.hexval()}"')
        background = _google_color(text_style.get("backgroundColor"), colors, None)
        if background is not None:
            attrs.append(f'backColor="{background.hexval()}"')
        fragment = f"<font {' '.join(attrs)}>{fragment}</font>"
        if text_style.get("underline"):
            fragment = f"<u>{fragment}</u>"
        if text_style.get("strikethrough"):
            fragment = f"<strike>{fragment}</strike>"
        baseline = str(text_style.get("baselineOffset") or "").upper()
        if baseline == "SUPERSCRIPT":
            fragment = f"<super>{fragment}</super>"
        elif baseline == "SUBSCRIPT":
            fragment = f"<sub>{fragment}</sub>"
        link = text_style.get("link")
        if isinstance(link, dict) and link.get("url"):
            fragment = f'<a href="{html.escape(str(link["url"]), quote=True)}">{fragment}</a>'
        fragments.append(fragment)
    return "".join(fragments) or _escape(block.get("text") or block.get("bullet") or "")


def _source_bullet_text(block: Dict[str, Any], snapshot: Dict[str, Any]) -> str:
    """Use the source list glyph for the saved nesting level when possible."""
    bullet = block.get("bullet_style")
    if not isinstance(bullet, dict):
        return "•"
    list_id = str(bullet.get("listId") or "")
    nesting_level = int(bullet.get("nestingLevel") or 0)
    lists = snapshot.get("lists") if isinstance(snapshot.get("lists"), dict) else {}
    list_item = lists.get(list_id) if isinstance(lists, dict) else {}
    properties = list_item.get("listProperties") if isinstance(list_item, dict) else {}
    levels = properties.get("nestingLevels") if isinstance(properties, dict) else []
    if isinstance(levels, list) and nesting_level < len(levels):
        level = levels[nesting_level]
        if isinstance(level, dict) and level.get("glyphSymbol"):
            return str(level["glyphSymbol"])
    return "•"


def resolve_resume_blocks(
    baseline_snapshot: Dict[str, Any],
    replacements: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    blocks = baseline_snapshot.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        blocks = _blocks_from_legacy_bullets(baseline_snapshot)

    replacement_by_anchor = _replacement_map(replacements)
    seen_anchors: set[str] = set()
    rendered: List[Dict[str, Any]] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        copied = dict(block)
        if copied.get("type") == "bullet":
            anchor_id = str(copied.get("anchor_id") or "")
            if anchor_id:
                if anchor_id in seen_anchors:
                    raise ResumePdfError("Synced baseline contains a duplicate bullet anchor.")
                seen_anchors.add(anchor_id)
            if anchor_id in replacement_by_anchor:
                copied["bullet"] = replacement_by_anchor[anchor_id]
                copied["text"] = replacement_by_anchor[anchor_id]
                # Replacement wording inherits the original paragraph/list
                # style, but not arbitrary inline emphasis from the old text.
                if isinstance(copied.get("text_runs"), list):
                    copied["text_runs"] = [
                        {"text": replacement_by_anchor[anchor_id], "text_style": {}}
                    ]
        elif copied.get("type") == "table":
            copied["rows"] = _resolve_table_rows(copied.get("rows"), replacement_by_anchor, seen_anchors)
        rendered.append(copied)

    missing = sorted(set(replacement_by_anchor) - seen_anchors)
    if missing:
        raise ResumePdfError("Approved swap references a bullet that is not in the synced baseline.")
    return rendered


def _resolve_table_rows(
    rows: Any,
    replacement_by_anchor: Dict[str, str],
    seen_anchors: set[str],
) -> List[List[Dict[str, Any]]]:
    resolved_rows: List[List[Dict[str, Any]]] = []
    for row in rows or []:
        if not isinstance(row, list):
            continue
        resolved_cells: List[Dict[str, Any]] = []
        for cell in row:
            if not isinstance(cell, dict):
                continue
            cell_blocks: List[Dict[str, Any]] = []
            for block in cell.get("blocks") or []:
                if not isinstance(block, dict):
                    continue
                copied = dict(block)
                if copied.get("type") == "bullet":
                    anchor_id = str(copied.get("anchor_id") or "")
                    if anchor_id:
                        if anchor_id in seen_anchors:
                            raise ResumePdfError("Synced baseline contains a duplicate bullet anchor.")
                        seen_anchors.add(anchor_id)
                    if anchor_id in replacement_by_anchor:
                        copied["bullet"] = replacement_by_anchor[anchor_id]
                        copied["text"] = replacement_by_anchor[anchor_id]
                        if isinstance(copied.get("text_runs"), list):
                            copied["text_runs"] = [
                                {"text": replacement_by_anchor[anchor_id], "text_style": {}}
                            ]
                elif copied.get("type") == "table":
                    copied["rows"] = _resolve_table_rows(copied.get("rows"), replacement_by_anchor, seen_anchors)
                cell_blocks.append(copied)
            resolved_cell = dict(cell)
            resolved_cell["blocks"] = cell_blocks
            resolved_cells.append(resolved_cell)
        if resolved_cells:
            resolved_rows.append(resolved_cells)
    return resolved_rows


def _replacement_map(replacements: List[Dict[str, Any]]) -> Dict[str, str]:
    mapped: Dict[str, str] = {}
    for item in replacements:
        anchor_id = str((item or {}).get("anchor_id") or "").strip()
        text = str((item or {}).get("approved_bullet") or "").strip()
        if not anchor_id or not text:
            raise ResumePdfError("Every approved swap needs an anchor and replacement bullet.")
        if anchor_id in mapped:
            raise ResumePdfError("A baseline bullet can only be swapped once.")
        mapped[anchor_id] = text
    return mapped


def _blocks_from_legacy_bullets(baseline_snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Render older snapshots acceptably after grouping bullets by section."""
    bullets = baseline_snapshot.get("bullets") if isinstance(baseline_snapshot, dict) else []
    if not isinstance(bullets, list):
        return []
    blocks: List[Dict[str, Any]] = []
    current_section = object()
    for bullet in bullets:
        if not isinstance(bullet, dict):
            continue
        section = str(bullet.get("section") or "Experience").strip()
        if section and section != current_section:
            blocks.append({"type": "paragraph", "text": section, "section": section})
            current_section = section
        copied = dict(bullet)
        copied["type"] = "bullet"
        copied["text"] = str(copied.get("bullet") or copied.get("text") or "")
        blocks.append(copied)
    return blocks


def _paragraph_style_name(text: str, paragraph_index: int) -> str:
    if paragraph_index == 0:
        return "name"
    if paragraph_index <= 2 and ("@" in text or "|" in text or "linkedin" in text.lower()):
        return "contact"
    compact = text.strip()
    if len(compact) <= 42 and not compact.endswith("."):
        return "section"
    return "body"


def _table_flowable(
    block: Dict[str, Any],
    styles: Dict[str, Any],
    width: float,
    Paragraph: Any,
    Table: Any,
    TableStyle: Any,
    colors: Any,
    snapshot: Dict[str, Any],
):
    rows = block.get("rows")
    if not isinstance(rows, list) or not rows:
        return None
    max_cols = max((len(row) for row in rows if isinstance(row, list)), default=0)
    if max_cols <= 0:
        return None

    rendered_rows: List[List[Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        rendered_cells: List[Any] = []
        for cell in row:
            flows: List[Any] = []
            if isinstance(cell, dict):
                for index, cell_block in enumerate(cell.get("blocks") or []):
                    if not isinstance(cell_block, dict):
                        continue
                    text = _escape(cell_block.get("text") or cell_block.get("bullet") or "")
                    if not text:
                        continue
                    text = _styled_block_text(cell_block, snapshot, colors)
                    if cell_block.get("type") == "bullet":
                        style = _source_paragraph_style(
                            cell_block, snapshot, styles["table_bullet"], colors
                        )
                        flows.append(
                            Paragraph(
                                text,
                                style,
                                bulletText=_source_bullet_text(cell_block, snapshot),
                            )
                        )
                    elif cell_block.get("type") == "table":
                        nested = _table_flowable(
                            cell_block,
                            styles,
                            width / max_cols,
                            Paragraph,
                            Table,
                            TableStyle,
                            colors,
                            snapshot,
                        )
                        if nested:
                            flows.append(nested)
                    else:
                        fallback = styles["table_heading"] if index == 0 else styles["table_body"]
                        style = _source_paragraph_style(
                            cell_block, snapshot, fallback, colors
                        )
                        flows.append(Paragraph(text, style))
            rendered_cells.append(flows or [""])
        while len(rendered_cells) < max_cols:
            rendered_cells.append("")
        rendered_rows.append(rendered_cells)

    table = Table(
        rendered_rows,
        colWidths=_source_table_widths(block, width, max_cols),
        hAlign="LEFT",
    )
    commands: List[Tuple[Any, ...]] = [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 1),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("BOX", (0, 0), (-1, -1), 0, colors.white),
        ("INNERGRID", (0, 0), (-1, -1), 0, colors.white),
    ]
    # Google Docs can set padding and shading per cell. Apply those values
    # without changing the legacy defaults for older snapshots.
    for row_index, row in enumerate(rows):
        for column_index, cell in enumerate(row if isinstance(row, list) else []):
            cell_style = cell.get("cell_style") if isinstance(cell, dict) else {}
            if not isinstance(cell_style, dict):
                continue
            position = (column_index, row_index)
            for source_key, command in (
                ("paddingLeft", "LEFTPADDING"),
                ("paddingRight", "RIGHTPADDING"),
                ("paddingTop", "TOPPADDING"),
                ("paddingBottom", "BOTTOMPADDING"),
            ):
                if source_key in cell_style:
                    commands.append(
                        (command, position, position, _dimension_points(cell_style[source_key]))
                    )
            background = _google_color(cell_style.get("backgroundColor"), colors, None)
            if background is not None:
                commands.append(("BACKGROUND", position, position, background))
    table.setStyle(TableStyle(commands))
    return table


def _source_table_widths(block: Dict[str, Any], width: float, column_count: int) -> List[float]:
    """Preserve explicit Google table widths and scale them to usable width."""
    table_style = block.get("table_style")
    properties = (
        table_style.get("tableColumnProperties")
        if isinstance(table_style, dict)
        else None
    )
    if not isinstance(properties, list) or len(properties) != column_count:
        return [width / column_count] * column_count
    raw = [_dimension_points(item.get("width"), 0.0) for item in properties if isinstance(item, dict)]
    if len(raw) != column_count or not all(value > 0 for value in raw):
        return [width / column_count] * column_count
    scale = width / sum(raw)
    return [value * scale for value in raw]


def _two_column_line(
    left: str,
    right: str,
    styles: Dict[str, Any],
    width: float,
    Paragraph: Any,
    Table: Any,
    TableStyle: Any,
    colors: Any,
    emphasis: bool,
    source_style: Any = None,
):
    style = source_style or (styles["table_heading"] if emphasis else styles["body"])
    if source_style is not None:
        from reportlab.lib.styles import ParagraphStyle

        right_style = ParagraphStyle(
            "GoogleResumeParagraphRight", parent=source_style, alignment=2
        )
    else:
        right_style = styles["table_heading_right"] if emphasis else styles["body_right"]
    table = Table(
        [[Paragraph(_escape(left), style), Paragraph(_escape(right), right_style)]],
        colWidths=[width * 0.72, width * 0.28],
        hAlign="LEFT",
    )
    table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
                ("BOX", (0, 0), (-1, -1), 0, colors.white),
            ]
        )
    )
    return table


def _split_resume_detail_line(value: Any) -> Tuple[str, str, bool] | None:
    text = str(value or "").strip()
    if not text:
        return None

    date_match = re.search(
        r"\s+((?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)\s+\d{4}\s*-\s*(?:PRESENT|(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|SEPT|OCT|NOV|DEC)\s+\d{4}))$",
        text,
        flags=re.IGNORECASE,
    )
    if date_match:
        left = text[: date_match.start()].strip()
        return (left, date_match.group(1).strip(), False) if left else None

    location_match = re.search(r"\s+([A-Z][A-Za-z .'-]+,\s*[A-Z]{2,}|[A-Z][A-Za-z .'-]+,\s*[A-Z][A-Za-z .'-]+)$", text)
    if location_match:
        left = text[: location_match.start()].strip()
        if left and (" - " in left or " | " in left or len(left.split()) >= 3):
            return left, location_match.group(1).strip(), True
    return None


def _escape(value: Any) -> str:
    return html.escape(str(value or "").strip()).replace("\n", "<br/>")
