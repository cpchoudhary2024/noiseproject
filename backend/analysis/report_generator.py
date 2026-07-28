import pandas as pd
import numpy as np
from datetime import datetime
import os
import json
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from analysis.noise_analyzer import NoiseAnalyzer
from analysis.iso_epa_standards import StandardsAnalyzer
from analysis.standards_reference import who_2018_environmental_noise_guideline_levels
from analysis.chart_generator import AdvancedChartGenerator
from analysis.acoustics import compute_ldn_lden, energetic_mean_db, exceedance_levels_db
import io
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY
from xml.sax.saxutils import escape

class ReportGenerator:
    """Generate comprehensive reports in various formats"""
    
    def __init__(self, df, filepath, analysis=None, standards=None):
        self.df = df.copy()
        self.filepath = filepath
        self.analyzer = NoiseAnalyzer(df)
        self.standards = StandardsAnalyzer(df)
        self._analysis = analysis
        self._standards = standards
        
        # Convert numeric columns
        for col in self.analyzer.noise_columns:
            self.df[col] = pd.to_numeric(self.df[col], errors='coerce')

    def _get_analysis(self):
        if self._analysis is None:
            self._analysis = self.analyzer.comprehensive_analysis()
        return self._analysis

    def _get_standards(self):
        if self._standards is None:
            self._standards = self.standards.analyze()
        return self._standards
    
    def generate_pdf_report(self, report_type='comprehensive', output_dir: str | None = None):
        """Generate a research-grade PDF report.

        This PDF follows a strict acoustic methodology and layout:
        - LEQ/LAeq stream ONLY: Lden/Ldn/Lnight + WHO 2018 comparisons
        - L-Max stream ONLY: nighttime LAmax + event exceedance counts
        - L-Min stream ONLY: baseline (no Lden/Ldn)
        """
        
        report_filename = f"noise_analysis_{report_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        report_dir = output_dir or os.path.dirname(self.filepath)
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, report_filename)

        doc = SimpleDocTemplate(report_path, pagesize=(11*inch, 8.5*inch), topMargin=0.5*inch, bottomMargin=0.5*inch, leftMargin=0.75*inch, rightMargin=0.75*inch)
        styles = self._get_pdf_styles()
        story = []

        # Build PDF story (strict 5-section layout)
        self._add_pdf_header(story, styles, report_type)

        ts_col, leq_col, lmax_col, lmin_col = self._resolve_acoustic_columns()
        ts = self._get_timestamp_series(ts_col)

        # Section 1
        self._add_pdf_section_executive_acoustic_summary(
            story,
            styles,
            ts=ts,
            leq_col=leq_col,
        )

        # Section 2
        self._add_pdf_section_regulatory_health_compliance(
            story,
            styles,
            ts=ts,
            leq_col=leq_col,
        )

        # Section 3
        self._add_pdf_section_sleep_disturbance_lmax(
            story,
            styles,
            ts=ts,
            lmax_col=lmax_col,
        )

        # Section 4
        self._add_pdf_section_statistical_profile_percentiles(
            story,
            styles,
            leq_col=leq_col,
        )

        # Section 5
        self._add_pdf_section_visualizations(
            story,
            styles,
            ts=ts,
            leq_col=leq_col,
            lmax_col=lmax_col,
            lmin_col=lmin_col,
        )
        
        doc.build(story, onFirstPage=self._add_page_template, onLaterPages=self._add_page_template)
        
        return report_path

    # -----------------------------
    # Strict PDF helpers (formatting + column mapping)
    # -----------------------------

    @staticmethod
    def _fmt_float(value, decimals: int = 2) -> str:
        try:
            if value is None:
                return "N/A"
            v = float(value)
            if not np.isfinite(v):
                return "N/A"
            return f"{v:.{decimals}f}"
        except Exception:
            return "N/A"

    @classmethod
    def _fmt_db(cls, value) -> str:
        s = cls._fmt_float(value, 2)
        return "N/A" if s == "N/A" else f"{s} dB(A)"

    @classmethod
    def _fmt_db_plain(cls, value) -> str:
        # For table cells where the unit is already in the header.
        return cls._fmt_float(value, 2)

    @staticmethod
    def _status_pass_fail(limit_db: float, measured_db) -> str:
        try:
            if measured_db is None:
                return "N/A"
            v = float(measured_db)
            if not np.isfinite(v):
                return "N/A"
            return "PASS" if v <= float(limit_db) else "FAIL"
        except Exception:
            return "N/A"

    @staticmethod
    def _exceedance_amount(limit_db: float, measured_db):
        try:
            if measured_db is None:
                return None
            v = float(measured_db)
            if not np.isfinite(v):
                return None
            return max(0.0, v - float(limit_db))
        except Exception:
            return None

    def _check_who_compliance_failures(self, ts: pd.Series, leq: pd.Series) -> bool:
        """Check if ANY WHO 2018 guideline is exceeded.
        
        Returns True if Lden > 53 dB OR Lnight > 45 dB.
        """
        try:
            env = compute_ldn_lden(ts, leq) or {}
            lden = env.get('Lden')
            lnight = env.get('Lnight')
            
            if lden is not None and float(lden) > 53.0:
                return True
            if lnight is not None and float(lnight) > 45.0:
                return True
            
            return False
        except Exception:
            return False

    def _resolve_acoustic_columns(self):
        """Map dataset columns into LEQ / L-Max / L-Min streams.

        LEQ (aka LAeq/Leq) is the ONLY stream used for Lden/Ldn/Lnight + WHO 2018.
        """

        def _norm(name: str) -> str:
            return "".join(ch for ch in (name or "").lower() if ch.isalnum())

        # Timestamp — single authoritative resolver (never positional).
        from analysis.timestamp_utils import resolve_time_column
        ts_col = resolve_time_column(self.df)

        # Noise columns: reuse analyzer detection but keep stable ordering.
        noise_cols = list(self.analyzer.noise_columns)

        # Find Lmax/Lmin explicitly.
        lmax_col = None
        lmin_col = None
        for c in noise_cols:
            n = _norm(c)
            if lmax_col is None and (n.startswith("lmax") or "lmax" in n or n == "max" or n.endswith("maxdba") or "lmaxdba" in n):
                lmax_col = c
            if lmin_col is None and (n.startswith("lmin") or "lmin" in n or n == "min" or n.endswith("mindba") or "lmindba" in n):
                lmin_col = c

        # LEQ column: prefer explicit LEQ/LAeq.
        leq_candidates = []
        for c in noise_cols:
            n = _norm(c)
            if c == lmax_col or c == lmin_col:
                continue
            if "laeq" in n or "leq" in n:
                leq_candidates.append(c)

        if leq_candidates:
            leq_col = leq_candidates[0]
        else:
            # Best-effort fallback: pick the first noise column that's not Lmax/Lmin.
            non_peak = [c for c in noise_cols if c != lmax_col and c != lmin_col]
            leq_col = non_peak[0] if non_peak else (noise_cols[0] if noise_cols else None)

        return ts_col, leq_col, lmax_col, lmin_col

    def _get_timestamp_series(self, ts_col: str | None) -> pd.Series:
        if ts_col and ts_col in self.df.columns:
            from analysis.timestamp_utils import parse_timestamps_robust
            ts, _ = parse_timestamps_robust(self.df[ts_col])
            if ts.notna().sum() > 0:
                return ts
        # Fall back to an index-based timeline if none exists.
        return pd.Series(pd.date_range(start=datetime.now(), periods=len(self.df), freq="s"))

    def _get_numeric_series(self, col: str | None) -> pd.Series:
        if not col or col not in self.df.columns:
            return pd.Series(dtype=float)
        return pd.to_numeric(self.df[col], errors="coerce")

    # -----------------------------
    # Section 1
    # -----------------------------

    def _add_pdf_section_executive_acoustic_summary(self, story, styles, *, ts: pd.Series, leq_col: str | None):
        story.append(Paragraph("Section 1: Executive Acoustic Summary", styles['h1']))

        ts_valid = ts.dropna()
        if ts_valid.empty:
            date_range = "Unknown"
            duration = "Unknown"
        else:
            start = ts_valid.min()
            end = ts_valid.max()
            date_range = f"{start.strftime('%Y-%m-%d %H:%M:%S')} to {end.strftime('%Y-%m-%d %H:%M:%S')}"
            seconds = max(0.0, (end - start).total_seconds())
            hours = seconds / 3600.0
            duration = f"{hours:.2f} hours"

        story.append(Paragraph(f"Measurement Date Range: <b>{escape(date_range)}</b>", styles['BodyText']))
        story.append(Paragraph(f"Total Duration: <b>{escape(duration)}</b>", styles['BodyText']))
        story.append(Spacer(1, 0.12 * inch))

        leq = self._get_numeric_series(leq_col)
        if leq.dropna().empty:
            story.append(Paragraph("LEQ stream not available; cannot compute LAeq.", styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))
            return

        laeq = energetic_mean_db(leq)
        laeq_str = self._fmt_db(laeq)
        story.append(Paragraph(f"24-Hour Energy Average (LAeq) from LEQ: <b>{escape(laeq_str)}</b>", styles['BodyText']))

        # Check if any WHO guideline fails to determine qualitative descriptor
        who_fails = self._check_who_compliance_failures(ts, leq)

        # One-sentence interpretation
        try:
            laeq_v = float(laeq) if laeq is not None else float('nan')
        except Exception:
            laeq_v = float('nan')

        if np.isfinite(laeq_v):
            # If ANY WHO guideline fails, use the "exceeds" descriptor
            if who_fails:
                interp = f"The environment exhibits an average continuous noise level of {self._fmt_float(laeq_v)} dB(A), characterized by acoustic loading that exceeds WHO health guidelines."
            else:
                if laeq_v < 55:
                    loading = "low"
                elif laeq_v < 65:
                    loading = "moderate"
                elif laeq_v < 75:
                    loading = "high"
                else:
                    loading = "very high"
                interp = f"The environment exhibits an average continuous noise level of {self._fmt_float(laeq_v)} dB(A), characterized by {loading} acoustic loading."
        else:
            interp = "The environment exhibits an average continuous noise level that could not be computed due to missing/invalid LEQ values."

        story.append(Paragraph(escape(interp), styles['BodyText']))
        story.append(Spacer(1, 0.18 * inch))

    # -----------------------------
    # Section 2
    # -----------------------------

    def _add_pdf_section_regulatory_health_compliance(self, story, styles, *, ts: pd.Series, leq_col: str | None):
        story.append(Paragraph("Section 2: Regulatory & Health Compliance (LEQ only)", styles['h1']))

        leq = self._get_numeric_series(leq_col)
        if leq.dropna().empty:
            story.append(Paragraph("LEQ stream not available; compliance metrics cannot be derived.", styles['BodyText']))
            story.append(Spacer(1, 0.18 * inch))
            return

        env = compute_ldn_lden(ts, leq) or {}
        measured_lden = env.get('Lden')
        measured_lnight = env.get('Lnight')

        # Local limits (example defaults; can be made configurable later)
        local_day_limit = 65.0
        local_night_limit = 55.0

        # Local day/night LAeq (07:00–22:00, 22:00–07:00)
        h = ts.dt.hour
        is_day = (h >= 7) & (h < 22)
        is_night = ~is_day
        laeq_day = energetic_mean_db(leq[is_day]) if is_day.any() else None
        laeq_night = energetic_mean_db(leq[is_night]) if is_night.any() else None

        # STRICT COLUMN BOUNDARIES: Each row has atomic, single-value cells
        rows = [
            [
                "WHO 2018 Road Traffic (Lden)",
                self._fmt_db_plain(measured_lden),
                self._fmt_db_plain(53.0),
                self._status_pass_fail(53.0, measured_lden),
                self._fmt_db_plain(self._exceedance_amount(53.0, measured_lden)),
            ],
            [
                "WHO 2018 Nighttime (Lnight)",
                self._fmt_db_plain(measured_lnight),
                self._fmt_db_plain(45.0),
                self._status_pass_fail(45.0, measured_lnight),
                self._fmt_db_plain(self._exceedance_amount(45.0, measured_lnight)),
            ],
            [
                "Local Day (07:00–22:00)",
                self._fmt_db_plain(laeq_day),
                self._fmt_db_plain(local_day_limit),
                self._status_pass_fail(local_day_limit, laeq_day),
                self._fmt_db_plain(self._exceedance_amount(local_day_limit, laeq_day)),
            ],
            [
                "Local Night (22:00–07:00)",
                self._fmt_db_plain(laeq_night),
                self._fmt_db_plain(local_night_limit),
                self._status_pass_fail(local_night_limit, laeq_night),
                self._fmt_db_plain(self._exceedance_amount(local_night_limit, laeq_night)),
            ],
        ]

        data = [["Check", "Measured (dB(A))", "Limit (dB(A))", "Status", "Exceedance (dB)"]] + rows
        table = Table(data, colWidths=[2.8 * inch, 1.6 * inch, 1.6 * inch, 0.8 * inch, 1.2 * inch])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3D5A80')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F0F4F8')),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#DDDDDD')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ALIGN', (1, 1), (-1, -1), 'CENTER'),  # Center-align numeric/status columns
        ]))
        story.append(table)
        story.append(Spacer(1, 0.18 * inch))

    # -----------------------------
    # Section 3
    # -----------------------------

    def _add_pdf_section_sleep_disturbance_lmax(self, story, styles, *, ts: pd.Series, lmax_col: str | None):
        story.append(Paragraph("Section 3: Single-Event Sleep Disturbance (L-Max only)", styles['h1']))
        if not lmax_col or lmax_col not in self.df.columns:
            story.append(Paragraph("L-Max stream not available; single-event peak analysis skipped.", styles['BodyText']))
            story.append(Spacer(1, 0.18 * inch))
            return

        lmax = self._get_numeric_series(lmax_col)
        h = ts.dt.hour
        is_night = (h >= 23) | (h < 7)
        night = lmax[is_night]

        if night.dropna().empty:
            story.append(Paragraph("No valid nighttime L-Max samples found (23:00–07:00).", styles['BodyText']))
            story.append(Spacer(1, 0.18 * inch))
            return

        la_max_night = float(night.max())
        exceed_threshold = 60.0
        n_events = int((night > exceed_threshold).sum())
        n_total = int(night.notna().sum())
        pct = (100.0 * n_events / max(1, n_total))

        # Convert sample count to minutes (assuming 1 Hz sampling)
        sampling_rate_hz = 1.0  # Assume 1 sample per second (typical for WLG)
        minutes_exceeding = n_events / (sampling_rate_hz * 60.0)

        story.append(Paragraph(
            f"Nighttime hours isolated: <b>23:00–07:00</b>. Peak extraction uses <b>{escape(lmax_col)}</b> only.",
            styles['BodyText'],
        ))
        story.append(Paragraph(
            f"Absolute highest nighttime peak (LAmax): <b>{escape(self._fmt_db(la_max_night))}</b>.",
            styles['BodyText'],
        ))
        story.append(Paragraph(
            f"Cumulative nighttime exposure exceeding {self._fmt_float(exceed_threshold)} dB(A) (outdoor facade screening threshold): <b>{self._fmt_float(minutes_exceeding, 1)} minutes</b> ({pct:.1f}% of nighttime samples).",
            styles['BodyText'],
        ))
        story.append(Spacer(1, 0.18 * inch))

    # -----------------------------
    # Section 4
    # -----------------------------

    def _add_pdf_section_statistical_profile_percentiles(self, story, styles, *, leq_col: str | None):
        story.append(Paragraph("Section 4: Statistical Noise Profile (Percentiles)", styles['h1']))
        leq = self._get_numeric_series(leq_col)
        if leq.dropna().empty:
            story.append(Paragraph("LEQ stream not available; percentiles cannot be computed.", styles['BodyText']))
            story.append(Spacer(1, 0.18 * inch))
            return

        exc = exceedance_levels_db(leq.dropna().to_numpy()) or {}
        # Expect keys: L5, L10, L50, L90, L95
        defs = {
            "L5": "High-noise / transient events (exceeded 5% of time)",
            "L10": "High-noise / transient events (exceeded 10% of time)",
            "L50": "Median acoustic climate (exceeded 50% of time)",
            "L90": "Background / ambient baseline (exceeded 90% of time)",
            "L95": "Background / ambient baseline (exceeded 95% of time)",
        }

        rows = []
        for k in ["L5", "L10", "L50", "L90", "L95"]:
            v = exc.get(k)
            rows.append([k, self._fmt_db_plain(v), defs.get(k, "")])

        data = [["Percentile", "Value (dB(A))", "Definition"]] + rows
        table = Table(data, colWidths=[1.0 * inch, 1.4 * inch, 5.2 * inch])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3D5A80')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F0F4F8')),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#DDDDDD')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(table)
        story.append(Spacer(1, 0.18 * inch))

    # -----------------------------
    # Section 5
    # -----------------------------

    def _fig_time_series_with_band(self, *, ts: pd.Series, leq: pd.Series, lmax: pd.Series | None, lmin: pd.Series | None):
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=ts, y=leq, mode='lines', name='LEQ (LAeq)', line=dict(color='#3D5A80', width=2)))

        if lmax is not None and lmin is not None and (not lmax.dropna().empty) and (not lmin.dropna().empty):
            fig.add_trace(go.Scatter(x=ts, y=lmax, mode='lines', name='L-Max', line=dict(color='rgba(244,162,97,0.9)', width=1)))
            fig.add_trace(go.Scatter(
                x=ts,
                y=lmin,
                mode='lines',
                name='L-Min',
                line=dict(color='rgba(42,157,143,0.9)', width=1),
                fill='tonexty',
                fillcolor='rgba(42,157,143,0.15)',
            ))

        # Fixed orientation levels, not per-point limits: the trace is LAeq, while
        # 53/45 dB are the WHO Lden/Lnight guideline values, which apply to
        # penalty-weighted long-term averages.
        fig.add_hline(y=53.0, line_dash='dot', line_color='rgba(231,111,81,0.9)', annotation_text='53 dB reference', annotation_position='top left')
        fig.add_hline(y=45.0, line_dash='dot', line_color='rgba(231,111,81,0.6)', annotation_text='45 dB reference', annotation_position='bottom left')

        fig.update_layout(
            title='Chart 1: Time Series (LEQ with L-Max/L-Min band)',
            xaxis_title='Time',
            yaxis_title='Sound Level (dB(A))',
            legend_orientation='h',
            margin=dict(l=40, r=20, t=60, b=40),
            height=420,
        )
        return fig

    def _fig_polar_hourly_laeq(self, *, ts: pd.Series, leq: pd.Series):
        try:
            df = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
            if df.empty:
                return None
            df['hour'] = df['ts'].dt.hour
            hour_labels = [f'{h:02d}:00' for h in range(24)]

            fig = go.Figure()
            for h in range(24):
                hour_values = pd.to_numeric(df.loc[df['hour'] == h, 'leq'], errors='coerce').dropna()
                if hour_values.empty:
                    continue
                fig.add_trace(go.Box(
                    y=hour_values,
                    x=[f'{h:02d}:00'] * len(hour_values),
                    name=f'{h:02d}:00',
                    boxpoints='outliers',
                    quartilemethod='linear',
                    marker=dict(color='#3D5A80'),
                    line=dict(color='#293241', width=1.5),
                    fillcolor='rgba(61, 90, 128, 0.35)',
                    hovertemplate='Hour %{x}<br>LEQ: %{y:.1f} dB(A)<extra></extra>',
                    showlegend=False,
                ))
            fig.update_layout(
                title='Chart 2: Diurnal Box-and-Whisker (Hourly LEQ volatility)',
                xaxis=dict(
                    title='Hour of Day',
                    categoryorder='array',
                    categoryarray=hour_labels,
                    tickmode='array',
                    tickvals=hour_labels,
                    ticktext=hour_labels,
                    automargin=True,
                ),
                yaxis=dict(title='LEQ dB(A)', gridcolor='rgba(0,0,0,0.08)', zeroline=False),
                margin=dict(l=55, r=25, t=60, b=90),
                height=520,
            )
            return fig
        except Exception as e:
            print(f"[PDF] Box plot failed: {str(e)[:100]}")
            return None

    def _fig_temporal_heatmap(self, *, ts: pd.Series, leq: pd.Series):
        df = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
        if df.empty:
            return None
        df['date'] = df['ts'].dt.date
        df['hour'] = df['ts'].dt.hour

        # Energetic mean per (date, hour)
        def _grp_energetic_mean(x):
            return energetic_mean_db(x)

        agg = df.groupby(['date', 'hour'])['leq'].apply(_grp_energetic_mean).reset_index(name='laeq')
        pivot = agg.pivot(index='hour', columns='date', values='laeq').sort_index(ascending=True)
        pivot = pivot.reindex(index=list(range(24)))

        x = [str(d) for d in pivot.columns]
        y = pivot.index.tolist()
        z = pivot.values.astype(float)

        fig = go.Figure(
            data=go.Heatmap(
                z=z,
                x=x,
                y=y,
                colorscale='Viridis',
                colorbar=dict(title='LEQ dB(A)'),
            )
        )
        fig.update_layout(
            title='Chart 3: Temporal Heatmap (LEQ intensity)',
            xaxis_title='Date',
            yaxis_title='Hour of day',
            margin=dict(l=50, r=20, t=60, b=60),
            height=420,
        )
        fig.update_yaxes(
            tickmode='array',
            tickvals=list(range(24)),
            ticktext=[f'{h:02d}:00' for h in range(24)],
            automargin=True,
        )
        return fig

    def _add_pdf_section_visualizations(self, story, styles, *, ts: pd.Series, leq_col: str | None, lmax_col: str | None, lmin_col: str | None):
        story.append(PageBreak())
        story.append(Paragraph("Section 5: Visualizations", styles['h1']))

        leq = self._get_numeric_series(leq_col)
        if leq.dropna().empty:
            story.append(Paragraph("LEQ stream not available; charts cannot be generated.", styles['BodyText']))
            return

        lmax = self._get_numeric_series(lmax_col) if (lmax_col and lmax_col in self.df.columns) else None
        lmin = self._get_numeric_series(lmin_col) if (lmin_col and lmin_col in self.df.columns) else None

        figs = [
            self._fig_time_series_with_band(ts=ts, leq=leq, lmax=lmax, lmin=lmin),
            self._fig_polar_hourly_laeq(ts=ts, leq=leq),
            self._fig_temporal_heatmap(ts=ts, leq=leq),
        ]

        for fig in figs:
            if fig is None:
                story.append(Paragraph("<i>Chart unavailable due to insufficient data.</i>", styles['BodyText']))
                story.append(Spacer(1, 0.12 * inch))
                continue
            img = self._plotly_fig_to_image(fig, width_inch=9.0, height_inch=3.8)
            if img:
                story.append(img)
            else:
                story.append(Paragraph("<i>Unable to render chart image in PDF.</i>", styles['BodyText']))
            story.append(Spacer(1, 0.18 * inch))

    def _get_noise_thresholds_exposure_references(self):
        """Widely cited references for interpreting community and occupational noise exposure.

        Note: These values are informational guidance/reference levels and are not universally
        enforceable legal limits. Legal requirements can vary by jurisdiction, land use, and
        permitting/industrial context.
        """

        return {
            "epa_1974": {
                "title": "US EPA (1974) — Levels of Environmental Noise (press release + Levels Document)",
                "url": "https://www.epa.gov/archive/epa/aboutepa/epa-identifies-noise-levels-affecting-health-and-welfare.html",
                "pdf": "https://nepis.epa.gov/Exe/ZyPDF.cgi/2000L3LN.PDF?Dockey=2000L3LN.PDF",
                "community": [
                    {
                        "context": "Prevent measurable hearing loss (lifetime)",
                        "metric": "24-hour average exposure",
                        "level": "70 dB",
                    },
                    {
                        "context": "Prevent activity interference & annoyance (outdoors)",
                        "metric": "Long-term energy average",
                        "level": "55 dB",
                    },
                    {
                        "context": "Prevent activity interference & annoyance (indoors: homes, hospitals, schools)",
                        "metric": "Long-term energy average",
                        "level": "45 dB",
                    },
                ],
            },
            "osha_1910_95": {
                "title": "OSHA 29 CFR 1910.95 — Occupational noise exposure",
                "url": "https://www.osha.gov/laws-regs/regulations/standardnumber/1910/1910.95",
                "occupational": [
                    {
                        "organization": "OSHA",
                        "limit": "85 dBA (8-hr TWA) — Action level",
                        "meaning": "Triggers hearing conservation program requirements at/above this level.",
                    },
                    {
                        "organization": "OSHA",
                        "limit": "90 dBA (8-hr TWA) — PEL basis (Table G-16)",
                        "meaning": "Permissible exposures depend on duration per Table G-16; controls/PPE required when exceeded.",
                    },
                    {
                        "organization": "OSHA",
                        "limit": "140 dB peak — impulsive/impact noise",
                        "meaning": "Impulsive or impact noise should not exceed 140 dB peak sound pressure level.",
                    },
                ],
            },
            "niosh_1998": {
                "title": "NIOSH (1998) — Criteria for a Recommended Standard: Occupational Noise Exposure",
                "url": "https://stacks.cdc.gov/view/cdc/6376",
                "pdf": "https://stacks.cdc.gov/view/cdc/6376/cdc_6376_DS1.pdf",
                "rel": {
                    "organization": "NIOSH",
                    "limit": "85 dBA (8-hr TWA) — REL",
                    "meaning": "Recommended exposure limit; exposures at/above are hazardous (criteria document).",
                },
            },
        }

    def _add_pdf_noise_thresholds_exposure_info(self, story, styles):
        """Add a final-page, professional reference section on community thresholds and occupational exposure."""

        refs = self._get_noise_thresholds_exposure_references()
        epa = refs.get("epa_1974", {})
        osha = refs.get("osha_1910_95", {})
        niosh = refs.get("niosh_1998", {})

        story.append(PageBreak())
        story.append(Paragraph("Noise Thresholds & Human Exposure (Informational)", styles['h1']))
        story.append(Paragraph(
            "<i>This section provides widely cited reference values to support interpretation. "
            "Applicable legal limits vary by jurisdiction, land use zoning, and permitting/industrial context.</i>",
            styles['BodyText'],
        ))
        story.append(Spacer(1, 0.15 * inch))

        # Community / residential reference levels (EPA)
        story.append(Paragraph("Community / Residential Reference Levels", styles['h2']))
        epa_title = epa.get("title", "US EPA (1974)")
        epa_url = epa.get("url")
        epa_pdf = epa.get("pdf")

        if epa_url:
            story.append(Paragraph(
                f"Source: <link href=\"{epa_url}\">{epa_title}</link>",
                styles['BodyText'],
            ))
        else:
            story.append(Paragraph(f"Source: {epa_title}", styles['BodyText']))
        if epa_pdf:
            story.append(Paragraph(
                f"Levels Document (PDF): <link href=\"{epa_pdf}\">{epa_pdf}</link>",
                styles['BodyText'],
            ))
        story.append(Spacer(1, 0.08 * inch))

        community_rows = epa.get("community", []) or []
        community_data = [["Context", "Metric", "Reference level", "Notes"]]
        for row in community_rows:
            community_data.append([
                row.get("context", ""),
                row.get("metric", ""),
                row.get("level", ""),
                "Informational reference (not a universal legal limit)",
            ])

        community_table = Table(community_data, colWidths=[3.2 * inch, 2.0 * inch, 1.2 * inch, 2.4 * inch])
        community_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3D5A80')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F0F4F8')),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#DDDDDD')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(community_table)
        story.append(Spacer(1, 0.2 * inch))

        # Occupational exposure (OSHA + NIOSH)
        story.append(Paragraph("Occupational Exposure (Workplace)", styles['h2']))

        osha_title = osha.get("title", "OSHA 29 CFR 1910.95")
        osha_url = osha.get("url")
        if osha_url:
            story.append(Paragraph(
                f"OSHA: <link href=\"{osha_url}\">{osha_title}</link>",
                styles['BodyText'],
            ))
        else:
            story.append(Paragraph(f"OSHA: {osha_title}", styles['BodyText']))

        niosh_title = niosh.get("title", "NIOSH (1998)")
        niosh_url = niosh.get("url")
        niosh_pdf = niosh.get("pdf")
        if niosh_url:
            story.append(Paragraph(
                f"NIOSH: <link href=\"{niosh_url}\">{niosh_title}</link>",
                styles['BodyText'],
            ))
        else:
            story.append(Paragraph(f"NIOSH: {niosh_title}", styles['BodyText']))
        if niosh_pdf:
            story.append(Paragraph(
                f"NIOSH PDF: <link href=\"{niosh_pdf}\">{niosh_pdf}</link>",
                styles['BodyText'],
            ))
        story.append(Spacer(1, 0.08 * inch))

        occupational_data = [["Organization", "Limit / Trigger", "Interpretation"]]
        for row in (osha.get("occupational", []) or []):
            occupational_data.append([
                row.get("organization", ""),
                row.get("limit", ""),
                row.get("meaning", ""),
            ])
        rel = niosh.get("rel") or {}
        if rel:
            occupational_data.append([
                rel.get("organization", ""),
                rel.get("limit", ""),
                rel.get("meaning", ""),
            ])

        occupational_table = Table(occupational_data, colWidths=[1.2 * inch, 3.0 * inch, 4.6 * inch])
        occupational_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3D5A80')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F0F4F8')),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#DDDDDD')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(occupational_table)

        story.append(Spacer(1, 0.15 * inch))
        story.append(Paragraph(
            "<b>Exposure duration matters:</b> OSHA Table G-16 provides permitted durations that decrease as dB increases "
            "(e.g., 8h at 90 dBA; 4h at 95 dBA; 2h at 100 dBA; 1h at 105 dBA; 0.5h at 110 dBA; 0.25h at 115 dBA).",
            styles['BodyText'],
        ))

    def generate_html_report(self, report_type='comprehensive', output_dir: str | None = None):
        """Generate a standalone HTML report."""
        return self._generate_html_report(report_type, output_dir=output_dir)

    def _get_pdf_styles(self):
        """Define styles for the PDF report."""
        styles = getSampleStyleSheet()
        
        # Modify existing styles
        styles['Title'].fontSize = 24
        styles['Title'].leading = 30
        styles['Title'].alignment = TA_CENTER
        styles['Title'].textColor = colors.HexColor('#3D5A80')

        styles['h1'].textColor = colors.HexColor('#3D5A80')
        styles['h1'].fontSize = 18
        styles['h1'].leading = 22
        styles['h1'].spaceBefore = 20
        styles['h1'].spaceAfter = 10
        styles['h1'].keepWithNext = 1

        styles['h2'].textColor = colors.HexColor('#293241')
        styles['h2'].fontSize = 14
        styles['h2'].leading = 18
        styles['h2'].spaceBefore = 15
        styles['h2'].spaceAfter = 8
        styles['h2'].keepWithNext = 1

        styles['BodyText'].fontSize = 10
        styles['BodyText'].leading = 14
        styles['BodyText'].alignment = TA_JUSTIFY

        # Add new custom styles
        styles.add(ParagraphStyle(name='SubTitle', fontSize=14, parent=styles['Normal'], alignment=TA_CENTER, textColor=colors.HexColor('#666666')))
        styles.add(ParagraphStyle(name='Footer', fontSize=8, parent=styles['Normal'], alignment=TA_CENTER, textColor=colors.grey))
        
        return styles

    def _add_page_template(self, canvas, doc):
        """Add header and footer to each page."""
        canvas.saveState()
        # Footer
        footer_text = f"Page {doc.page} | Noise Analysis Report | Generated: {datetime.now().strftime('%Y-%m-%d')}"
        canvas.setFont('Helvetica', 8)
        canvas.drawCentredString(doc.width/2 + doc.leftMargin, 0.25 * inch, footer_text)
        # Header Line
        canvas.setStrokeColorRGB(0.239, 0.353, 0.502) # #3D5A80
        canvas.setLineWidth(2)
        canvas.line(doc.leftMargin, doc.height + doc.topMargin + 0.2*inch, doc.width + doc.leftMargin, doc.height + doc.topMargin + 0.2*inch)
        canvas.restoreState()

    def _add_pdf_header(self, story, styles, report_type):
        story.append(Paragraph("Environmental Noise Analysis Report", styles['Title']))
        story.append(Spacer(1, 0.2 * inch))
        story.append(Paragraph(f"Source File: {os.path.basename(self.filepath)}", styles['SubTitle']))
        story.append(Paragraph(f"Report Type: {report_type.upper()}", styles['SubTitle']))
        story.append(Spacer(1, 0.4 * inch))

    def _plotly_fig_to_image(self, fig, width_inch=8, height_inch=4):
        """Convert a Plotly figure to a ReportLab Image object."""
        if fig is None:
            return None
        try:
            img_bytes = fig.to_image(format="png", scale=2)
            
            img = Image(io.BytesIO(img_bytes))
            
            # Maintain aspect ratio
            aspect = img.imageHeight / img.imageWidth if img.imageWidth > 0 else 1
            img.drawWidth = width_inch * inch
            img.drawHeight = (width_inch * inch) * aspect
            
            # If height is too large, constrain by height instead
            if img.drawHeight > height_inch * inch:
                img.drawHeight = height_inch * inch
                img.drawWidth = (height_inch * inch) / aspect
                
            return img
        except Exception as e:
            print(f"[PDF] Warning: Could not convert plotly figure to image: {str(e)[:100]}")
            return None

    def _add_pdf_executive_summary(self, story, styles, analysis):
        story.append(Paragraph("Executive Summary", styles['h1']))
        
        stats = analysis.get('statistics', {})
        if not stats:
            story.append(Paragraph("No statistical data available.", styles['BodyText']))
            return

        for col_name, col_stats in stats.items():
            laeq = col_stats.get('laeq_db', 'N/A')
            lmin = col_stats.get('min', 'N/A')
            lmax = col_stats.get('max', 'N/A')
            summary_text = f"The primary noise column, <b>{col_name}</b>, shows an energy-average level (LAeq) of <b>{laeq} dB(A)</b>, with levels ranging from {lmin} to {lmax} dB(A)."
            story.append(Paragraph(summary_text, styles['BodyText']))
            story.append(Spacer(1, 6))

        interpretations = analysis.get('interpretations', [])
        if interpretations:
            story.append(Paragraph("Key Interpretations:", styles['h2']))
            for interp in interpretations:
                story.append(Paragraph(f"• {interp}", styles['BodyText']))
        story.append(Spacer(1, 0.2 * inch))

    def _add_pdf_methods_section(self, story, styles):
        story.append(Paragraph("Methods & Definitions", styles['h1']))
        story.append(Paragraph("The following standard acoustical metrics and methods were used in this analysis:", styles['BodyText']))
        story.append(Spacer(1, 10))
        
        definitions = [
            ("<b>LAeq (Energy-Average Sound Level):</b> The equivalent continuous sound level, which represents the average acoustic energy over the measurement period. It is calculated as: <br/><font name='Courier'>LAeq = 10 * log10( (1/N) * Σ(10^(Li/10)) )</font>", styles['BodyText']),
            ("<b>Statistical Levels (Lx):</b> The sound level exceeded for x% of the measurement time. For example, L90 is the level exceeded 90% of the time and is often considered the background noise level.", styles['BodyText']),
            ("<b>Lnight (Night Average Sound Level):</b> The average level over the defined night period (EU/WHO commonly use 23:00–07:00).", styles['BodyText']),
            ("<b>Ldn (Day-Night Average Sound Level):</b> A 24-hour average LAeq with a 10 dB penalty added to nighttime levels (typically 22:00–07:00) to account for increased sensitivity to noise at night.", styles['BodyText']),
            ("<b>Lden (Day-Evening-Night Average Sound Level):</b> A 24-hour average LAeq with a 5 dB penalty for evening hours (typically 19:00–23:00) and a 10 dB penalty for nighttime hours (typically 23:00–07:00).", styles['BodyText'])
        ]
        
        for text, style in definitions:
            story.append(Paragraph(text, style))
            story.append(Spacer(1, 6))
        story.append(Spacer(1, 0.2 * inch))

    def _add_pdf_environmental_metrics(self, story, styles, analysis):
        metrics = analysis.get('environmental_metrics', {})
        if not metrics:
            return

        story.append(Paragraph("Environmental Noise Metrics (Ldn & Lden)", styles['h1']))
        
        data = [['Metric', 'Value (dB(A))', 'Description']]
        
        for col_name, m in metrics.items():
            if not isinstance(m, dict): continue
            
            data.append([Paragraph(f"<b>{col_name}</b>", styles['h2']), '', ''])
            
            env_metrics = {
                'LAeq_24h': "24-Hour Energy Average",
                'LAeq_day_ldn': "Daytime Average for Ldn (07:00–22:00)",
                'LAeq_night_ldn': "Nighttime Average for Ldn (22:00–07:00)",
                'LAeq_day_lden': "Daytime Average for Lden (07:00–19:00)",
                'LAeq_evening_lden': "Evening Average for Lden (19:00–23:00)",
                'LAeq_night_lden': "Nighttime Average for Lnight/Lden (23:00–07:00)",
                'Lnight': "Night Average (23:00–07:00)",
                'Lden': "Day-Evening-Night Level (+5 evening, +10 night penalty)",
                'Ldn': "Day-Night Level (+10 night penalty; night typically 22:00–07:00)"
            }
            
            for key, desc in env_metrics.items():
                if key in m and m[key] is not None:
                    data.append([key, str(m[key]), desc])

        if len(data) > 1:
            table = Table(data, colWidths=[1.5*inch, 1.5*inch, 6*inch])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#3D5A80')),
                ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
                ('ALIGN', (0,0), (-1,-1), 'LEFT'),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0,0), (-1,0), 12),
                ('BACKGROUND', (0,1), (-1,-1), colors.HexColor('#F0F4F8')),
                ('GRID', (0,0), (-1,-1), 1, colors.HexColor('#DDDDDD')),
                ('SPAN', (0,1), (-1,1)), # Span for column name
            ]))
            story.append(table)
        story.append(Spacer(1, 0.2 * inch))

    def _add_pdf_standards_thresholds(self, story, styles, standards):
        """Add a concise, cited standards table (WHO 2018 guideline levels)."""

        who = None
        if isinstance(standards, dict):
            who = standards.get('who_2018')
        if not who:
            who = who_2018_environmental_noise_guideline_levels()

        source = (who or {}).get('source', {})
        guidelines = (who or {}).get('guidelines', {})

        if not guidelines:
            return

        story.append(Paragraph("Standards & Thresholds (Guidelines)", styles['h1']))
        story.append(Paragraph(
            f"<b>WHO {source.get('year', 2018)}</b> — {source.get('title', 'Environmental Noise Guidelines')}.",
            styles['BodyText'],
        ))
        if source.get('url'):
            story.append(Paragraph(f"Source: {source['url']}", styles['BodyText']))
        disclaimer = (who or {}).get('disclaimer')
        if disclaimer:
            story.append(Paragraph(f"<i>{disclaimer}</i>", styles['BodyText']))
        story.append(Spacer(1, 0.15 * inch))

        data = [["Noise source", "Metric", "Guideline (dB)", "Strength"]]
        for source_key, entry in guidelines.items():
            metrics_db = (entry or {}).get('metrics_db', {})
            strength = (entry or {}).get('recommendation_strength', 'N/A')
            for metric, limit in (metrics_db or {}).items():
                data.append([
                    str(source_key).replace('_', ' '),
                    str(metric),
                    str(limit),
                    str(strength),
                ])

        table = Table(data, colWidths=[2.2 * inch, 1.2 * inch, 1.2 * inch, 1.2 * inch])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3D5A80')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F0F4F8')),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#DDDDDD')),
        ]))
        story.append(table)
        story.append(Spacer(1, 0.2 * inch))

    def _add_pdf_statistics_section(self, story, styles, analysis):
        stats = analysis.get('statistics', {})
        percentiles = analysis.get('percentiles', {})
        if not stats: return

        story.append(Paragraph("Detailed Statistical Analysis", styles['h1']))

        for col_name in stats.keys():
            story.append(Paragraph(f"Analysis for: {col_name}", styles['h2']))
            
            col_stats = stats[col_name]
            col_percentiles = percentiles.get(col_name, {})

            data = [
                ['Metric', 'Value (dB)'],
                ['LAeq (energy-average)', col_stats.get('laeq_db', 'N/A')],
                ['Min / Max', f"{col_stats.get('min', 'N/A')} / {col_stats.get('max', 'N/A')}"],
                ['L95 (Background Noise)', col_percentiles.get('L95', 'N/A')],
                ['L90', col_percentiles.get('L90', 'N/A')],
                ['L50 (Median)', col_percentiles.get('L50', 'N/A')],
                ['L10', col_percentiles.get('L10', 'N/A')],
                ['L5 (high-noise events; exceeded 5% of time)', col_percentiles.get('L5', 'N/A')],
            ]
            
            table = Table(data, colWidths=[2*inch, 1.5*inch])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#3D5A80')),
                ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
                ('ALIGN', (0,0), (-1,-1), 'LEFT'),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0,0), (-1,0), 10),
                ('BACKGROUND', (0,1), (-1,-1), colors.HexColor('#F0F4F8')),
                ('GRID', (0,0), (-1,-1), 1, colors.HexColor('#DDDDDD')),
            ]))
            story.append(table)
            story.append(Spacer(1, 0.2 * inch))

    def _add_pdf_key_visuals(self, story, styles, charts: AdvancedChartGenerator, analysis: dict):
        story.append(Paragraph("Key Visualizations & Analysis", styles['h1']))
        story.append(Spacer(1, 0.1 * inch))
        
        # Import the environmental visualization engine
        try:
            from analysis.environmental_viz import EnvironmentalVisualizationEngine
            viz_engine = EnvironmentalVisualizationEngine(self.df, 'Noise_Level_dB')
            has_env_viz = True
        except Exception as e:
            print(f"[PDF] Could not import environmental viz: {e}")
            has_env_viz = False
        
        # Advanced visualizations (new environmental charts)
        if has_env_viz:
            advanced_visuals = {}
            for title, fn in [
                ("Exceedance Analysis", lambda: viz_engine.generate_exceedance_analysis('Noise_Level_dB')),
                ("Temporal Heatmap", lambda: viz_engine.generate_enhanced_temporal_heatmap('Noise_Level_dB', 'hourly')),
                ("Compliance Dashboard", lambda: viz_engine.generate_compliance_dashboard('Noise_Level_dB', analysis or {})),
            ]:
                try:
                    advanced_visuals[title] = fn()
                except Exception as e:
                    print(f"[PDF] Advanced visual '{title}' failed: {str(e)[:160]}")
                    advanced_visuals[title] = None
            
            story.append(Paragraph("Environmental Analysis", styles['h2']))
            for title, fig in advanced_visuals.items():
                if fig:
                    story.append(Paragraph(title, styles['h3']))
                    img = self._plotly_fig_to_image(fig, width_inch=8.5, height_inch=3.5)
                    if img:
                        story.append(img)
                    else:
                        story.append(Paragraph(f"<i>{title}: Unable to render chart</i>", styles['BodyText']))
                    story.append(Spacer(1, 0.15 * inch))
                else:
                    story.append(Paragraph(f"<i>{title}: Not available</i>", styles['BodyText']))
                    story.append(Spacer(1, 0.1 * inch))
        
        # Classic visualizations
        story.append(PageBreak())
        story.append(Paragraph("Standard Charts", styles['h2']))
        
        visuals = {
            "Diurnal Box-and-Whisker (Hourly LEQ Volatility)": charts.generate_diurnal_box_whisker_chart(),
            "Hourly Noise Levels (Heatmap)": charts.generate_heatmap_hourly(),
            "Noise Level Distribution": charts.generate_distribution_heatmap(),
            "Time Series Analysis": charts.generate_time_series_heatmap(),
        }

        for title, fig in visuals.items():
            if fig:
                story.append(Paragraph(title, styles['h3']))
                img = self._plotly_fig_to_image(fig, width_inch=8.5, height_inch=3.5)
                if img:
                    story.append(img)
                else:
                    story.append(Paragraph(f"<i>{title}: Unable to render chart</i>", styles['BodyText']))
                story.append(Spacer(1, 0.15 * inch))

    def _add_pdf_compliance_section(self, story, styles, analysis):
        compliance = analysis.get('compliance', {})
        if not compliance: return

        story.append(Paragraph("Compliance Assessment", styles['h1']))
        
        for col_name, col_compliance in compliance.items():
            story.append(Paragraph(f"Compliance for: {col_name}", styles['h2']))
            
            data = [['Check', 'Measured (dB)', 'Limit (dB)', 'Status', 'Exceeded By (dB)']]

            # Always include LAeq summary if present.
            if 'current_leq' in col_compliance:
                data.append(['current_leq (LAeq)', str(col_compliance.get('current_leq')), '-', 'INFO', '-'])

            for check_name, info in col_compliance.items():
                if check_name in {'current_leq', 'current_Lden', 'current_Lnight', 'current_LAeq_24h'}:
                    continue

                if not isinstance(info, dict):
                    continue

                measured = info.get('value_db')
                limit = info.get('limit_db')
                status = info.get('status', 'N/A')
                exceeded_by = info.get('exceeded_by_db')

                status_cell = Paragraph(str(status), styles['BodyText'])
                if status == 'FAIL':
                    status_cell = Paragraph(f"<font color='red'>{status}</font>", styles['BodyText'])

                data.append([
                    str(check_name),
                    str(measured) if measured is not None else 'N/A',
                    str(limit) if limit is not None else 'N/A',
                    status_cell,
                    str(exceeded_by) if exceeded_by is not None else 'N/A',
                ])

            table = Table(data, colWidths=[3.0*inch, 1.2*inch, 1.0*inch, 1.0*inch, 1.3*inch])
            table.setStyle(TableStyle([
                ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#3D5A80')),
                ('TEXTCOLOR', (0,0), (-1,0), colors.whitesmoke),
                ('ALIGN', (0,0), (-1,-1), 'LEFT'),
                ('FONTNAME', (0,0), (-1,0), 'Helvetica-Bold'),
                ('BOTTOMPADDING', (0,0), (-1,0), 10),
                ('GRID', (0,0), (-1,-1), 1, colors.HexColor('#DDDDDD')),
            ]))
            story.append(table)
            story.append(Spacer(1, 0.2 * inch))

    def _add_pdf_iso_epa_details(self, story, styles, standards):
        # This can be expanded with more details from the standards analysis
        pass

    def _add_pdf_recommendations(self, story, styles, standards):
        recs = standards.get('recommendations', [])
        if not recs: 
            return

        story.append(Paragraph("Recommendations", styles['h1']))
        
        for i, rec in enumerate(recs, 1):
            rec_text = rec if isinstance(rec, str) else str(rec)
            story.append(Paragraph(f"{i}. {rec_text}", styles['BodyText']))
            story.append(Spacer(1, 0.1 * inch))
    
    def generate_iso_report(self):
        """Generate ISO compliance report"""
        return self._generate_html_report('iso')
    
    def generate_epa_report(self):
        """Generate EPA compliance report"""
        return self._generate_html_report('epa')
    
    def _generate_html_report(self, report_type='comprehensive', output_dir: str | None = None):
        """Generate HTML report"""
        
        # Run analyses (cached when provided)
        analysis = self._get_analysis()
        standards = self._get_standards()
        charts = AdvancedChartGenerator(self.df)
        
        # Create HTML content
        html_content = f"""
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Noise Analysis Report - {report_type.upper()}</title>
            <style>
                * {{
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                }}
                
                body {{
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    color: #333;
                    background-color: #f5f5f5;
                    line-height: 1.6;
                }}
                
                .container {{
                    max-width: 1200px;
                    margin: 0 auto;
                    background-color: white;
                    padding: 40px;
                }}
                
                .header {{
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    padding: 40px;
                    border-radius: 8px;
                    margin-bottom: 30px;
                    text-align: center;
                }}
                
                .header h1 {{
                    font-size: 32px;
                    margin-bottom: 10px;
                }}
                
                .header p {{
                    font-size: 16px;
                    opacity: 0.9;
                }}
                
                .section {{
                    margin-bottom: 40px;
                    border-left: 4px solid #667eea;
                    padding-left: 20px;
                }}
                
                .section h2 {{
                    color: #667eea;
                    font-size: 24px;
                    margin-bottom: 20px;
                    border-bottom: 2px solid #667eea;
                    padding-bottom: 10px;
                }}
                
                .section h3 {{
                    color: #555;
                    font-size: 18px;
                    margin-top: 20px;
                    margin-bottom: 15px;
                }}
                
                .stats-grid {{
                    display: grid;
                    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                    gap: 20px;
                    margin-bottom: 20px;
                }}
                
                .stat-box {{
                    background: linear-gradient(135deg, #f5f7fa 0%, #c3cfe2 100%);
                    padding: 20px;
                    border-radius: 8px;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                }}
                
                .stat-box.warning {{
                    background: linear-gradient(135deg, #fff5e6 0%, #ffd4a3 100%);
                }}
                
                .stat-box.danger {{
                    background: linear-gradient(135deg, #ffe5e5 0%, #ffb3b3 100%);
                }}
                
                .stat-label {{
                    color: #666;
                    font-size: 12px;
                    text-transform: uppercase;
                    margin-bottom: 10px;
                    font-weight: 600;
                }}
                
                .stat-value {{
                    color: #333;
                    font-size: 28px;
                    font-weight: bold;
                }}
                
                .stat-unit {{
                    color: #999;
                    font-size: 14px;
                    margin-left: 5px;
                }}
                
                table {{
                    width: 100%;
                    border-collapse: collapse;
                    margin-bottom: 20px;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                }}
                
                th {{
                    background-color: #667eea;
                    color: white;
                    padding: 15px;
                    text-align: left;
                    font-weight: 600;
                }}
                
                td {{
                    padding: 12px 15px;
                    border-bottom: 1px solid #ddd;
                }}
                
                tr:hover {{
                    background-color: #f9f9f9;
                }}
                
                .pass {{
                    color: #27ae60;
                    font-weight: bold;
                }}
                
                .fail {{
                    color: #e74c3c;
                    font-weight: bold;
                }}
                
                .warning {{
                    color: #f39c12;
                    font-weight: bold;
                }}
                
                .interpretation-box {{
                    background-color: #ecf0f1;
                    border-left: 4px solid #3498db;
                    padding: 15px;
                    margin-bottom: 15px;
                    border-radius: 4px;
                }}
                
                .interpretation-box.good {{
                    background-color: #d5f4e6;
                    border-left-color: #27ae60;
                }}
                
                .interpretation-box.warning {{
                    background-color: #fff3cd;
                    border-left-color: #f39c12;
                }}
                
                .interpretation-box.danger {{
                    background-color: #f8d7da;
                    border-left-color: #e74c3c;
                }}
                
                .chart-container {{
                    margin: 30px 0;
                    padding: 20px;
                    background-color: #f9f9f9;
                    border-radius: 8px;
                }}
                
                .footer {{
                    margin-top: 50px;
                    padding-top: 20px;
                    border-top: 2px solid #ddd;
                    text-align: center;
                    color: #999;
                    font-size: 12px;
                }}
                
                .recommendation {{
                    padding: 15px;
                    margin-bottom: 10px;
                    background-color: #e8f4f8;
                    border-left: 4px solid #3498db;
                    border-radius: 4px;
                }}
                
                @media print {{
                    body {{
                        background-color: white;
                    }}
                    .container {{
                        box-shadow: none;
                    }}
                    .page-break {{
                        page-break-after: always;
                    }}
                }}
            </style>
        </head>
        <body>
            <div class="container">
        """
        
        # Add header
        html_content += f"""
                <div class="header">
                    <h1>Noise Analysis Report</h1>
                    <p>Report Type: {report_type.upper()}</p>
                    <p>Generated on: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
                </div>
        """
        
        # Add content based on report type
        if report_type in ['comprehensive', 'summary']:
            html_content += self._add_executive_summary(analysis)
            html_content += self._add_methods_section(analysis)
            html_content += self._add_environmental_metrics_section(analysis)
            html_content += self._add_standards_thresholds_section(standards)
            html_content += self._add_statistics_section(analysis)
            if report_type == 'comprehensive':
                html_content += self._add_compliance_section(analysis)
                html_content += self._add_iso_epa_section(standards)
                html_content += self._add_key_visuals(charts)
                html_content += self._add_detailed_analysis(analysis)
        
        elif report_type == 'iso':
            html_content += self._add_iso_section(standards)
        
        elif report_type == 'epa':
            html_content += self._add_epa_section(standards)
        
        # Add recommendations
        html_content += self._add_recommendations(standards)

        # Add final informational section (community thresholds + occupational exposure guidance)
        html_content += self._add_noise_thresholds_exposure_info_section()
        
        # Add footer
        html_content += """
                <div class="footer">
                    <p>This report includes derived metrics and guideline references (e.g., WHO 2018). Jurisdictional limits may differ.</p>
                    <p>For professional acoustic assessment, consult a certified acoustic engineer.</p>
                </div>
            </div>
        </body>
        </html>
        """
        
        # Save to file
        report_filename = f"noise_analysis_{report_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        report_dir = output_dir or os.path.dirname(self.filepath)
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, report_filename)
        
        with open(report_path, 'w', encoding='utf-8') as f:
            f.write(html_content)
        
        return report_path

    def _add_noise_thresholds_exposure_info_section(self):
        refs = self._get_noise_thresholds_exposure_references()
        epa = refs.get('epa_1974', {})
        osha = refs.get('osha_1910_95', {})
        niosh = refs.get('niosh_1998', {})

        epa_url = epa.get('url')
        epa_pdf = epa.get('pdf')
        osha_url = osha.get('url')
        niosh_url = niosh.get('url')
        niosh_pdf = niosh.get('pdf')

        html = """
                <div class="section">
                    <h2>Noise Thresholds &amp; Human Exposure (Informational)</h2>
                    <div class="interpretation-box">
                        <p><em>These are widely cited reference values to support interpretation. Applicable legal limits vary by jurisdiction, land use zoning, and permitting/industrial context.</em></p>
                    </div>
        """

        # Community / residential references (EPA)
        html += "<h3>Community / Residential Reference Levels</h3>"
        if epa_url:
            html += f"<p>Source: <a href=\"{epa_url}\" target=\"_blank\" rel=\"noopener noreferrer\">{epa.get('title','US EPA (1974)')}</a>"
            if epa_pdf:
                html += f" (<a href=\"{epa_pdf}\" target=\"_blank\" rel=\"noopener noreferrer\">PDF</a>)"
            html += "</p>"

        html += '<table><tr><th>Context</th><th>Metric</th><th>Reference level</th><th>Notes</th></tr>'
        for row in (epa.get('community', []) or []):
            html += (
                f"<tr><td>{row.get('context','')}</td><td>{row.get('metric','')}</td>"
                f"<td>{row.get('level','')}</td><td>Informational reference (not a universal legal limit)</td></tr>"
            )
        html += '</table>'

        # Occupational exposure guidance
        html += "<h3 style=\"margin-top: 20px;\">Occupational Exposure (Workplace)</h3>"
        if osha_url:
            html += f"<p>OSHA: <a href=\"{osha_url}\" target=\"_blank\" rel=\"noopener noreferrer\">{osha.get('title','29 CFR 1910.95')}</a></p>"
        if niosh_url:
            html += f"<p>NIOSH: <a href=\"{niosh_url}\" target=\"_blank\" rel=\"noopener noreferrer\">{niosh.get('title','NIOSH (1998)')}</a>"
            if niosh_pdf:
                html += f" (<a href=\"{niosh_pdf}\" target=\"_blank\" rel=\"noopener noreferrer\">PDF</a>)"
            html += "</p>"

        html += '<table><tr><th>Organization</th><th>Limit / Trigger</th><th>Interpretation</th></tr>'
        for row in (osha.get('occupational', []) or []):
            html += f"<tr><td>{row.get('organization','')}</td><td>{row.get('limit','')}</td><td>{row.get('meaning','')}</td></tr>"
        rel = niosh.get('rel') or {}
        if rel:
            html += f"<tr><td>{rel.get('organization','')}</td><td>{rel.get('limit','')}</td><td>{rel.get('meaning','')}</td></tr>"
        html += '</table>'

        html += """
                    <p style="margin-top: 12px;"><strong>Exposure duration matters:</strong> OSHA Table G-16 provides permitted durations that decrease as dB increases (e.g., 8h at 90 dBA; 4h at 95 dBA; 2h at 100 dBA; 1h at 105 dBA; 0.5h at 110 dBA; 0.25h at 115 dBA).</p>
                </div>
        """

        return html

    def _add_methods_section(self, analysis):
        """Add methods/definitions for research-grade clarity."""
        html = """
                <div class="section">
                    <h2>Methods & Definitions</h2>
                    <div class="interpretation-box">
                        <p><strong>LAeq (energy-average):</strong> Aggregates are computed in the energy domain:
                        <span style="font-family: monospace;">LAeq = 10·log10(mean(10^(L/10)))</span>.</p>
                        <p><strong>Exceedance levels (Lx):</strong> L5/L10/L50/L90/L95 are reported as levels exceeded x% of the time.</p>
                                                <p><strong>Day/evening/night windows:</strong> When timestamps exist, we compute windowed LAeq values and derived indicators.</p>
                                                <ul>
                                                    <li><strong>Ldn</strong> uses day 07:00–22:00 and night 22:00–07:00 (+10 dB night penalty).</li>
                                                    <li><strong>Lnight/Lden</strong> use night 23:00–07:00, with Lden using day 07:00–19:00 and evening 19:00–23:00 (+5 dB evening, +10 dB night).</li>
                                                </ul>
                                                <p>If sampling intervals vary, results assume equal time-weight per sample.</p>
                    </div>
                </div>
        """
        return html

    def _add_environmental_metrics_section(self, analysis):
        metrics = analysis.get('environmental_metrics', {}) or {}
        if not metrics:
            return ""

        html = """
                <div class="section">
                    <h2>Environmental Noise Metrics</h2>
        """

        for col_name, m in metrics.items():
            if not isinstance(m, dict) or not m:
                continue

            html += f"<h3>{col_name}</h3>"
            html += '<table><tr><th>Metric</th><th>Value (dB(A))</th></tr>'
            for key in [
                'LAeq_24h',
                'LAeq_day_ldn', 'LAeq_night_ldn',
                'LAeq_day_lden', 'LAeq_evening_lden', 'LAeq_night_lden',
                'LAeq_day', 'LAeq_evening', 'LAeq_night',
                'Lnight', 'Ldn', 'Lden'
            ]:
                if key in m:
                    html += f"<tr><td>{key}</td><td>{m[key]}</td></tr>"
            html += '</table>'

        html += '</div>'
        return html

    def _add_standards_thresholds_section(self, standards):
        who = None
        if isinstance(standards, dict):
            who = standards.get('who_2018')
        if not who:
            who = who_2018_environmental_noise_guideline_levels()

        source = (who or {}).get('source', {})
        guidelines = (who or {}).get('guidelines', {})
        if not guidelines:
            return ""

        html = """
                <div class="section">
                    <h2>Standards &amp; Thresholds (Guidelines)</h2>
        """

        title = source.get('title', 'Environmental Noise Guidelines')
        year = source.get('year', 2018)
        url = source.get('url')
        html += f"<p><strong>WHO {year}</strong> — {title}</p>"
        if url:
            html += f"<p>Source: <a href=\"{url}\" target=\"_blank\" rel=\"noopener noreferrer\">{url}</a></p>"
        disclaimer = (who or {}).get('disclaimer')
        if disclaimer:
            html += f"<p><em>{disclaimer}</em></p>"

        html += '<table><tr><th>Noise source</th><th>Metric</th><th>Guideline (dB)</th><th>Strength</th></tr>'
        for source_key, entry in guidelines.items():
            metrics_db = (entry or {}).get('metrics_db', {})
            strength = (entry or {}).get('recommendation_strength', 'N/A')
            for metric, limit in (metrics_db or {}).items():
                html += f"<tr><td>{str(source_key).replace('_', ' ')}</td><td>{metric}</td><td>{limit}</td><td>{strength}</td></tr>"
        html += '</table>'

        html += '</div>'
        return html
    
    def _add_executive_summary(self, analysis):
        """Add executive summary section"""
        html = """
                <div class="section page-break">
                    <h2>Executive Summary</h2>
        """
        
        stats = analysis.get('statistics', {})
        
        # Add summary statistics
        html += '<div class="stats-grid">'
        
        for col_name, col_stats in stats.items():
            mean = col_stats.get('mean', 0)
            
            # Determine color
            if mean < 55:
                css_class = ''
            elif mean < 70:
                css_class = 'warning'
            else:
                css_class = 'danger'
            
            html += f"""
                <div class="stat-box {css_class}">
                    <div class="stat-label">{col_name}</div>
                    <div class="stat-value">{mean}<span class="stat-unit">dB(A)</span></div>
                    <div style="font-size: 12px; margin-top: 8px; color: #666;">
                        LAeq (energy-average), Range: {col_stats.get('min', 0)} - {col_stats.get('max', 0)} dB
                    </div>
                </div>
            """
        
        html += '</div>'
        
        # Add interpretations
        interpretations = analysis.get('interpretations', [])
        for interp in interpretations:
            html += f'<div class="interpretation-box">{interp}</div>'
        
        html += '</div>'
        return html
    
    def _add_statistics_section(self, analysis):
        """Add statistics section"""
        html = """
                <div class="section">
                    <h2>Statistical Analysis</h2>
        """
        
        stats = analysis.get('statistics', {})
        percentiles = analysis.get('percentiles', {})
        
        for col_name in stats.keys():
            col_stats = stats[col_name]
            col_percentiles = percentiles.get(col_name, {})
            
            html += f'<h3>{col_name}</h3>'
            html += '<table><tr><th>Metric</th><th>Value (dB)</th></tr>'
            html += f"<tr><td>LAeq (energy-average)</td><td>{col_stats.get('laeq_db', col_stats.get('mean', 'N/A'))}</td></tr>"
            html += f"<tr><td>Arithmetic mean (for reference)</td><td>{col_stats.get('mean_arithmetic_db', 'N/A')}</td></tr>"
            html += f"<tr><td>Median (L50)</td><td>{col_stats['median']}</td></tr>"
            html += f"<tr><td>Standard Deviation</td><td>{col_stats['std_dev']}</td></tr>"
            html += f"<tr><td>Minimum</td><td>{col_stats['min']}</td></tr>"
            html += f"<tr><td>Maximum</td><td>{col_stats['max']}</td></tr>"
            html += f"<tr><td>Range</td><td>{col_stats['range']}</td></tr>"
            html += f"<tr><td>Interquartile Range</td><td>{col_stats['interquartile_range']}</td></tr>"
            
            html += '<tr style="background-color: #f0f0f0;"><td colspan="2"><strong>Percentile Levels</strong></td></tr>'
            html += f"<tr><td>L5 (Exceeded 5% of time)</td><td>{col_percentiles.get('L5', 'N/A')}</td></tr>"
            html += f"<tr><td>L10 (Exceeded 10% of time)</td><td>{col_percentiles.get('L10', 'N/A')}</td></tr>"
            html += f"<tr><td>L50 (Median)</td><td>{col_percentiles.get('L50', 'N/A')}</td></tr>"
            html += f"<tr><td>L90 (Exceeded 90% of time)</td><td>{col_percentiles.get('L90', 'N/A')}</td></tr>"
            html += f"<tr><td>L95 (Exceeded 95% of time)</td><td>{col_percentiles.get('L95', 'N/A')}</td></tr>"
            html += '</table>'
        
        html += '</div>'
        return html
    
    def _add_compliance_section(self, analysis):
        """Add compliance section"""
        html = """
                <div class="section">
                    <h2>Compliance Assessment</h2>
        """
        
        compliance = analysis.get('compliance', {}) or {}

        # Simple reference limits for the quick PASS/FAIL checks produced by NoiseAnalyzer.
        quick_limits = {
            'residential_day': 55,
            'residential_night': 45,
            'commercial_day': 65,
            'commercial_night': 55,
            'industrial': 75,
            'highway': 70,
        }
        
        for col_name, col_compliance in compliance.items():
            html += f'<h3>{col_name}</h3>'
            html += '<table><tr><th>Area Type</th><th>Limit (dB)</th><th>Status</th><th>Exceeded By (dB)</th></tr>'
            
            for area_type, status_info in col_compliance.items():
                if area_type == 'current_leq':
                    continue

                limit = quick_limits.get(area_type, 'N/A')
                status = status_info if isinstance(status_info, str) else (status_info.get('status', 'N/A') if isinstance(status_info, dict) else 'N/A')
                status_class = 'pass' if status in ('PASS', 'COMPLIANT') else 'fail'

                # If we have a numeric current LAeq, compute exceedance.
                exceeded = 0
                try:
                    current = float(col_compliance.get('current_leq'))
                    if isinstance(limit, (int, float)):
                        exceeded = max(0.0, current - float(limit))
                except Exception:
                    exceeded = 0

                html += f'<tr><td>{area_type}</td><td>{limit}</td><td class="{status_class}">{status}</td><td>{round(float(exceeded), 2) if exceeded else 0}</td></tr>'
            
            html += '</table>'
        
        html += '</div>'
        return html

    def _add_key_visuals(self, charts: AdvancedChartGenerator):
        """Embed key visuals directly in the comprehensive report."""
        html = """
                <div class="section page-break">
                    <h2>Key Visualizations</h2>
                    <p>These plots are generated from your dataset and reflect the analysis metrics used in this report.</p>
        """

        figs = []
        # Prefer time-aware charts when available.
        ts = charts.generate_time_series_heatmap()
        if ts:
            figs.append(("Time Series", ts))
        diurnal = charts.generate_diurnal_box_whisker_chart()
        if diurnal:
            figs.append(("Diurnal Box-and-Whisker", diurnal))
        hm = charts.generate_heatmap_hourly()
        if hm:
            figs.append(("Hour × Date Heatmap", hm))
        dist = charts.generate_distribution_heatmap()
        if dist:
            figs.append(("Distribution of Noise Levels", dist))

        for title, fig in figs:
            html += f'<div class="chart-container"><h3>{title}</h3>'
            # Embed the plot
            plot_div = fig.to_html(full_html=False, include_plotlyjs='cdn')
            html += plot_div
            html += '</div>'

        html += '</div>'
        return html
    
    def _add_detailed_analysis(self, analysis):
        """Add detailed analysis section"""
        html = """
                <div class="section">
                    <h2>Detailed Analysis</h2>
        """
        
        # Add hourly analysis
        hourly = analysis.get('hourly_analysis', {})
        if hourly:
            html += '<h3>Hourly Average Noise Levels (LAeq)</h3>'
            html += '<table><tr><th>Hour</th>'
            for col_name in self.analyzer.noise_columns:
                html += f'<th>{col_name} (dB)</th>'
            html += '</tr>'
            
            for hour, data in hourly.items():
                html += f'<tr><td>{hour}:00 - {hour}:59</td>'
                for col_name in self.analyzer.noise_columns:
                    html += f"<td>{data.get(col_name, {}).get('laeq_db', 'N/A')}</td>"
                html += '</tr>'
            html += '</table>'
        
        html += '</div>'
        return html
    
    def _add_iso_epa_section(self, standards):
        """Add standards context (ISO methodology + OSHA occupational)."""
        html = """
                <div class="section">
                    <h2>Standards Context (ISO/OSHA)</h2>
        """

        iso = standards.get('iso_analysis', {}) if isinstance(standards, dict) else {}
        if iso:
            html += '<h3>ISO 1996</h3>'
            if iso.get('standard'):
                html += f"<p><strong>{iso.get('standard')}</strong></p>"
            if iso.get('note'):
                html += f"<p>{iso.get('note')}</p>"

        epa = standards.get('epa_analysis', {}) if isinstance(standards, dict) else {}
        osha = (epa or {}).get('osha_pel_compliance', {}) if isinstance(epa, dict) else {}
        if osha:
            html += '<h3>OSHA (Occupational Exposure)</h3>'
            for col_name, info in osha.items():
                html += f'<h4>{col_name}</h4>'
                html += '<table><tr><th>Metric</th><th>Value</th></tr>'
                rows = [
                    ('8h PEL (TWA)', info.get('permissible_exposure_limit_8h_twa')),
                    ('Action level', info.get('action_level')),
                    ('Measured (LAeq)', info.get('measured_level')),
                    ('OSHA compliance', info.get('osha_compliance')),
                    ('Action level status', info.get('action_level_status')),
                    ('PPE required', info.get('ppe_required')),
                ]
                for k, v in rows:
                    html += f"<tr><td>{k}</td><td>{v}</td></tr>"
                html += '</table>'
        
        html += '</div>'
        return html
    
    def _add_iso_section(self, standards):
        """Add ISO section"""
        return self._add_iso_epa_section({'iso_analysis': (standards or {}).get('iso_analysis', {})})
    
    def _add_epa_section(self, standards):
        """Add EPA section"""
        return self._add_iso_epa_section({'epa_analysis': (standards or {}).get('epa_analysis', {})})
    
    def _add_recommendations(self, standards):
        """Add recommendations section"""
        html = """
                <div class="section">
                    <h2>Recommendations</h2>
        """
        
        recommendations = standards.get('recommendations', [])
        if recommendations:
            for rec in recommendations:
                html += f'<div class="recommendation">{rec}</div>'
        else:
            html += '<p>No specific recommendations generated based on the analysis.</p>'
        
        html += '</div>'
        return html
