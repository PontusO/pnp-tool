"""
PDF report generator for ClaudePnP.

Produces a single document that consolidates all statistics and output tables
from a production run so operators have a single file for documentation,
traceability, and machine setup reference.

Report structure
----------------
1. Title block — job name, date, machine count, total placements
2. Per-machine section (one section per machine):
   a. Run statistics (cycles, utilisation, board-time estimate)
   b. Nozzle configuration table — head-by-head loading sheet
   c. Feeder assignment table — slot map for the feeder bank
   d. Sequence summary — one row per pick sequence
3. Component types table — full component resolution from Phase 1
"""

import csv
import math
from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    HRFlowable, PageBreak, Paragraph, SimpleDocTemplate,
    Spacer, Table, TableStyle,
)


# ── Colour palette ─────────────────────────────────────────────────────────────

_NAVY     = colors.HexColor("#1a3557")
_BLUE     = colors.HexColor("#2d6a9f")
_LBLUE    = colors.HexColor("#d6e8f7")
_WHITE    = colors.white
_LGREY    = colors.HexColor("#f5f5f5")
_GREEN    = colors.HexColor("#2e7d32")
_ORANGE   = colors.HexColor("#e65100")
_BLACK    = colors.black


# ── Style helpers ──────────────────────────────────────────────────────────────

def _styles():
    s = getSampleStyleSheet()
    s.add(ParagraphStyle("Title2",   parent=s["Title"],   fontSize=22, textColor=_NAVY,
                         spaceAfter=4))
    s.add(ParagraphStyle("H1",       parent=s["Heading1"], fontSize=14, textColor=_NAVY,
                         spaceBefore=12, spaceAfter=4))
    s.add(ParagraphStyle("H2",       parent=s["Heading2"], fontSize=11, textColor=_BLUE,
                         spaceBefore=8,  spaceAfter=2))
    s.add(ParagraphStyle("Mono",     parent=s["Normal"],   fontName="Courier",
                         fontSize=8))
    s.add(ParagraphStyle("Small",    parent=s["Normal"],   fontSize=8,
                         textColor=colors.HexColor("#555555")))
    s.add(ParagraphStyle("Stat",     parent=s["Normal"],   fontSize=10,
                         leading=16))
    s.add(ParagraphStyle("Cell",     parent=s["Normal"],   fontSize=7.5, leading=9))
    s.add(ParagraphStyle("CellHdr",  parent=s["Normal"],   fontSize=8, leading=10,
                         fontName="Helvetica-Bold", textColor=_WHITE))
    return s


def _esc(text: str) -> str:
    """Escape characters that have meaning in reportlab Paragraph mini-XML."""
    return (str(text).replace("&", "&amp;")
                     .replace("<", "&lt;")
                     .replace(">", "&gt;"))


def _wrap(cells, style):
    """Wrap a row of raw values in Paragraphs so long text wraps within the column."""
    return [Paragraph(_esc(c), style) for c in cells]


def _table_style(n_rows: int, header_rows: int = 1, stripe: bool = True) -> TableStyle:
    """Build a table style. n_rows is the total row count including header(s)."""
    cmds = [
        # Header
        ("BACKGROUND",    (0, 0), (-1, header_rows - 1), _BLUE),
        ("TEXTCOLOR",     (0, 0), (-1, header_rows - 1), _WHITE),
        ("FONTNAME",      (0, 0), (-1, header_rows - 1), "Helvetica-Bold"),
        ("FONTSIZE",      (0, 0), (-1, header_rows - 1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, header_rows - 1), 5),
        ("TOPPADDING",    (0, 0), (-1, header_rows - 1), 5),
        # Body
        ("FONTNAME",      (0, header_rows), (-1, -1), "Helvetica"),
        ("FONTSIZE",      (0, header_rows), (-1, -1), 7.5),
        ("TOPPADDING",    (0, header_rows), (-1, -1), 3),
        ("BOTTOMPADDING", (0, header_rows), (-1, -1), 3),
        # Grid
        ("GRID",          (0, 0), (-1, -1), 0.3, colors.HexColor("#cccccc")),
        ("LINEBELOW",     (0, header_rows - 1), (-1, header_rows - 1), 1, _NAVY),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]
    if stripe:
        for row in range(header_rows, n_rows, 2):
            cmds.append(("BACKGROUND", (0, row), (-1, row), _LGREY))
    return TableStyle(cmds)


