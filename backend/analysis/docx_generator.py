# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""
Word Document Report Generator for Noise Analysis
Generates publication-grade .docx reports with all analysis data
"""

from docx import Document
from docx.shared import Pt, RGBColor, Inches, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.style import WD_STYLE_TYPE
from datetime import datetime
import os
import re
import numpy as np


class WordReportGenerator:
    def __init__(self, analysis_data, standards_data, daily_summary=None, hourly_summary=None,
                 device_id: str = '', source_files: list | None = None,
                 merge_gap_report: dict | None = None, filepath: str = '',
                 deidentify: bool = True):
        self.analysis = analysis_data or {}
        self.standards = standards_data or {}
        self.daily_summary = daily_summary
        self.hourly_summary = hourly_summary

        # This generator is a SEPARATE render path from ReportGeneratorV2 and was
        # therefore not covered by that class's de-identification: a .docx built
        # from the same request still printed the participant's name in the
        # title-page metadata. Apply the same rule here, from the same helpers,
        # so the two paths cannot drift apart again.
        from analysis.report_generator_v2 import deidentify_label, is_safe_label
        self.deidentify = bool(deidentify)
        _raw_device = str(device_id or '').strip()
        _raw_sources = [str(f) for f in (source_files or [])]

        if self.deidentify:
            self.device_id = (deidentify_label(_raw_device,
                                               'Monitoring location (identifier withheld)')[0]
                              if _raw_device else '')
            self.source_files = []
            for i, name in enumerate(_raw_sources, start=1):
                stem = os.path.splitext(os.path.basename(name))[0]
                stem = re.sub(r'^\d{8}_\d{6}_', '', stem)
                self.source_files.append(stem if is_safe_label(stem) else f'Source file {i}')
        else:
            self.device_id = _raw_device
            self.source_files = _raw_sources
        self.merge_gap_report = merge_gap_report
        self.filepath = filepath
        self.doc = Document()
        
    def _primary_stats(self) -> dict:
        """Statistics for the LEQ stream.

        ``next(iter(statistics.values()))`` returned whichever column came first
        in the file. These loggers export L-Max, LEQ, L-Min in that order, so the
        report was publishing the L-MAX column's figures under the labels LAeq,
        LAmin and LAmax: 53.90 dB reported as LAeq where the true LEQ energy
        average was 52.78 dB.
        """
        stats = self.analysis.get('statistics') or {}
        if not stats:
            return {}

        def _norm(c):
            return ''.join(ch for ch in str(c).lower() if ch.isalnum())

        for col in stats:
            n = _norm(col)
            if ('leq' in n or 'laeq' in n) and 'max' not in n and 'min' not in n:
                return stats[col] or {}
        # No LEQ-like column: prefer one that is neither L-Max nor L-Min.
        for col in stats:
            n = _norm(col)
            if 'max' not in n and 'min' not in n:
                return stats[col] or {}
        return next(iter(stats.values()), {}) or {}

    def _stats_for(self, kind: str) -> dict:
        """Statistics for the 'lmax' or 'lmin' stream, empty if absent."""
        stats = self.analysis.get('statistics') or {}
        for col in stats:
            n = ''.join(ch for ch in str(col).lower() if ch.isalnum())
            if kind in n:
                return stats[col] or {}
        return {}

    def _source_label(self) -> str:
        """De-identified label for the source file.

        The .docx title page printed the raw filename, which is the most common
        carrier of a participant's name. Text-level checks over the body missed
        it because it sits in the metadata paragraph.
        """
        from analysis.report_generator_v2 import is_safe_label
        stem = os.path.splitext(os.path.basename(self.filepath or ""))[0]
        stem = re.sub(r"^\d{8}_\d{6}_", "", stem)
        if not self.deidentify or is_safe_label(stem):
            return stem or "not recorded"
        return self.device_id or "withheld"

    def generate(self):
        """Generate complete Word report."""
        self._add_title_page()
        self._add_executive_summary()
        self._add_acoustic_metrics()
        self._add_daily_summary()
        self._add_hourly_summary()
        self._add_statistical_analysis()
        self._add_standards_compliance()
        self._add_who_guidelines()
        self._add_recommendations()
        self._add_disclaimer_section()
        
        return self.doc
    
    def _add_title_page(self):
        """Add title page with header and metadata."""
        title = self.doc.add_heading('Noise Analysis Report', 0)
        title.alignment = WD_ALIGN_PARAGRAPH.CENTER
        title_format = title.runs[0].font
        title_format.size = Pt(28)
        title_format.color.rgb = RGBColor(0, 102, 255)
        
        subtitle = self.doc.add_heading('WHO 2018 Health-Based Assessment', level=2)
        subtitle.alignment = WD_ALIGN_PARAGRAPH.CENTER
        
        self.doc.add_paragraph()  # Spacer

        # Metadata block
        metadata_para = self.doc.add_paragraph()
        metadata_para.alignment = WD_ALIGN_PARAGRAPH.CENTER

        # A literal "\n" inside a run is not a line break in Word: the title block
        # rendered as "Device / Location ID: Home ASource File: Home AGenerated:".
        # add_break() emits a real <w:br/>.
        def _meta_line(text, size=11, bold=False):
            r = metadata_para.add_run(text)
            r.font.size = Pt(size)
            r.font.bold = bold
            metadata_para.add_run().add_break()

        if self.device_id:
            _meta_line(f"Device / Location ID: {self.device_id}", size=12, bold=True)

        import os as _os
        if self.source_files:
            _meta_line(f"Source Files ({len(self.source_files)}): " + ", ".join(self.source_files))
        elif self.filepath:
            _meta_line(f"Source File: {self._source_label()}")

        _meta_line(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        _meta_line("Platform: Noise Analysis Platform v1.0")
        metadata_para.add_run("Standard: WHO 2018 Environmental Noise Guidelines").font.size = Pt(11)

        # Data loss warning on title page (for merged datasets)
        if self.merge_gap_report and self.merge_gap_report.get('gap_count', 0) > 0:
            self.doc.add_paragraph()
            warn_para = self.doc.add_paragraph()
            warn_para.alignment = WD_ALIGN_PARAGRAPH.CENTER
            gd = self.merge_gap_report
            missing_min = round(gd.get('missing_seconds', 0) / 60.0, 1)
            warn_run = warn_para.add_run(
                f"⚠ DATA LOSS NOTED: {gd['gap_count']} gap(s) detected in merged dataset — "
                f"{missing_min} minutes missing. See Section 2 for full continuity log."
            )
            warn_run.font.size = Pt(10)
            warn_run.font.bold = True
            warn_run.font.color.rgb = RGBColor(0xB9, 0x1C, 0x1C)

        self.doc.add_page_break()
    
    def _add_executive_summary(self):
        """Add executive summary section."""
        heading = self.doc.add_heading('Executive Summary', level=1)
        heading.runs[0].font.color.rgb = RGBColor(0, 102, 255)
        
        # Key findings
        if self.analysis.get('statistics'):
            stats = self._primary_stats()
            mean_val = stats.get('laeq_db', stats.get('mean', 0))
            
            summary_text = f"""This noise analysis report presents a comprehensive assessment of acoustic environment 
