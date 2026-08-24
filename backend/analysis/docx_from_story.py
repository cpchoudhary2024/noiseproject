# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""Render a ReportLab story to a Word document.

Why this exists
---------------
The Word and PDF versions of a report must say the same thing. The way to
guarantee that is not to write the document twice — it is to build the content
once, as a ReportLab story, and render that one story to both formats. Every
section, every number and every caption therefore comes from a single code path;
a change to the PDF cannot leave the Word version behind, because there is no
second version to update.

What is preserved
-----------------
Page size, margins, section order, page breaks, table structure and shading,
figure images at their exact printed dimensions, and the font size of every
element. The result is an ordinary Word document — real paragraphs, real
headings, real tables — so it can be edited and tracked normally.

What cannot be guaranteed
-------------------------
Word repaginates with its own layout engine and the reader's installed fonts, so
line breaks within a paragraph may fall differently from the PDF. Page breaks
that the story states explicitly are reproduced exactly; breaks that ReportLab
chose by filling a page are approximated by the same content in the same order.
"""
from __future__ import annotations

import io
import os
import re
from xml.etree import ElementTree

from docx import Document
from docx.enum.section import WD_SECTION
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table)

# Helvetica is the PDF face; Arial is its metrically compatible Word equivalent,
# so a given point size occupies the same width in both documents.
_BODY_FONT = 'Arial'

_ALIGN = {
    TA_CENTER: WD_ALIGN_PARAGRAPH.CENTER,
    TA_RIGHT: WD_ALIGN_PARAGRAPH.RIGHT,
    TA_JUSTIFY: WD_ALIGN_PARAGRAPH.JUSTIFY,
}

# The subset of ReportLab's mini-HTML actually used by these reports.
_ENTITIES = {
    '&nbsp;': ' ', '&amp;': '&', '&lt;': '<', '&gt;': '>',
    '&ldquo;': '“', '&rdquo;': '”', '&times;': '×',
    '&mdash;': '—', '&ndash;': '–', '&deg;': '°',
}


def _plain_runs(markup: str) -> list[tuple[str, bool, bool, str | None]]:
    """Flatten ReportLab paragraph markup to (text, bold, italic, hex colour) runs.

    Parsed as XML rather than by stripping tags, so nesting such as
    ``<b><font color="#c00">…</font></b>`` keeps both attributes instead of
    losing whichever tag is handled second.
    """
    text = markup or ''
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = re.sub(r'<br\s*/?>', '\n', text)
    # Any entity this module does not know would abort the XML parse; the
    # document must still render, so drop the marker rather than the report.
    text = re.sub(r'&(?![a-zA-Z]+;|#\d+;)', '&amp;', text)

    try:
        root = ElementTree.fromstring(f'<root>{text}</root>')
    except ElementTree.ParseError:
        return [(re.sub(r'<[^>]+>', '', text), False, False, None)]

    runs: list[tuple[str, bool, bool, str | None]] = []

    def walk(node, bold: bool, italic: bool, colour: str | None):
        tag = node.tag.lower()
        if tag in ('b', 'strong'):
            bold = True
        elif tag in ('i', 'em'):
            italic = True
        if tag == 'font' and node.get('color'):
            colour = node.get('color')
        if node.text:
            runs.append((node.text, bold, italic, colour))
        for child in node:
            walk(child, bold, italic, colour)
            if child.tail:
                runs.append((child.tail, bold, italic, colour))

    walk(root, False, False, None)
    return [r for r in runs if r[0]]


def _hex_to_rgb(value: str | None) -> RGBColor | None:
    if not value:
        return None
    v = str(value).lstrip('#')
    if len(v) != 6:
        return None
    try:
        return RGBColor(int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except ValueError:
        return None


def _reportlab_colour_hex(colour) -> str | None:
    """ReportLab Color -> 'RRGGBB', or None when it has no usable value."""
    try:
        return '{:02X}{:02X}{:02X}'.format(
            int(round(colour.red * 255)), int(round(colour.green * 255)),
            int(round(colour.blue * 255)))
    except AttributeError:
        return None


def _shade_cell(cell, hex_fill: str) -> None:
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), hex_fill)
    cell._tc.get_or_add_tcPr().append(shd)


def _set_cell_borders(cell, hex_colour: str, eighths: int = 4) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement('w:tcBorders')
    for edge in ('top', 'left', 'bottom', 'right'):
        el = OxmlElement(f'w:{edge}')
        el.set(qn('w:val'), 'single')
        el.set(qn('w:sz'), str(eighths))
        el.set(qn('w:color'), hex_colour)
        borders.append(el)
    tc_pr.append(borders)


class _TableStyleIndex:
    """Look up the ReportLab TableStyle commands that apply to a given cell."""

    def __init__(self, table: Table):
        self._cmds = []
        style = getattr(table, '_bkgrndcmds', None)
        for source in (getattr(table, '_cellstyles', None),):
            del source  # cell styles are already resolved into the commands below
        raw = list(getattr(table, '_commands', []) or [])
        raw += list(style or [])
        self._cmds = raw
        self._nrows = len(table._cellvalues)
        self._ncols = len(table._cellvalues[0]) if table._cellvalues else 0

    @staticmethod
    def _norm(idx: int, n: int) -> int:
        return idx if idx >= 0 else n + idx

    def _covers(self, cmd, row: int, col: int) -> bool:
        try:
            (c0, r0), (c1, r1) = cmd[1], cmd[2]
        except (IndexError, TypeError, ValueError):
            return False
        r0, r1 = self._norm(r0, self._nrows), self._norm(r1, self._nrows)
        c0, c1 = self._norm(c0, self._ncols), self._norm(c1, self._ncols)
        return min(r0, r1) <= row <= max(r0, r1) and min(c0, c1) <= col <= max(c0, c1)

    def lookup(self, name: str, row: int, col: int):
        """Last matching command wins, as in ReportLab."""
        found = None
        for cmd in self._cmds:
            if cmd and str(cmd[0]).upper() == name and self._covers(cmd, row, col):
                found = cmd
        return found

    def spans(self) -> list[tuple[int, int, int, int]]:
        out = []
        for cmd in self._cmds:
            if cmd and str(cmd[0]).upper() == 'SPAN':
                (c0, r0), (c1, r1) = cmd[1], cmd[2]
                out.append((self._norm(r0, self._nrows), self._norm(c0, self._ncols),
                            self._norm(r1, self._nrows), self._norm(c1, self._ncols)))
        return out


class StoryToDocx:
    """Walk a ReportLab story and emit the equivalent Word document."""

    def __init__(self, *, page_width_in: float, page_height_in: float,
                 margins_in: tuple[float, float, float, float],
                 footer_lines: tuple[str, ...] = ()):
        self.doc = Document()
        section = self.doc.sections[0]
        section.page_width = Inches(page_width_in)
        section.page_height = Inches(page_height_in)
        top, bottom, left, right = margins_in
        section.top_margin, section.bottom_margin = Inches(top), Inches(bottom)
        section.left_margin, section.right_margin = Inches(left), Inches(right)
        self.content_width_in = page_width_in - left - right

        normal = self.doc.styles['Normal']
        normal.font.name = _BODY_FONT
        normal.font.size = Pt(9)
        normal.paragraph_format.space_before = Pt(0)
        normal.paragraph_format.space_after = Pt(0)

        self._page_of: dict[int, int] = {}
        self._current_page = 1

        if footer_lines:
            self._add_footer(section, footer_lines)

    # ── public ───────────────────────────────────────────────────────────────

    def render(self, story: list, page_of: dict[int, int] | None = None) -> 'StoryToDocx':
        """Emit ``story``. ``page_of`` maps id(flowable) -> PDF page number.

        With that map the Word file breaks pages wherever the PDF broke,
        including the breaks ReportLab chose by filling a page rather than the
        ones the story states. Without it only the explicit breaks carry over
        and Word paginates the rest itself.
        """
        self._page_of = page_of or {}
        self._current_page = 1
        for flowable in story:
            self._emit(flowable)
        return self

    def _sync_page(self, flowable) -> None:
        """Break if the PDF had moved on to a later page by this flowable."""
        target = self._page_of.get(id(flowable))
        if target is None or target <= self._current_page:
            return
        for _ in range(target - self._current_page):
            self._page_break()
        self._current_page = target

    def save(self, path: str) -> str:
        self.doc.save(path)
        return path

    # ── flowable dispatch ────────────────────────────────────────────────────

    def _emit(self, flowable) -> None:
        self._sync_page(flowable)
        if isinstance(flowable, KeepTogether):
            # Word has no equivalent that spans arbitrary content; the parts are
            # emitted in order and "keep with next" is set so Word makes the
            # same effort ReportLab did.
            parts = getattr(flowable, '_content', []) or []
            for part in parts:
                self._emit(part)
            return
        if isinstance(flowable, PageBreak):
            self._explicit_page_break()
        elif isinstance(flowable, Paragraph):
            self._paragraph(flowable)
        elif isinstance(flowable, Table):
            self._table(flowable)
        elif isinstance(flowable, Image):
            self._image(flowable)
        elif isinstance(flowable, Spacer):
            self._spacer(flowable)
        # Anything else (dividers, custom flowables) has no textual content to
        # carry across and is skipped rather than guessed at.

    # ── element writers ──────────────────────────────────────────────────────

    def _page_break(self) -> None:
        self.doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    def _explicit_page_break(self) -> None:
        """A PageBreak flowable. Advances the tracked page so the map stays aligned."""
        self._page_break()
        self._current_page += 1

    def _spacer(self, spacer: Spacer) -> None:
        p = self.doc.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        # A Spacer is pure vertical distance; an empty paragraph of exactly that
        # height reproduces it without introducing a blank line of body text.
        run = p.add_run()
        run.font.size = Pt(max(1.0, float(spacer.height) * 0.75))

    def _paragraph(self, para: Paragraph, container=None) -> None:
        style = para.style
        target = container if container is not None else self.doc
        p = target.add_paragraph()
        fmt = p.paragraph_format
        fmt.space_before = Pt(float(getattr(style, 'spaceBefore', 0) or 0))
        fmt.space_after = Pt(float(getattr(style, 'spaceAfter', 0) or 0))
        leading = float(getattr(style, 'leading', 0) or 0)
        if leading:
            fmt.line_spacing = Pt(leading)
        fmt.alignment = _ALIGN.get(getattr(style, 'alignment', None))
        if getattr(style, 'keepWithNext', 0):
            fmt.keep_with_next = True
        left_indent = float(getattr(style, 'leftIndent', 0) or 0)
        if left_indent:
            fmt.left_indent = Pt(left_indent)
        # A negative first-line indent is a hanging indent — used by the numbered
        # reference list, whose markers would otherwise sit flush with the
        # wrapped text and stop being scannable.
        first_line = float(getattr(style, 'firstLineIndent', 0) or 0)
        if first_line:
            fmt.first_line_indent = Pt(first_line)

        size = float(getattr(style, 'fontSize', 9) or 9)
        base_colour = _reportlab_colour_hex(getattr(style, 'textColor', None))
        style_bold = 'bold' in str(getattr(style, 'fontName', '')).lower()

        for text, bold, italic, colour in _plain_runs(getattr(para, 'text', '')):
            run = p.add_run(text)
            run.font.name = _BODY_FONT
            run.font.size = Pt(size)
            run.bold = bool(bold or style_bold)
            run.italic = bool(italic)
            rgb = _hex_to_rgb(colour) or _hex_to_rgb('#' + base_colour if base_colour else None)
            if rgb is not None:
                run.font.color.rgb = rgb
        return p

    @staticmethod
    def _image_stream(image: Image) -> io.BytesIO | None:
        """The PNG behind a ReportLab Image flowable, as a fresh stream.

        ``Image.filename`` cannot be used: given a BytesIO, ReportLab stores
        ``str(buffer)`` — the repr — so the attribute holds a string that is not
        a path and not the data. ``_png_bytes`` is attached by the figure
        renderer for exactly this reason; a real file path is still honoured.
        """
        blob = getattr(image, '_png_bytes', None)
        if isinstance(blob, (bytes, bytearray)):
            return io.BytesIO(bytes(blob))
        name = getattr(image, 'filename', None)
        if isinstance(name, str) and os.path.isfile(name):
            with open(name, 'rb') as fh:
                return io.BytesIO(fh.read())
        return None

    def _image(self, image: Image) -> None:
        stream = self._image_stream(image)
        if stream is None:
            return
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        # Exactly the printed size the PDF uses, so the figure occupies the same
        # area on the page in both documents.
        p.add_run().add_picture(
            stream,
            width=Inches(float(image.drawWidth) / 72.0),
            height=Inches(float(image.drawHeight) / 72.0),
        )

    def _table(self, table: Table) -> None:
        values = table._cellvalues
        if not values:
            return
        nrows, ncols = len(values), len(values[0])
        idx = _TableStyleIndex(table)

        # A one-row, one-column table whose cell holds flowables is a layout
        # wrapper (the side-by-side figure blocks), not tabular data. Emitting
        # it as a Word table would add borders around a figure that has none.
        docx_table = self.doc.add_table(rows=nrows, cols=ncols)
        docx_table.alignment = WD_TABLE_ALIGNMENT.CENTER
        docx_table.autofit = False

        col_widths = list(getattr(table, '_colWidths', None) or [])
        for c in range(ncols):
            width_pt = col_widths[c] if c < len(col_widths) and col_widths[c] else None
            if width_pt:
                for r in range(nrows):
                    docx_table.cell(r, c).width = Inches(float(width_pt) / 72.0)

        for r in range(nrows):
            for c in range(ncols):
                cell = docx_table.cell(r, c)
                cell.text = ''
                self._fill_cell(cell, values[r][c], idx, r, c)

                bg = idx.lookup('BACKGROUND', r, c)
                if bg is not None:
                    hexval = _reportlab_colour_hex(bg[3])
                    if hexval:
                        _shade_cell(cell, hexval)
                grid = idx.lookup('GRID', r, c) or idx.lookup('BOX', r, c)
                if grid is not None and len(grid) >= 5:
                    hexval = _reportlab_colour_hex(grid[4]) or 'D9D9D9'
                    _set_cell_borders(cell, hexval)

        for r0, c0, r1, c1 in idx.spans():
            try:
                docx_table.cell(r0, c0).merge(docx_table.cell(r1, c1))
            except Exception:
                continue

    def _fill_cell(self, cell, value, idx: _TableStyleIndex, r: int, c: int) -> None:
        """Write one cell's content, recursing into nested flowables."""
        items = value if isinstance(value, (list, tuple)) else [value]
        first = True
        for item in items:
            if isinstance(item, Paragraph):
                if first:
                    cell.paragraphs[0]._p.getparent().remove(cell.paragraphs[0]._p)
                    first = False
                self._paragraph(item, container=cell)
            elif isinstance(item, Image):
                stream = self._image_stream(item)
                if stream is not None:
                    p = cell.paragraphs[0] if first else cell.add_paragraph()
                    first = False
                    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    p.add_run().add_picture(
                        stream,
                        width=Inches(float(item.drawWidth) / 72.0),
                        height=Inches(float(item.drawHeight) / 72.0),
                    )
            elif isinstance(item, Table):
                # Nested layout table (side-by-side figure columns).
                self._nested_table(cell, item)
                first = False
            elif isinstance(item, Spacer):
                continue
            elif item is not None and str(item).strip():
                p = cell.paragraphs[0] if first else cell.add_paragraph()
                first = False
                size_cmd = idx.lookup('FONTSIZE', r, c)
                colour_cmd = idx.lookup('TEXTCOLOR', r, c)
                align_cmd = idx.lookup('ALIGN', r, c)
                run = p.add_run(str(item))
                run.font.name = _BODY_FONT
                run.font.size = Pt(float(size_cmd[3]) if size_cmd else 9)
                font_cmd = idx.lookup('FONTNAME', r, c)
                if font_cmd and 'bold' in str(font_cmd[3]).lower():
                    run.bold = True
                if colour_cmd:
                    rgb = _hex_to_rgb('#' + (_reportlab_colour_hex(colour_cmd[3]) or ''))
                    if rgb is not None:
                        run.font.color.rgb = rgb
                if align_cmd:
                    p.alignment = {'CENTER': WD_ALIGN_PARAGRAPH.CENTER,
                                   'RIGHT': WD_ALIGN_PARAGRAPH.RIGHT,
                                   'LEFT': WD_ALIGN_PARAGRAPH.LEFT}.get(
                        str(align_cmd[3]).upper())

    def _nested_table(self, cell, table: Table) -> None:
        values = table._cellvalues
        if not values:
            return
        nested = cell.add_table(rows=len(values), cols=len(values[0]))
        nested.autofit = False
        idx = _TableStyleIndex(table)
        for r, row in enumerate(values):
            for c, val in enumerate(row):
                target = nested.cell(r, c)
                target.text = ''
                self._fill_cell(target, val, idx, r, c)

    # ── footer ───────────────────────────────────────────────────────────────

    @staticmethod
    def _add_page_number_field(paragraph) -> None:
        """Insert a live Word PAGE field.

        The PDF footer reads "Page 3 | …" because ReportLab knows the page it is
        drawing. Word only knows at render time, so the number has to be a field
        rather than text — and it stays correct if the document is edited.
        """
        run = paragraph.add_run()
        begin = OxmlElement('w:fldChar'); begin.set(qn('w:fldCharType'), 'begin')
        instr = OxmlElement('w:instrText'); instr.set(qn('xml:space'), 'preserve')
        instr.text = ' PAGE '
        end = OxmlElement('w:fldChar'); end.set(qn('w:fldCharType'), 'end')
        for el in (begin, instr, end):
            run._r.append(el)
        return run

    def _add_footer(self, section, lines: tuple[str, ...]) -> None:
        footer = section.footer
        # The first paragraph exists already; reuse it so no blank line appears.
        para = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
        grey = RGBColor(0x80, 0x80, 0x80)
        for i, line in enumerate(lines):
            p = para if i == 0 else footer.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            size = Pt(8 if i == 0 else 7)
            if i == 0:
                # "Page N | <line>", matching the PDF's first footer line.
                lead = p.add_run('Page ')
                lead.font.name, lead.font.size, lead.font.color.rgb = _BODY_FONT, size, grey
                num = self._add_page_number_field(p)
                num.font.name, num.font.size, num.font.color.rgb = _BODY_FONT, size, grey
                line = ' | ' + line
            run = p.add_run(line)
            run.font.name = _BODY_FONT
            run.font.size = size
            run.font.color.rgb = grey


def render_story_to_docx(story: list, output_path: str, *,
                         page_width_in: float, page_height_in: float,
                         margins_in: tuple[float, float, float, float],
                         footer_lines: tuple[str, ...] = (),
                         page_of: dict[int, int] | None = None) -> str:
    """Render ``story`` to a Word file at ``output_path``. Returns the path."""
    return (StoryToDocx(page_width_in=page_width_in, page_height_in=page_height_in,
                        margins_in=margins_in, footer_lines=footer_lines)
            .render(story, page_of=page_of)
            .save(output_path))


class PageTrackingDocTemplate(SimpleDocTemplate):
    """A doc template that records which page each flowable landed on.

    ReportLab decides most page breaks by filling the frame, and those decisions
    are invisible to anything downstream. Recording them during the PDF build
    lets the Word renderer reproduce the same pagination instead of leaving Word
    to lay the content out again and land on a different page count.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.flowable_pages: dict[int, int] = {}

    def afterFlowable(self, flowable):  # noqa: N802 - ReportLab's spelling
        self.flowable_pages[id(flowable)] = self.page