def _stat_block(label: str, value: str, styles) -> Table:
    """A small two-cell stat card (label | value)."""
    t = Table([[Paragraph(label, styles["Small"]),
                Paragraph(f"<b>{value}</b>", styles["Stat"])]],
              colWidths=[4 * cm, 5 * cm])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0, 0), (0, 0), _LBLUE),
        ("BACKGROUND",    (1, 0), (1, 0), _WHITE),
        ("BOX",           (0, 0), (-1, -1), 0.5, _BLUE),
        ("LINEAFTER",     (0, 0), (0, 0), 0.5, _BLUE),
        ("TOPPADDING",    (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING",   (0, 0), (-1, -1), 6),
        ("RIGHTPADDING",  (0, 0), (-1, -1), 6),
        ("VALIGN",        (0, 0), (-1, -1), "MIDDLE"),
    ]))
    return t


def _fmt_time(secs: float) -> str:
    m, s = divmod(int(secs), 60)
    return f"{m}m {s:02d}s" if m else f"{s}s"


# ── CSV readers ────────────────────────────────────────────────────────────────

def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    """Return (headers, rows) from a CSV file, skipping comment lines."""
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.reader(f)
        rows   = [r for r in reader if r and not r[0].startswith("#")]
    if not rows:
        return [], []
    return rows[0], rows[1:]


def _read_sequence_headers(path: Path) -> list[dict]:
    """Parse # SEQ header lines from a sequence file."""
    seqs = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line.startswith("# SEQ"):
                continue
            # # SEQ 0001  row=FRONT  placements=8  pick_descends=2
            parts = dict(tok.split("=") for tok in line.split() if "=" in tok)
            seqs.append({
                "seq":           line.split()[2],
                "row":           parts.get("row", ""),
                "placements":    parts.get("placements", ""),
                "pick_descends": parts.get("pick_descends", ""),
            })
    return seqs


# ── Section builders ───────────────────────────────────────────────────────────

def _section_machine_stats(machine: dict, styles, page_w: float) -> list:
    """Stat cards for one machine's run metrics."""
    st = machine["stats"]
    et, lo, hi = machine["board_time"]

    stats = [
        ("Pick cycles",        f"{st['cycles']}  (ideal min: {st['ideal_min']})"),
        ("Total placements",   str(st["total_placements"])),
        ("Head utilisation",   f"{st['utilisation_pct']:.0f}%"),
        ("Pick descends",      str(st["total_descends"])),
        ("Best simultaneous",  f"{st['max_simultaneous']} per descend"),
        ("Est. board time",    f"{_fmt_time(et)}  ({_fmt_time(lo)} – {_fmt_time(hi)})"),
    ]
    cards = [[_stat_block(lbl, val, styles) for lbl, val in stats[i:i+3]]
             for i in range(0, len(stats), 3)]
    col_w = (page_w - 2 * cm) / 3
    out = []
    for row in cards:
        t = Table([row], colWidths=[col_w] * len(row), hAlign="LEFT")
        t.setStyle(TableStyle([("LEFTPADDING", (0, 0), (-1, -1), 0),
                                ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                                ("TOPPADDING",   (0, 0), (-1, -1), 0),
                                ("BOTTOMPADDING",(0, 0), (-1, -1), 4)]))
        out.append(t)
    return out