data against WHO 2018 Environmental Noise Guidelines. The measurement campaign captured noise levels with an energy-average 
(LAeq) of {self._fmt(mean_val, 1)} dB(A), providing insights into environmental noise exposure and health-relevant metrics.

Key metrics are compared against WHO guideline thresholds:
• Lden (Day-Evening-Night): 53 dB for road traffic (guideline)
• Lnight (Night-time): 45 dB (guideline)
• Sleep disturbance threshold: 60 dB at façade

This report includes detailed statistical analysis, compliance assessment, and science-based recommendations 
for noise mitigation and health protection."""
            
            self.doc.add_paragraph(summary_text)
    
    def _add_acoustic_metrics(self):
        """Add core acoustic metrics section."""
        heading = self.doc.add_heading('Core Acoustic Metrics', level=1)
        heading.runs[0].font.color.rgb = RGBColor(0, 102, 255)
        
        if not self.analysis.get('statistics'):
            self.doc.add_paragraph("No statistical data available.")
            return
        
        stats = self._primary_stats()
        
        # LAmax and LAmin must come from their own streams, not from the LEQ
        # column's extremes. The loudest LEQ interval is an interval average and
        # is always below the true instantaneous peak.
        lmax_stats = self._stats_for('lmax')
        lmin_stats = self._stats_for('lmin')
        n_samples = (self.analysis.get('data_summary') or {}).get('total_records')

        metrics = [
            ('LAeq (energy average, LEQ stream)',
             f"{self._fmt(stats.get('laeq_db', stats.get('mean')), 1)} dB(A)"),
            ('Highest LEQ interval',
             f"{self._fmt(stats.get('max'), 1)} dB(A)"),
            ('Lowest LEQ interval',
             f"{self._fmt(stats.get('min'), 1)} dB(A)"),
            ('LAmax (highest single level)',
             f"{self._fmt(lmax_stats.get('max'), 1)} dB(A)" if lmax_stats else 'Not recorded'),
            ('LAmin (lowest single level)',
             f"{self._fmt(lmin_stats.get('min'), 1)} dB(A)" if lmin_stats else 'Not recorded'),
            ('Median (L50)', f"{self._fmt(stats.get('median'), 1)} dB(A)"),
            ('Standard Deviation', f"{self._fmt(stats.get('std_dev'), 2)} dB"),
            # Coefficient of variation is omitted: decibels are an interval scale
            # with an arbitrary zero, so std/mean carries no physical meaning.
            ('Measurement Count',
             f"{n_samples:,} samples" if isinstance(n_samples, int) else 'N/A'),
        ]

        table = self.doc.add_table(rows=len(metrics) + 1, cols=2)
        table.style = 'Light Grid Accent 1'
        
        # Header
        hdr_cells = table.rows[0].cells
        hdr_cells[0].text = 'Metric'
        hdr_cells[1].text = 'Value'


        for i, (label, value) in enumerate(metrics, 1):
            row_cells = table.rows[i].cells
            row_cells[0].text = label
            row_cells[1].text = value
    
    def _add_daily_summary(self):
        """Add daily summary table."""
        heading = self.doc.add_heading('Daily Summary', level=1)
        heading.runs[0].font.color.rgb = RGBColor(0, 102, 255)
        
        if self.daily_summary is None or len(self.daily_summary) == 0:
            self.doc.add_paragraph("No daily summary data available.")
            return
        
        # Create table
        table = self.doc.add_table(rows=1 + len(self.daily_summary), cols=8)
        table.style = 'Light Grid Accent 1'
        
        # Header
        headers = ['Date', 'LAeq (24h)', 'Min (dB)', 'Max (dB)', 'Std Dev', 'Daytime', 'Nighttime', 'Lden']
        hdr_cells = table.rows[0].cells
        for i, header in enumerate(headers):
            hdr_cells[i].text = header
        
        # Data rows
        for i, (_, row) in enumerate(self.daily_summary.iterrows(), 1):
            cells = table.rows[i].cells
            cells[0].text = str(row.get('Date', ''))
            cells[1].text = self._fmt(row.get('Average_L_EQ_dB'), 1)
            cells[2].text = self._fmt(row.get('Min_L_EQ_dB'), 1)
            cells[3].text = self._fmt(row.get('Max_L_EQ_dB'), 1)
            cells[4].text = self._fmt(row.get('Std_Dev', row.get('Std_Dev_dB')), 2)
            cells[5].text = self._fmt(row.get('Daytime_LAeq'), 1)
            cells[6].text = self._fmt(row.get('Nighttime_LAeq'), 1)
            cells[7].text = self._fmt(row.get('Daily_Lden'), 1)
    
    def _add_hourly_summary(self):
        """Add hourly summary table."""
        heading = self.doc.add_heading('Hourly Profile (24-Hour Distribution)', level=1)
        heading.runs[0].font.color.rgb = RGBColor(0, 102, 255)
        
        if self.hourly_summary is None or len(self.hourly_summary) == 0:
            self.doc.add_paragraph("No hourly summary data available.")
            return
        
        # Create table
        table = self.doc.add_table(rows=1 + len(self.hourly_summary), cols=5)
        table.style = 'Light Grid Accent 1'
        
        # Header
        headers = ['Hour', 'LAeq (dB)', 'Min (dB)', 'Max (dB)', 'Std Dev']
        hdr_cells = table.rows[0].cells
        for i, header in enumerate(headers):
            hdr_cells[i].text = header
        
        # Data rows
        for i, (_, row) in enumerate(self.hourly_summary.iterrows(), 1):
            cells = table.rows[i].cells
            hour = int(row.get('Hour', i-1))
            cells[0].text = f"{str(hour).zfill(2)}:00"
            cells[1].text = self._fmt(row.get('Average_L_EQ_dB'), 1)
            cells[2].text = self._fmt(row.get('Min_L_EQ_dB'), 1)
            cells[3].text = self._fmt(row.get('Max_L_EQ_dB'), 1)
            cells[4].text = self._fmt(row.get('Std_Dev', row.get('Std_Dev_dB')), 2)
    
    def _add_statistical_analysis(self):
        """Add statistical analysis section."""
        heading = self.doc.add_heading('Statistical Analysis', level=1)
        heading.runs[0].font.color.rgb = RGBColor(0, 102, 255)
        
        if not self.analysis.get('statistics'):
            self.doc.add_paragraph("No statistical data available.")
            return
        
        percentiles = self.analysis.get('percentiles', {})
        stats = self._primary_stats()
        first_col = next(iter(self.analysis.get('statistics', {}).keys()), 'LEQ')
        pcts = percentiles.get(first_col, {})
        
        self.doc.add_heading('Percentile Distribution', level=2)
        
        table = self.doc.add_table(rows=6, cols=2)
        table.style = 'Light Grid Accent 1'
        
        percentile_data = [
            ('L5 (Exceeded 5% of time)', pcts.get('L5')),
            ('L10 (Exceeded 10% of time)', pcts.get('L10')),
            ('L50 (Median)', pcts.get('L50')),
            ('L90 (Exceeded 90% of time)', pcts.get('L90')),
            ('L95 (Exceeded 95% of time)', pcts.get('L95')),
        ]
        
        hdr_cells = table.rows[0].cells
        hdr_cells[0].text = 'Percentile'
        hdr_cells[1].text = 'Value (dB)'
        
        for i, (label, value) in enumerate(percentile_data, 1):
            cells = table.rows[i].cells
            cells[0].text = label
            cells[1].text = self._fmt(value, 1)
    
    def _add_standards_compliance(self):
        """Add standards compliance section."""
        heading = self.doc.add_heading('Standards & Regulatory Compliance', level=1)
        heading.runs[0].font.color.rgb = RGBColor(0, 102, 255)
        
        compliance = self.analysis.get('compliance', {})
        if not compliance:
            self.doc.add_paragraph("No compliance data available.")
            return
        
        # Add compliance summary
        current_data = next(iter(compliance.values()), {})
        
        self.doc.add_heading('Measured Levels vs. Guidelines', level=2)
        
        table = self.doc.add_table(rows=5, cols=2)
        table.style = 'Light Grid Accent 1'
        
        hdr_cells = table.rows[0].cells
        hdr_cells[0].text = 'Metric'
        hdr_cells[1].text = 'Measured Value'
        
        measured_data = [
            ('LAeq (Current)', current_data.get('current_leq')),
            ('Lden (24-hour)', current_data.get('current_Lden')),
            ('Lnight (Sleep)', current_data.get('current_Lnight')),
            ('LAeq 24h', current_data.get('current_LAeq_24h')),
        ]
        
        for i, (label, value) in enumerate(measured_data, 1):
            cells = table.rows[i].cells
            cells[0].text = label
            cells[1].text = self._fmt(value, 1) if value else 'N/A'
    
    def _add_who_guidelines(self):
        """Add WHO 2018 guidelines reference."""
        heading = self.doc.add_heading('WHO 2018 Environmental Noise Guidelines', level=1)
        heading.runs[0].font.color.rgb = RGBColor(0, 102, 255)
        
        who_text = """The WHO 2018 Environmental Noise Guidelines provide evidence-based recommendations 
