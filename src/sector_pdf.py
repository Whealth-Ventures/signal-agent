"""PDF renderer for the sector digest — an alternative delivery to Slack.

Produces ONE combined document (a fortnightly "Sector Signal" report): a cover
line, then one section per sector (each starting on its own page), grouped by
event type with bullet one-liners and clickable source links. Mirrors the
Slack formatter's content exactly — same ordering, same one-liners, same geo
tags — just laid out for print/read instead of chat.

Pure-Python via reportlab (no system deps). Import is lazy in the pipeline so a
Slack-only run never needs reportlab installed.
"""
from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    PageBreak,
)

from query_planner import Sector
from sector_ranker import SectorRankingResult

_INK = HexColor("#111827")
_MUTED = HexColor("#6b7280")
_ACCENT = HexColor("#1a56db")
_RULE = HexColor("#e5e7eb")

_GEO_TAG = {"India": "[IND]", "US": "[US]", "Global": "[GLOBAL]"}


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "SigTitle", parent=base["Title"], fontSize=20, leading=24,
            textColor=_INK, spaceAfter=2,
        ),
        "subtitle": ParagraphStyle(
            "SigSub", parent=base["Normal"], fontSize=10, leading=14,
            textColor=_MUTED, spaceAfter=10,
        ),
        "sector": ParagraphStyle(
            "SigSector", parent=base["Heading1"], fontSize=16, leading=20,
            textColor=_INK, spaceBefore=4, spaceAfter=0,
        ),
        "portco": ParagraphStyle(
            "SigPortco", parent=base["Normal"], fontSize=9.5, leading=13,
            textColor=_MUTED, spaceAfter=6,
        ),
        "summary": ParagraphStyle(
            "SigSummary", parent=base["Normal"], fontSize=10.5, leading=15,
            textColor=_INK, spaceAfter=8,
        ),
        "group": ParagraphStyle(
            "SigGroup", parent=base["Heading2"], fontSize=11.5, leading=15,
            textColor=_ACCENT, spaceBefore=8, spaceAfter=3,
        ),
        "bullet": ParagraphStyle(
            "SigBullet", parent=base["Normal"], fontSize=10, leading=14,
            textColor=_INK, alignment=TA_LEFT, leftIndent=12, bulletIndent=0,
            spaceAfter=3,
        ),
        "empty": ParagraphStyle(
            "SigEmpty", parent=base["Normal"], fontSize=10, leading=14,
            textColor=_MUTED, spaceAfter=6, italic=True,
        ),
    }


def _bullet_markup(one_liner: str, url: str, geo: str | None) -> str:
    tag = _GEO_TAG.get(geo or "Global", "[GLOBAL]")
    text = escape((one_liner or "").strip())
    safe_url = escape(url or "", {'"': "&quot;"})
    link = (
        f' <a href="{safe_url}"><font color="#1a56db">link</font></a>'
        if url else ""
    )
    return f'<b>{tag}</b> {text}{link}'


def render_sector_pdf(
    entries: list[tuple[SectorRankingResult, Sector]],
    out_path: Path,
    *,
    date_label: str,
    title: str = "Sector Signal",
) -> Path:
    """Render all sector results into one PDF at `out_path`. Returns the path."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    st = _styles()

    doc = SimpleDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=f"{title} — {date_label}", author="Signal Agent",
    )

    total_items = sum(r.total for r, _ in entries)
    flow: list = [
        Paragraph(f"{escape(title)} — Fortnightly Sector Roundup", st["title"]),
        Paragraph(
            f"{escape(date_label)}  ·  {len(entries)} sectors  ·  "
            f"{total_items} items",
            st["subtitle"],
        ),
    ]

    for i, (result, sector) in enumerate(entries):
        if i > 0:
            flow.append(PageBreak())
        portco = f" · {escape(sector.portco)}" if sector.portco else ""
        plural = "item" if result.total == 1 else "items"
        flow.append(Paragraph(escape(sector.display), st["sector"]))
        flow.append(Paragraph(
            f"{portco.lstrip(' ·') or '—'}  ·  {result.total} {plural}",
            st["portco"],
        ))
        if result.summary:
            flow.append(Paragraph(escape(result.summary), st["summary"]))
        if result.total == 0:
            flow.append(Paragraph(
                "No substantive sector news in the last 14 days.", st["empty"],
            ))
            continue
        for group_display, items in result.groups:
            flow.append(Paragraph(
                f"{escape(group_display)} ({len(items)})", st["group"],
            ))
            for r in items:
                flow.append(Paragraph(
                    _bullet_markup(r.one_liner, r.story.canonical_url, r.story.geo),
                    st["bullet"], bulletText="•",
                ))

    doc.build(flow)
    return out_path