def _section_nozzle_config(nozzle_csv: Path, styles) -> list:
    headers, rows = _read_csv(nozzle_csv)
    if not headers:
        return []
    data = [headers] + rows
    col_widths = [2 * cm, 4 * cm]
    t = Table(data, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    t.setStyle(_table_style(len(data)))
    return [Paragraph("Nozzle Configuration", styles["H2"]), t]


def _section_feeders(feeders_csv: Path, styles, page_w: float) -> list:
    headers, rows = _read_csv(feeders_csv)
    if not headers:
        return []

    # Keep the most operator-relevant columns; drop physical_x_mm (derived).
    # Map CSV column → (display label, fixed width in cm or None for flexible).
    spec = [
        ("slot",             "Slot",    1.0),
        ("row",              "Row",     1.5),
        ("mod_group",        "Mod",     1.2),
        ("value",            "Value",   None),
        ("package",          "Package", None),
        ("feeder_width_mm",  "Width",   1.3),
        ("nozzle_type",      "Nozzle",  1.5),
        ("name_mpn",         "MPN",     None),
        ("total_placements", "Qty",     1.2),
    ]
    spec = [s for s in spec if s[0] in headers]
    keep_idx = [headers.index(s[0]) for s in spec]

    hdr_cells = _wrap([s[1] for s in spec], styles["CellHdr"])
    body = [_wrap([r[i] for i in keep_idx], styles["Cell"]) for r in rows]
    data = [hdr_cells] + body

    avail       = page_w - 2 * cm
    fixed_total = sum(s[2] for s in spec if s[2]) * cm
    flex_cols   = [s for s in spec if s[2] is None]
    flex_w      = (avail - fixed_total) / max(len(flex_cols), 1)
    col_widths  = [(s[2] * cm) if s[2] else flex_w for s in spec]

    t = Table(data, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    style = _table_style(len(data))
    row_col = headers.index("row")
    for i, r in enumerate(rows, start=1):
        if r[row_col] == "REAR":
            style.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fff3e0"))
    t.setStyle(style)
    return [Paragraph("Feeder Assignment", styles["H2"]), t]


def _section_sequences(sequence_txt: Path, styles, page_w: float) -> list:
    seqs = _read_sequence_headers(sequence_txt)
    if not seqs:
        return []
    headers = ["Seq", "Row", "Placements", "Pick descends"]
    data = [headers] + [[s["seq"], s["row"], s["placements"], s["pick_descends"]]
                        for s in seqs]
    avail     = page_w - 2 * cm
    col_widths = [1.8 * cm, 2.5 * cm, 2.5 * cm, 3.0 * cm]
    t = Table(data, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    style = _table_style(len(data))
    for i, s in enumerate(seqs, start=1):
        if s["row"] == "REAR":
            style.add("BACKGROUND", (0, i), (-1, i), colors.HexColor("#fff3e0"))
    t.setStyle(style)
    return [Paragraph("Pick Sequence Summary", styles["H2"]), t]


def _section_components(components_csv: Path, styles, page_w: float) -> list:
    headers, rows = _read_csv(components_csv)
    if not headers:
        return []

    spec = [
        ("value",        "Value",      None),
        ("package",      "Package",    None),
        ("count",        "Qty",        1.0),
        ("feeder_width", "Width",      1.3),
        ("feeder_row",   "Row",        1.5),
        ("nozzle_type",  "Nozzle",     1.5),
        ("name",         "MPN",        None),
        ("matched_by",   "Matched by", None),
        ("status",       "Status",     1.8),
    ]
    spec = [s for s in spec if s[0] in headers]
    keep_idx   = [headers.index(s[0]) for s in spec]
    status_col = headers.index("status") if "status" in headers else -1

    hdr_cells = _wrap([s[1] for s in spec], styles["CellHdr"])
    body = []
    for r in rows:
        cells = []
        for s, idx in zip(spec, keep_idx):
            val = r[idx]
            if s[0] == "status":
                colour = "#2e7d32" if val == "OK" else "#e65100"
                cells.append(Paragraph(
                    f'<font color="{colour}"><b>{_esc(val)}</b></font>',
                    styles["Cell"]))
            else:
                cells.append(Paragraph(_esc(val), styles["Cell"]))
        body.append(cells)
    data = [hdr_cells] + body

    avail       = page_w - 2 * cm
    fixed_total = sum(s[2] for s in spec if s[2]) * cm
    flex_cols   = [s for s in spec if s[2] is None]
    flex_w      = (avail - fixed_total) / max(len(flex_cols), 1)
    col_widths  = [(s[2] * cm) if s[2] else flex_w for s in spec]

    t = Table(data, colWidths=col_widths, hAlign="LEFT", repeatRows=1)
    t.setStyle(_table_style(len(data)))
    return [Paragraph("Component Types", styles["H1"]), t]


# ── Public entry point ────────────────────────────────────────────────────────

def write_pdf_report(
    output_path:    Path,
    job_name:       str,
    machines:       list[dict],
    components_csv: Path,
    n_heads:        int,
) -> None:
    """
    Generate the PDF report.

    Each entry in *machines* must be a dict with keys:
        label        str       e.g. "Machine 1" or "" for single-machine jobs
        stats        dict      from PnPOptimizer.cycle_stats()
        board_time   tuple     (est_s, min_s, max_s)
        head_config  dict      nozzle_type → head_count
        feeders_csv  Path
        sequence_txt Path
        nozzle_csv   Path
    """
    styles   = _styles()
    page_w, page_h = landscape(A4)
    doc = SimpleDocTemplate(
        str(output_path),
        pagesize=landscape(A4),
        leftMargin=1 * cm, rightMargin=1 * cm,
        topMargin=1.5 * cm, bottomMargin=1.5 * cm,
        title=f"ClaudePnP Report — {job_name}",
        author="ClaudePnP",
    )

    story = []

    # ── Title block ──────────────────────────────────────────────────────────
    story.append(Paragraph("ClaudePnP Production Report", styles["Title2"]))
    story.append(HRFlowable(width="100%", thickness=2, color=_NAVY, spaceAfter=6))

    total_placements = sum(m["stats"]["total_placements"] for m in machines)
    meta = [
        ["Job",        job_name],
        ["Date",       datetime.now().strftime("%Y-%m-%d  %H:%M")],
        ["Machines",   str(len(machines))],
        ["Heads",      str(n_heads)],
        ["Placements", str(total_placements)],
    ]
    meta_t = Table(meta, colWidths=[2.5 * cm, 7 * cm], hAlign="LEFT")
    meta_t.setStyle(TableStyle([
        ("FONTNAME",  (0, 0), (0, -1), "Helvetica-Bold"),
        ("FONTSIZE",  (0, 0), (-1, -1), 9),
        ("TOPPADDING",(0, 0), (-1, -1), 2),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
        ("TEXTCOLOR", (0, 0), (0, -1), _NAVY),
    ]))
    story.append(meta_t)
    story.append(Spacer(1, 0.5 * cm))

    # ── Per-machine sections ─────────────────────────────────────────────────
    for machine in machines:
        label = machine["label"] or "Single Machine"
        story.append(Paragraph(label, styles["H1"]))
        story.append(HRFlowable(width="100%", thickness=0.5, color=_BLUE, spaceAfter=4))

        # Stat cards
        for card_row in _section_machine_stats(machine, styles, page_w):
            story.append(card_row)
        story.append(Spacer(1, 0.4 * cm))

        # Nozzle config + sequences side by side
        nozzle_content = _section_nozzle_config(machine["nozzle_csv"], styles)
        seq_content    = _section_sequences(machine["sequence_txt"], styles, page_w)

        if nozzle_content and seq_content:
            left_col  = nozzle_content
            right_col = seq_content
            # Wrap each column in a mini-table for side-by-side layout
            col_w = (page_w - 2 * cm) / 2 - 0.5 * cm
            side = Table([[left_col, right_col]],
                         colWidths=[col_w, col_w + 1 * cm])
            side.setStyle(TableStyle([
                ("VALIGN",       (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING",  (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]))
            story.append(side)
        elif nozzle_content:
            story.extend(nozzle_content)
        elif seq_content:
            story.extend(seq_content)

        story.append(Spacer(1, 0.4 * cm))

        # Feeder table (full width, can be long)
        story.extend(_section_feeders(machine["feeders_csv"], styles, page_w))
        story.append(PageBreak())

    # ── Component types (shared across all machines) ──────────────────────────
    story.extend(_section_components(components_csv, styles, page_w))

    doc.build(story)