for protecting public health from environmental noise. Key guideline values for road traffic:

• Lden ≤ 53 dB: Road traffic noise (Day-Evening-Night noise level)
• Lnight ≤ 45 dB: Nighttime noise level for sleep protection
• Sleep disturbance threshold: 60 dB peak at façade

These guidelines are based on extensive epidemiological evidence linking environmental noise exposure 
to cardiovascular disease, cognitive impairment in children, sleep disturbance, and annoyance.

Interpretation:
• Green (≤ Guideline): No significant health effects expected at this exposure level
• Yellow (Guideline - 5 dB): Caution; health effects possible at elevated levels
• Orange (5-10 dB above): Warning; significant health effects likely
• Red (> 10 dB above): Critical; substantial health effects and immediate action recommended"""
        
        self.doc.add_paragraph(who_text)
    
    def _add_recommendations(self):
        """Add recommendations section."""
        heading = self.doc.add_heading('Health-Based Recommendations', level=1)
        heading.runs[0].font.color.rgb = RGBColor(0, 102, 255)
        
        recommendations = self.analysis.get('interpretations', [])
        
        if not recommendations:
            self.doc.add_paragraph("No specific recommendations available.")
            return
        
        for rec in recommendations:
            self.doc.add_paragraph(rec, style='List Bullet')
        
        # Add general recommendations
        self.doc.add_paragraph()
        general_recs = self.doc.add_heading('General Mitigation Strategies', level=2)
        
        strategies = [
            'Source reduction: Install noise barriers or modify operational practices',
            'Path modification: Create buffer zones or use sound-absorbing materials',
            'Receiver protection: Upgrade building insulation or provide hearing protection',
            'Urban planning: Implement noise zoning in sensitive areas (schools, hospitals, residential)',
            'Community engagement: Provide noise monitoring data and health impact information',
        ]
        
        for strategy in strategies:
            self.doc.add_paragraph(strategy, style='List Bullet')
    
    def _add_disclaimer_section(self):
        """Add Methodological Limitations & Disclaimer section."""
        self.doc.add_page_break()
        
        heading = self.doc.add_heading('Methodological Limitations & Disclaimer', level=1)
        heading.runs[0].font.color.rgb = RGBColor(0, 102, 255)
        
        # Source Attribution
        subheading1 = self.doc.add_heading('Source Attribution', level=2)
        self.doc.add_paragraph(
            "Acoustic sensors measure total environmental energy; they do not definitively identify specific noise sources "
            "(e.g., distinguishing a commercial aircraft from a heavy goods vehicle). Source-specific compliance requires "
            "cross-referencing with external databases (e.g., ADS-B flight tracking)."
        )
        
        # Health vs. Legal Limits
        subheading2 = self.doc.add_heading('Health vs. Legal Limits', level=2)
        self.doc.add_paragraph(
            "This report evaluates data against both biological health guidelines (WHO 2018) and local regulatory limits "
            "(e.g., Maryland COMAR). Passing local legal zoning limits does not inherently guarantee the absence of adverse "
            "physiological health impacts."
        )
        
        # Equipment Calibration
        subheading3 = self.doc.add_heading('Equipment Calibration', level=2)
        self.doc.add_paragraph(
            "The accuracy of these metrics is strictly dependent on the proper deployment, windshielding, and recent acoustic "
            "calibration of the monitoring hardware."
        )
        
        # Not Medical Advice
        subheading4 = self.doc.add_heading('Not Medical Advice', level=2)
        self.doc.add_paragraph(
            "This data is for environmental and epidemiological research purposes. It does not constitute a clinical medical "
            "diagnosis or formal legal counsel."
        )
    
    def _fmt(self, value, decimals=1, default='N/A'):
        """Format numeric value safely."""
        try:
            num = float(value)
            if np.isnan(num) or np.isinf(num):
                return default
            return f"{num:.{decimals}f}"
        except (TypeError, ValueError):
            return default
