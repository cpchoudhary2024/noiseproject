# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""Render a ReportLab story to a Word document."""
import io
import os
import re
from xml.etree import ElementTree

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
from reportlab.platypus import (Image, KeepTogether, PageBreak, Paragraph,
                                SimpleDocTemplate, Spacer, Table)

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


def _plain_runs(markup):
    """Flatten ReportLab paragraph markup to (text, bold, italic, hex colour) runs."""
    text = markup or ''
    for entity, char in _ENTITIES.items():
        text = text.replace(entity, char)
    text = re.sub(r'<br\s*/?>', '\n', text)
    text = re.sub(r'&(?![a-zA-Z]+;|#\d+;)', '&amp;', text)

    try:
        root = ElementTree.fromstring(f'<root>{text}</root>')
    except ElementTree.ParseError:
        return [(re.sub(r'<[^>]+>', '', text), False, False, None)]

    runs = []

    def walk(node, bold, italic, colour):
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


def _hex_to_rgb(value):
    if not value:
        return None
    v = str(value).lstrip('#')
    if len(v) != 6:
        return None
    try:
        return RGBColor(int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    except ValueError:
        return None


def _reportlab_colour_hex(colour):
    try:
        return '{:02X}{:02X}{:02X}'.format(
            int(round(colour.red * 255)), int(round(colour.green * 255)),
            int(round(colour.blue * 255)))
    except AttributeError:
        return None


def _shade_cell(cell, hex_fill):
    shd = OxmlElement('w:shd')
    shd.set(qn('w:val'), 'clear')
    shd.set(qn('w:color'), 'auto')
    shd.set(qn('w:fill'), hex_fill)
    cell._tc.get_or_add_tcPr().append(shd)


def _set_cell_borders(cell, hex_colour, eighths=4):
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

    def __init__(self, table):
        self._cmds = []
        style = getattr(table, '_bkgrndcmds', None)
        for source in (getattr(table, '_cellstyles', None),):
            del source
        raw = list(getattr(table, '_commands', []) or [])
        raw += list(style or [])
        self._cmds = raw
        self._nrows = len(table._cellvalues)
        self._ncols = len(table._cellvalues[0]) if table._cellvalues else 0

    @staticmethod
    def _norm(idx, n):
        return idx if idx >= 0 else n + idx

    def _covers(self, cmd, row, col):
        try:
            (c0, r0), (c1, r1) = cmd[1], cmd[2]
        except (IndexError, TypeError, ValueError):
            return False
        r0, r1 = self._norm(r0, self._nrows), self._norm(r1, self._nrows)
        c0, c1 = self._norm(c0, self._ncols), self._norm(c1, self._ncols)
        return min(r0, r1) <= row <= max(r0, r1) and min(c0, c1) <= col <= max(c0, c1)

    def lookup(self, name, row, col):
        """Last matching command wins, as in ReportLab."""
        found = None
        for cmd in self._cmds:
            if cmd and str(cmd[0]).upper() == name and self._covers(cmd, row, col):
                found = cmd
        return found

    def spans(self):
        out = []
        for cmd in self._cmds:
            if cmd and str(cmd[0]).upper() == 'SPAN':
                (c0, r0), (c1, r1) = cmd[1], cmd[2]
                out.append((self._norm(r0, self._nrows), self._norm(c0, self._ncols),
                            self._norm(r1, self._nrows), self._norm(c1, self._ncols)))
        return out


class StoryToDocx:
    """Walk a ReportLab story and emit the equivalent Word document."""

    def __init__(self, *, page_width_in, page_height_in,
                 margins_in,
                 footer_lines=()):
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

        self._page_of = {}
        self._current_page = 1

        if footer_lines:
            self._add_footer(section, footer_lines)

    # public

    def render(self, story, page_of=None):
        """Emit ``story``."""
        self._page_of = page_of or {}
        self._current_page = 1
        for flowable in story:
            self._emit(flowable)
        return self

    def _sync_page(self, flowable):
        target = self._page_of.get(id(flowable))
        if target is None or target <= self._current_page:
            return
        for _ in range(target - self._current_page):
            self._page_break()
        self._current_page = target

    def save(self, path):
        self.doc.save(path)
        return path

    # flowable dispatch

    def _emit(self, flowable):
        self._sync_page(flowable)
        if isinstance(flowable, KeepTogether):
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

    # element writers

    def _page_break(self):
        self.doc.add_paragraph().add_run().add_break(WD_BREAK.PAGE)

    def _explicit_page_break(self):
        self._page_break()
        self._current_page += 1

    def _spacer(self, spacer):
        p = self.doc.add_paragraph()
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        run = p.add_run()
        run.font.size = Pt(max(1.0, float(spacer.height) * 0.75))

    def _paragraph(self, para, container=None):
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
    def _image_stream(image):
        blob = getattr(image, '_png_bytes', None)
        if isinstance(blob, (bytes, bytearray)):
            return io.BytesIO(bytes(blob))
        name = getattr(image, 'filename', None)
        if isinstance(name, str) and os.path.isfile(name):
            with open(name, 'rb') as fh:
                return io.BytesIO(fh.read())
        return None

    def _image(self, image):
        stream = self._image_stream(image)
        if stream is None:
            return
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        p.add_run().add_picture(
            stream,
            width=Inches(float(image.drawWidth) / 72.0),
            height=Inches(float(image.drawHeight) / 72.0),
        )

    def _table(self, table):
        values = table._cellvalues
        if not values:
            return
        nrows, ncols = len(values), len(values[0])
        idx = _TableStyleIndex(table)

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
            # a span Word can't merge just keeps its cells separate
            try:
                docx_table.cell(r0, c0).merge(docx_table.cell(r1, c1))
            except Exception:
                continue

    def _fill_cell(self, cell, value, idx, r, c):
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

    def _nested_table(self, cell, table):
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

    # footer

    @staticmethod
    def _add_page_number_field(paragraph):
        run = paragraph.add_run()
        begin = OxmlElement('w:fldChar'); begin.set(qn('w:fldCharType'), 'begin')
        instr = OxmlElement('w:instrText'); instr.set(qn('xml:space'), 'preserve')
        instr.text = ' PAGE '
        end = OxmlElement('w:fldChar'); end.set(qn('w:fldCharType'), 'end')
        for el in (begin, instr, end):
            run._r.append(el)
        return run

    def _add_footer(self, section, lines):
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


def render_story_to_docx(story, output_path, *,
                         page_width_in, page_height_in,
                         margins_in,
                         footer_lines=(),
                         page_of=None):
    """Render ``story`` to a Word file at ``output_path``."""
    return (StoryToDocx(page_width_in=page_width_in, page_height_in=page_height_in,
                        margins_in=margins_in, footer_lines=footer_lines)
            .render(story, page_of=page_of)
            .save(output_path))


class PageTrackingDocTemplate(SimpleDocTemplate):
    """A doc template that records which page each flowable landed on."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.flowable_pages = {}

    def afterFlowable(self, flowable):  # noqa: N802 - ReportLab's spelling
        self.flowable_pages[id(flowable)] = self.page
