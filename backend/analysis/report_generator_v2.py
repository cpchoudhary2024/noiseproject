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
from analysis.gap_detector import detect_gaps, gap_report_to_dict, data_completeness_pct, _modal_interval_seconds
from analysis.compliance_matrix import evaluate_compliance
import io
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak, Table, TableStyle, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from xml.sax.saxutils import escape

class ReportGeneratorV2:
    """
    Publication-grade environmental noise analysis report.
    
    Implements 8-section architecture:
    1. Executive Acoustic Summary
    2. Data Quality & Completeness (QA/QC)
    3. Global Regulatory & Health Compliance (LEQ only)
    4. Single-Event Sleep Disturbance (L-Max only)
    5. Daily Summary Matrix (Multi-week variance)
    6. Diurnal Hourly Profile (24-hour cycle)
    7. Statistical Noise Profile (Percentiles)
    8. Advanced Visualizations (Time series, box-and-whisker, heatmap)
    """
    
    def __init__(self, df, filepath, analysis=None, standards=None, daily_summary=None, hourly_summary=None,
                 device_id: str = '', source_files: list | None = None, merge_gap_report: dict | None = None,
                 custom_section_heading: str = '', custom_section_body: str = '', environment: str = 'outdoor'):
        """
        Initialize report generator with ONLY the uploaded data.
        NO external CSV file loading - all summaries computed from df.

        environment : 'outdoor' (default) or 'indoor' — controls whether the WHO
        indoor bedroom guidelines are evaluated in the compliance matrix.
        """
        self.df = df.copy()
        self.filepath = filepath
        self.device_id = str(device_id or '').strip()
        self.source_files = list(source_files or [])
        self.merge_gap_report = merge_gap_report  # pre-computed gap dict from the merge step
        self.custom_section_heading = str(custom_section_heading or '').strip()
        self.custom_section_body = str(custom_section_body or '').strip()
        self.environment = str(environment or 'outdoor').strip().lower()
        self.timestamps_synthetic = False  # set True if no real timestamps could be read
        self.timestamp_integrity = None
        self.analyzer = NoiseAnalyzer(df)
        self.standards = StandardsAnalyzer(df)
        self._analysis = analysis
        self._standards = standards
        
        # Convert numeric columns
        for col in self.analyzer.noise_columns:
            self.df[col] = pd.to_numeric(self.df[col], errors='coerce')
        
        # Compute summaries from actual data (NOT external CSVs)
        self.daily_summary = daily_summary
        self.hourly_summary = hourly_summary
        if self.daily_summary is None or self.hourly_summary is None:
            self._compute_summaries()

    def _get_analysis(self):
        if self._analysis is None:
            self._analysis = self.analyzer.comprehensive_analysis()
        return self._analysis

    def _get_standards(self):
        if self._standards is None:
            self._standards = self.standards.analyze()
        return self._standards

    # ============================================================
    # PLAIN-ENGLISH DATASET SUMMARY (template-based, no AI)
    # Sources: WHO Environmental Noise Guidelines (2018), Table 1–3
    #          WHO Guidelines for Community Noise (1999)
    # ============================================================

    @staticmethod
    def generate_plain_english_summary(
        *,
        laeq: float | None,
        lden: float | None = None,
        lnight: float | None = None,
        laeq_day: float | None = None,
        laeq_night: float | None = None,
        laeq_min: float | None = None,
        laeq_max: float | None = None,
        l10: float | None = None,
        l90: float | None = None,
        start_date: str = '',
        end_date: str = '',
        duration_label: str = '',
        data_completeness_pct: float | None = None,
        n_days: int = 0,
    ) -> str:
        """
        Returns a structured multi-paragraph plain-English summary.

        Sections are separated by double newlines (\\n\\n).
        Bullet points use the '  • ' prefix so renderers can convert them to <li>.

        All thresholds sourced from WHO Environmental Noise Guidelines 2018 (Tables 1-3)
        and WHO Guidelines for Community Noise (Berglund et al., 1999).
        """
        def _f(v, d=1):
            try:
                return f"{float(v):.{d}f}" if v is not None and np.isfinite(float(v)) else None
            except Exception:
                return None

        def _v(val):
            try:
                return float(val) if val is not None and np.isfinite(float(val)) else None
            except Exception:
                return None

        laeq_v = _v(laeq)
        if laeq_v is None:
            return (
                "A plain-English summary could not be generated because the average noise level "
                "(LAeq) could not be computed. Please check that the dataset contains valid "
                "numeric measurements."
            )

        # ── 1. Opening paragraph ─────────────────────────────────────────────
        date_ctx = ""
        if start_date and end_date:
            date_ctx = f" from {start_date} to {end_date}"
        elif duration_label:
            date_ctx = f" over {duration_label}"

        day_word = (f"{n_days} day{'s' if n_days != 1 else ''}" if n_days > 0
                    else duration_label or "the measurement period")

        completeness_note = ""
        if data_completeness_pct is not None:
            try:
                cp = float(data_completeness_pct)
                if cp < 90:
                    completeness_note = (
                        f" Data completeness was {cp:.0f}%, meaning gaps exist in the record; "
                        f"averages may slightly under- or over-estimate true exposure."
                    )
                else:
                    completeness_note = f" Data completeness was {cp:.0f}%."
            except Exception:
                pass

        para1 = (
            f"This dataset covers {day_word} of continuous outdoor noise monitoring{date_ctx}."
            f"{completeness_note}"
        )

        # ── 2. Noise level paragraph ─────────────────────────────────────────
        # Level labels and analogies from ISO 226 reference levels and
        # Berglund et al. (1999) / WHO 2018 explanatory notes.
        if laeq_v < 40:
            level_label   = "very quiet"
            level_analogy = "comparable to a rural area at night or a library reading room"
        elif laeq_v < 50:
            level_label   = "quiet"
            level_analogy = "comparable to a calm residential street at night or soft rainfall"
        elif laeq_v < 55:
            level_label   = "moderate"
            level_analogy = "comparable to a typical residential neighbourhood during the day"
        elif laeq_v < 65:
            level_label   = "elevated"
            level_analogy = "comparable to a busy urban street or a bustling café"
        elif laeq_v < 75:
            level_label   = "high"
            level_analogy = "comparable to heavy road traffic or a passing freight train"
        else:
            level_label   = "very high"
            level_analogy = "comparable to a construction zone or an expressway at close range"

        level_sentence = (
            f"The overall 24-hour energy-average level (LAeq) was {_f(laeq_v)} dB(A), "
            f"placing the acoustic environment in the {level_label} range, {level_analogy}."
        )

        # Day / night breakdown — written as separate sentences, no em-hyphens
        # Time periods follow Maryland COMAR (day 07:00-22:00, night 22:00-07:00).
        day_night_sentence = ""
        try:
            dv = _v(laeq_day)
            nv = _v(laeq_night)
            if dv is not None and nv is not None:
                diff = dv - nv
                if diff > 5:
                    day_night_sentence = (
                        f" Daytime levels (07:00–22:00) averaged {_f(dv)} dB(A) and nighttime "
                        f"levels (22:00–07:00) averaged {_f(nv)} dB(A). The {_f(abs(diff))} dB "
                        f"day-to-night difference is consistent with activity-driven or "
                        f"traffic-related noise sources."
                    )
                elif diff < -3:
                    day_night_sentence = (
                        f" Daytime levels (07:00–22:00) averaged {_f(dv)} dB(A) and nighttime "
                        f"levels (22:00–07:00) averaged {_f(nv)} dB(A). Nighttime is louder than "
                        f"daytime by {_f(abs(diff))} dB, which suggests a nocturnal noise source."
                    )
                else:
                    day_night_sentence = (
                        f" Daytime levels (07:00–22:00) averaged {_f(dv)} dB(A) and nighttime "
                        f"levels (22:00–07:00) averaged {_f(nv)} dB(A). The similar day and night "
                        f"readings are consistent with a continuous or steady-state noise source."
                    )
        except Exception:
            pass

        peak_sentence = ""
        if _v(laeq_max) is not None:
            peak_sentence = f" The highest single recorded level was {_f(_v(laeq_max))} dB(A)."

        para2 = level_sentence + day_night_sentence + peak_sentence

        # ── 3. Variability paragraph ─────────────────────────────────────────
        para3 = ""
        try:
            l10_v = _v(l10)
            l90_v = _v(l90)
            if l10_v is not None and l90_v is not None:
                spread = l10_v - l90_v
                if spread > 20:
                    para3 = (
                        f"The noise environment was highly variable. Levels exceeded 10% of the "
                        f"time (L10 = {_f(l10_v)} dB(A)) were {_f(spread)} dB above the quiet-hour "
                        f"background (L90 = {_f(l90_v)} dB(A)), pointing to frequent loud transient "
                        f"events such as passing vehicles or heavy machinery."
                    )
                elif spread > 12:
                    para3 = (
                        f"The noise environment showed moderate variability "
                        f"(L10 = {_f(l10_v)} dB(A), L90 = {_f(l90_v)} dB(A), spread = {_f(spread)} dB), "
                        f"suggesting intermittent noise sources alongside a steady background level."
                    )
                else:
                    para3 = (
                        f"The noise environment was relatively stable, with an L10-to-L90 spread "
                        f"of only {_f(spread)} dB (L10 = {_f(l10_v)} dB(A), L90 = {_f(l90_v)} dB(A)), "
                        f"consistent with a continuous or steady noise source."
                    )
        except Exception:
            pass

        # ── 4. WHO compliance bullet points ─────────────────────────────────
        WHO_LDEN_LIMIT   = 53.0   # WHO 2018, Table 1
        WHO_LNIGHT_LIMIT = 45.0   # WHO 2018, Table 1
        WHO_LOAEL_NIGHT  = 40.0   # WHO 2018, Section 4.1

        concern_level = "LOW"
        bullets = []

        lden_v = _v(lden)
        if lden_v is not None:
            if lden_v > WHO_LDEN_LIMIT:
                excess = lden_v - WHO_LDEN_LIMIT
                bullets.append(
                    f"24-hour weighted average (Lden): {_f(lden_v)} dB(A). "
                    f"Exceeds the WHO 2018 road-traffic guideline of {WHO_LDEN_LIMIT} dB(A) "
                    f"by {_f(excess)} dB."
                )
                concern_level = "HIGH" if excess >= 8 else "MODERATE-HIGH"
            else:
                bullets.append(
                    f"24-hour weighted average (Lden): {_f(lden_v)} dB(A). "
                    f"Within the WHO 2018 road-traffic guideline of {WHO_LDEN_LIMIT} dB(A)."
                )

        lnight_v = _v(lnight)
        if lnight_v is not None:
            if lnight_v > WHO_LNIGHT_LIMIT:
                excess = lnight_v - WHO_LNIGHT_LIMIT
                bullets.append(
                    f"Nighttime level (Lnight): {_f(lnight_v)} dB(A). "
                    f"Exceeds the WHO 2018 sleep-protection limit of {WHO_LNIGHT_LIMIT} dB(A) "
                    f"by {_f(excess)} dB."
                )
                if concern_level == "LOW":
                    concern_level = "MODERATE-HIGH"
            elif lnight_v > WHO_LOAEL_NIGHT:
                bullets.append(
                    f"Nighttime level (Lnight): {_f(lnight_v)} dB(A). "
                    f"Within the WHO 2018 limit of {WHO_LNIGHT_LIMIT} dB(A) but above the "
                    f"lowest-observed-adverse-effect level (LOAEL) of {WHO_LOAEL_NIGHT} dB(A), "
                    f"at which initial sleep disturbance effects begin."
                )
                if concern_level == "LOW":
                    concern_level = "MODERATE"
            else:
                bullets.append(
                    f"Nighttime level (Lnight): {_f(lnight_v)} dB(A). "
                    f"Below the WHO 2018 LOAEL of {WHO_LOAEL_NIGHT} dB(A). "
                    f"No sleep effects are expected at this level."
                )

        if not bullets:
            if laeq_v > 65:
                concern_level = "HIGH"
                bullets.append(
                    f"Average level of {_f(laeq_v)} dB(A) suggests likely exceedance of WHO health "
                    f"guidelines. Note: Lden/Lnight could not be computed as no timestamp data was available."
                )
            elif laeq_v > 53:
                concern_level = "MODERATE"
                bullets.append(
                    f"Average level of {_f(laeq_v)} dB(A) falls in a range that may exceed WHO Lden "
                    f"guidelines. Note: Lden/Lnight could not be computed from this dataset."
                )
            else:
                concern_level = "LOW"
                bullets.append(
                    f"Average level of {_f(laeq_v)} dB(A) is below the WHO Lden "
                    f"threshold of {WHO_LDEN_LIMIT} dB(A)."
                )

        if concern_level == "MODERATE-HIGH":
            concern_level = "HIGH"

        bullet_lines = "\n".join(f"  • {b}" for b in bullets)
        para4 = f"WHO 2018 Health Guideline Compliance:\n{bullet_lines}"

        # ── 5. Concern-level paragraph ───────────────────────────────────────
        concern_map = {
            "LOW": (
                "The acoustic environment is generally within WHO health-based guidelines. "
                "No immediate action is indicated, but periodic re-monitoring is advisable."
            ),
            "MODERATE": (
                "Noise levels are within WHO guidelines but above the LOAEL for nighttime sleep "
                "effects. Continued monitoring is recommended, particularly for sensitive occupants "
                "such as children or elderly residents."
            ),
            "HIGH": (
                "WHO 2018 health-based guidelines are exceeded. Based on WHO evidence, prolonged "
                "exposure at these levels is associated with increased risk of cardiovascular effects "
                "(hypertension, ischaemic heart disease) and impaired sleep quality. Professional "
                "acoustic assessment and noise-reduction measures are recommended."
            ),
            "SERIOUS": (
                "Noise levels significantly exceed WHO guidelines. WHO 2018 identifies strong "
                "cardiovascular and sleep health risks at these levels. Immediate professional "
                "acoustic assessment is strongly recommended."
            ),
        }
        if concern_level not in concern_map:
            concern_level = "HIGH" if laeq_v > 55 else "MODERATE"

        para5 = f"Overall Concern Level: {concern_level}. {concern_map[concern_level]}"

        # ── Assemble with paragraph separators ──────────────────────────────
        parts = [para1, para2]
        if para3:
            parts.append(para3)
        parts.append(para4)
        parts.append(para5)
        return "\n\n".join(parts)

    def _compute_summaries(self):
        """
        Compute daily and hourly summaries DIRECTLY from the uploaded dataset.
        This ensures 100% data accuracy and isolation.
        """
        ts_col, leq_col, lmax_col, lmin_col = self._resolve_acoustic_columns()
        
        if not leq_col or leq_col not in self.df.columns:
            print("[Report] No LEQ column found; cannot compute summaries")
            return
        
        ts = self._get_timestamp_series(ts_col)
        leq = self._get_numeric_series(leq_col)
        
        if ts.empty or leq.empty:
            print("[Report] Insufficient timestamp or LEQ data; cannot compute summaries")
            return
        
        df_data = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
        if df_data.empty:
            print("[Report] No valid ts/leq pairs; cannot compute summaries")
            return
        
        # ========== DAILY SUMMARY (COMPUTED) ==========
        df_data['date'] = df_data['ts'].dt.date
        
        daily_rows = []
        for date, group in df_data.groupby('date'):
            leq_vals = group['leq']
            
            laeq_24h = energetic_mean_db(leq_vals)
            
            # Day (07:00-22:00) and Night (22:00-07:00) subdivisions
            h = group['ts'].dt.hour
            is_day = (h >= 7) & (h < 22)
            is_night = ~is_day
            
            laeq_day = energetic_mean_db(leq_vals[is_day]) if is_day.any() else None
            laeq_night = energetic_mean_db(leq_vals[is_night]) if is_night.any() else None
            
            # Lden for the day (with penalties)
            _lden_out = compute_ldn_lden(group['ts'][is_day], leq_vals[is_day]) if is_day.any() else None
            lden_day = _lden_out.get('Lden') if _lden_out else None
            
            daily_rows.append({
                'Date': pd.Timestamp(date),
                'Average_L_EQ_dB': laeq_24h,
                'Min_L_EQ_dB': leq_vals.min(),
                'Max_L_EQ_dB': leq_vals.max(),
                'Std_Dev': leq_vals.std(),
                'Daytime_LAeq': laeq_day,
                'Nighttime_LAeq': laeq_night,
                'Daily_Lden': lden_day,
            })
        
        if daily_rows:
            self.daily_summary = pd.DataFrame(daily_rows)
            print(f"[Report] Computed daily summary: {len(self.daily_summary)} days")
        
        # ========== HOURLY SUMMARY (COMPUTED) ==========
        df_data['hour'] = df_data['ts'].dt.hour
        
        hourly_rows = []
        for hour, group in df_data.groupby('hour'):
            leq_vals = group['leq']
            
            laeq_hourly = energetic_mean_db(leq_vals)
            
            hourly_rows.append({
                'Hour': hour,
                'Average_L_EQ_dB': laeq_hourly,
                'Min_L_EQ_dB': leq_vals.min(),
                'Max_L_EQ_dB': leq_vals.max(),
                'Std_Dev': leq_vals.std(),
            })
        
        if hourly_rows:
            # Sort by hour to ensure 0-23 ordering
            self.hourly_summary = pd.DataFrame(hourly_rows).sort_values('Hour').reset_index(drop=True)
            print(f"[Report] Computed hourly summary: {len(self.hourly_summary)} hours")
    
    def generate_pdf_report(self, report_type='comprehensive', output_dir: str | None = None):
        """Generate an 8-section publication-grade PDF report."""
        
        report_filename = f"noise_analysis_{report_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        report_dir = output_dir or os.path.dirname(self.filepath)
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, report_filename)

        doc = SimpleDocTemplate(report_path, pagesize=(11*inch, 8.5*inch), 
                               topMargin=0.5*inch, bottomMargin=0.5*inch, 
                               leftMargin=0.75*inch, rightMargin=0.75*inch)
        styles = self._get_pdf_styles()
        story = []

        # Header
        self._add_pdf_header(story, styles, report_type)

        ts_col, leq_col, lmax_col, lmin_col = self._resolve_acoustic_columns()
        ts = self._get_timestamp_series(ts_col)

        # Timestamp-integrity warning — fabricated dates were substituted.
        if self.timestamps_synthetic:
            warn_style = ParagraphStyle(
                'TsWarn', parent=styles['BodyText'], fontSize=9, leading=12,
                backColor=colors.HexColor('#FEF2F2'), borderColor=colors.HexColor('#DC2626'),
                borderWidth=1, borderPadding=(8, 10, 8, 10), textColor=colors.HexColor('#7F1D1D'),
            )
            story.append(Paragraph(
                "<b>⚠ Timestamps could not be read from this file.</b> The original date/time column "
                "was missing or corrupted, so date- and time-based results in this report "
                "(Lden, Lnight, the daily summary, diurnal profile, and heatmap) are based on a "
                "substituted index and should NOT be interpreted as real dates or times. Overall "
                "LAeq and statistical percentiles remain valid. Re-export the source file keeping the "
                "full 'YYYY-MM-DD HH:MM:SS' timestamp column for a complete time-based assessment.",
                warn_style
            ))
            story.append(Spacer(1, 0.18 * inch))

        # ========== 8 SECTIONS ==========
        
        # Section 1: Executive Acoustic Summary
        self._add_section_1_executive_summary(story, styles, ts=ts, leq_col=leq_col)
        story.append(Spacer(1, 0.2 * inch))
        
        # Section 2: Data Quality & Completeness
        self._add_section_2_data_quality(story, styles, ts=ts)
        story.append(Spacer(1, 0.2 * inch))
        
        # Section 3: Global Regulatory & Health Compliance
        self._add_section_3_compliance(story, styles, ts=ts, leq_col=leq_col)
        story.append(Spacer(1, 0.2 * inch))
        
        # Section 4: Single-Event Sleep Disturbance
        self._add_section_4_sleep_disturbance(story, styles, ts=ts, lmax_col=lmax_col)
        story.append(Spacer(1, 0.2 * inch))

        # Optional custom section (user-provided notes) — inserted after Section 4
        if self.custom_section_heading or self.custom_section_body:
            self._add_custom_section(story, styles)
            story.append(Spacer(1, 0.2 * inch))

        # Section 5: Daily Summary Matrix
        self._add_section_5_daily_matrix(story, styles)
        story.append(PageBreak())
        
        # Section 6: Diurnal Hourly Profile
        self._add_section_6_hourly_profile(story, styles, ts=ts)
        story.append(Spacer(1, 0.2 * inch))
        
        # Section 7: Statistical Profile
        self._add_section_7_percentiles(story, styles, leq_col=leq_col)
        story.append(Spacer(1, 0.2 * inch))
        
        # Section 8: Advanced Visualizations
        self._add_section_8_visualizations(story, styles, ts=ts, leq_col=leq_col, lmax_col=lmax_col, lmin_col=lmin_col)
        story.append(PageBreak())
        
        # Section 9: Methodological Limitations & Disclaimer
        self._add_section_9_disclaimer(story, styles)
        
        doc.build(story, onFirstPage=self._add_page_template, onLaterPages=self._add_page_template)
        
        return report_path

    def generate_html_report(self, report_type='comprehensive', output_dir: str | None = None):
        """Generate a plain-language Community Noise Report for non-expert readers.

        Designed for research-study participants: a clear headline, friendly key
        numbers, three intuitive visuals (how your noise compares, day-by-day
        trend, a typical day), plus plain-language health meaning and actions.
        No percentile tables, box-and-whisker, radar charts, or pass/fail jargon.
        """
        report_filename = f"community_noise_report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        report_dir = output_dir or os.path.dirname(self.filepath)
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, report_filename)

        ts_col, leq_col, lmax_col, lmin_col = self._resolve_acoustic_columns()
        ts  = self._get_timestamp_series(ts_col)
        leq = self._get_numeric_series(leq_col)

        ts_valid = ts.dropna()
        start_str = ts_valid.min().strftime('%d %b %Y') if not ts_valid.empty else 'N/A'
        end_str   = ts_valid.max().strftime('%d %b %Y') if not ts_valid.empty else 'N/A'
        n_days_v = int(round((ts_valid.max() - ts_valid.min()).total_seconds() / 86400)) if not ts_valid.empty else 0
        completeness = data_completeness_pct(ts_valid, actual_count=len(self.df))

        # Acoustic metrics
        leq_clean = leq.dropna()
        laeq_v   = energetic_mean_db(leq) if not leq_clean.empty else None
        env      = compute_ldn_lden(ts, leq) or {} if not leq_clean.empty else {}
        lden_v   = env.get('Lden')
        lnight_v = env.get('Lnight')
        peak_v   = float(leq_clean.max()) if not leq_clean.empty else None
        exc = exceedance_levels_db(leq_clean.to_numpy()) or {} if not leq_clean.empty else {}
        l90_v = exc.get('L90')

        # Participant-friendly headline numbers
        WHO_DAY_GUIDELINE = 53.0   # WHO 2018 road-traffic Lden guideline
        pct_within = (100.0 * float((leq_clean <= WHO_DAY_GUIDELINE).mean())
                      if not leq_clean.empty else None)

        # Loudest / quietest hour of day (from the precomputed hourly summary)
        loud_hr = quiet_hr = None
        loud_db = quiet_db = None
        if self.hourly_summary is not None and not self.hourly_summary.empty:
            hs = self.hourly_summary.dropna(subset=['Average_L_EQ_dB'])
            if not hs.empty:
                lrow = hs.loc[hs['Average_L_EQ_dB'].idxmax()]
                qrow = hs.loc[hs['Average_L_EQ_dB'].idxmin()]
                loud_hr, loud_db = int(lrow['Hour']), float(lrow['Average_L_EQ_dB'])
                quiet_hr, quiet_db = int(qrow['Hour']), float(qrow['Average_L_EQ_dB'])

        # Plain-English summary (reused; rendered as readable prose)
        h = ts.dt.hour
        is_day   = (h >= 7) & (h < 22)
        is_night = ~is_day
        laeq_day_v   = energetic_mean_db(leq[is_day])   if is_day.any()   else None
        laeq_night_v = energetic_mean_db(leq[is_night]) if is_night.any() else None
        summary_text = ReportGeneratorV2.generate_plain_english_summary(
            laeq=laeq_v, lden=lden_v, lnight=lnight_v,
            laeq_day=laeq_day_v, laeq_night=laeq_night_v,
            laeq_min=float(leq_clean.min()) if not leq_clean.empty else None,
            laeq_max=peak_v,
            l10=exc.get('L10'), l90=l90_v,
            start_date=start_str, end_date=end_str,
            duration_label=self._compute_duration_label(ts),
            data_completeness_pct=completeness, n_days=n_days_v,
        )

        # Concern level → colour
        _st_lower = summary_text.lower()
        if 'concern level: high' in _st_lower or 'concern level: serious' in _st_lower:
            concern_color, concern_bg, concern_label = '#991b1b', '#FEF2F2', 'HIGH'
        elif 'concern level: moderate' in _st_lower:
            concern_color, concern_bg, concern_label = '#92400e', '#FFFBEB', 'MODERATE'
        else:
            concern_color, concern_bg, concern_label = '#166534', '#F0FDF4', 'LOW'

        def _hfmt(v):
            try:
                fv = float(v)
                return f"{fv:.1f}" if fv is not None and np.isfinite(fv) else "N/A"
            except Exception:
                return "N/A"

        def _esc(s):
            return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        def _summary_to_html(text: str) -> str:
            """Convert structured summary text to clean single-box HTML with visual sections."""
            paras = [p.strip() for p in text.split('\n\n') if p.strip()]
            body_paras, who_para, concern_para = [], None, None
            for para in paras:
                first_line = para.split('\n')[0].strip()
                if first_line.startswith('WHO ') or first_line.startswith('WHO '):
                    who_para = para
                elif first_line.startswith('Overall Concern'):
                    concern_para = para
                else:
                    body_paras.append(para)

            parts = []

            # Concern level badge — shown first for instant visual cue
            if concern_para:
                level = 'HIGH'
                for lv in ('SERIOUS', 'HIGH', 'MODERATE', 'LOW'):
                    if lv in concern_para.upper():
                        level = lv
                        break
                badge_colors = {
                    'LOW':      ('#166534', '#dcfce7'),
                    'MODERATE': ('#92400e', '#fef3c7'),
                    'HIGH':     ('#991b1b', '#fee2e2'),
                    'SERIOUS':  ('#7f1d1d', '#fca5a5'),
                }
                badge_fg, badge_bg = badge_colors.get(level, ('#1e3a5f', '#dbeafe'))
                # Strip the "Overall Concern Level: X." prefix to get explanation text
                explanation = concern_para
                for prefix in (f'Overall Concern Level: {level}. ', f'Overall Concern Level: {level}.'):
                    if explanation.startswith(prefix):
                        explanation = explanation[len(prefix):]
                        break
                parts.append(
                    f"<div style='display:flex;align-items:flex-start;gap:12px;margin-bottom:14px;"
                    f"padding:10px 14px;background:{badge_bg};border-radius:6px;border-left:4px solid {badge_fg}'>"
                    f"<span style='font-weight:700;font-size:13px;color:{badge_fg};white-space:nowrap;"
                    f"letter-spacing:.05em;padding-top:1px'>CONCERN LEVEL: {level}</span>"
                    f"<span style='font-size:13px;color:#374151;line-height:1.55'>{_esc(explanation)}</span>"
                    f"</div>"
                )

            # Body paragraphs — flow as readable prose
            for para in body_paras:
                parts.append(
                    f"<p style='margin:0 0 10px;line-height:1.7;color:#1f2937'>{_esc(para)}</p>"
                )

            # WHO compliance section — divider + bullet list
            if who_para:
                lines = who_para.split('\n')
                bullet_lines  = [l for l in lines if l.strip().startswith('•')]
                header_lines  = [l for l in lines if not l.strip().startswith('•')]
                section_title = ' '.join(header_lines).strip()
                items = ''.join(
                    f"<li style='margin-bottom:5px;line-height:1.55'>{_esc(l.strip().lstrip('•').strip())}</li>"
                    for l in bullet_lines
                )
                parts.append(
                    f"<div style='margin-top:6px;padding-top:10px;border-top:1px solid rgba(0,0,0,0.10)'>"
                    f"<p style='margin:0 0 6px;font-weight:600;font-size:13px;color:#1e3a5f'>{_esc(section_title)}</p>"
                    f"<ul style='margin:0;padding-left:18px;color:#374151;font-size:13px'>{items}</ul>"
                    f"</div>"
                )

            return '\n'.join(parts)

        def _level_word(v):
            if v is None or not np.isfinite(v):
                return ('not available', '#64748b')
            if v < 45:  return ('quiet', '#166534')
            if v < 55:  return ('moderate', '#15803d')
            if v < 65:  return ('elevated', '#b45309')
            if v < 75:  return ('high', '#c2410c')
            return ('very high', '#991b1b')

        avg_word, avg_color = _level_word(laeq_v)

        # ── Participant-friendly charts ──
        def _chart_html(fig):
            return (fig.to_html(full_html=False, include_plotlyjs=False) if fig is not None
                    else "<p style='color:#6b7280;font-style:italic'>Chart unavailable — not enough data.</p>")
        fig_compare = self._fig_compare_to_references(laeq_v)
        fig_daily   = self._fig_daily_simple()
        fig_typical = self._fig_typical_day()

        # ── Plain-language guidance ──
        health_points, action_points = self._participant_guidance(lden_v, lnight_v, laeq_v)

        # ── Plain within-guideline verdict (uses Lden, the metric the guideline applies to) ──
        guide_metric = lden_v if (lden_v is not None and np.isfinite(lden_v)) else laeq_v
        if guide_metric is not None and np.isfinite(guide_metric):
            if guide_metric <= WHO_DAY_GUIDELINE:
                verdict_txt = (f"Your overall day-and-night noise level is {guide_metric:.0f} dB, which is "
                               f"<strong>within</strong> the World Health Organization health guideline of "
                               f"{WHO_DAY_GUIDELINE:.0f} dB.")
                verdict_bg, verdict_clr = '#dcfce7', '#166534'
            else:
                verdict_txt = (f"Your overall day-and-night noise level is {guide_metric:.0f} dB, which is "
                               f"<strong>{guide_metric - WHO_DAY_GUIDELINE:.0f} dB above</strong> the World Health "
                               f"Organization health guideline of {WHO_DAY_GUIDELINE:.0f} dB.")
                verdict_bg, verdict_clr = '#fee2e2', '#991b1b'
        else:
            verdict_txt = "An overall guideline comparison could not be computed for this dataset."
            verdict_bg, verdict_clr = '#f1f5f9', '#475569'

        def _keycard(value, unit, label, sub):
            return (
                "<div class='kc'>"
                f"<div class='kc-val'>{_esc(value)}<span class='kc-unit'>{_esc(unit)}</span></div>"
                f"<div class='kc-label'>{_esc(label)}</div>"
                f"<div class='kc-sub'>{_esc(sub)}</div>"
                "</div>"
            )

        loud_txt  = f"{loud_hr:02d}:00" if loud_hr is not None else "N/A"
        quiet_txt = f"{quiet_hr:02d}:00" if quiet_hr is not None else "N/A"

        # ════════════════════════════════════════════════════════════════════
        # PARTICIPANT-FRIENDLY ASSEMBLY
        # ════════════════════════════════════════════════════════════════════
        source_label = (_esc(", ".join(self.source_files)) if self.source_files
                        else _esc(os.path.basename(self.filepath)))
        completeness_str = f"{completeness:.0f}%" if completeness is not None else "N/A"
        place = _esc(self.device_id) if self.device_id else "this location"

        def _section(title, intro, body):
            intro_html = f"<p class='sec-intro'>{intro}</p>" if intro else ""
            return f"<section class='card'><h2>{_esc(title)}</h2>{intro_html}{body}</section>"

        # Key-number cards
        key_cards = "".join([
            _keycard(_hfmt(laeq_v), " dB", "Average noise level",
                     f"{avg_word} — the steady level with the same energy as the real noise"),
            _keycard(quiet_txt, "", "Quietest time of day",
                     (f"around {quiet_db:.0f} dB" if quiet_db is not None else "")),
            _keycard(loud_txt, "", "Loudest time of day",
                     (f"around {loud_db:.0f} dB" if loud_db is not None else "")),
            _keycard((f"{pct_within:.0f}" if pct_within is not None else "N/A"), "%",
                     "Time within the health guideline",
                     "share of time at or below the WHO 53 dB level"),
        ])

        # Health & action bullet lists
        health_html = "<ul class='plain-list'>" + "".join(
            f"<li>{_esc(p)}</li>" for p in health_points) + "</ul>"
        action_html = "<ul class='plain-list'>" + "".join(
            f"<li>{_esc(p)}</li>" for p in action_points) + "</ul>"

        # Timestamp warning (only when dates were unreadable)
        ts_warn_html = ""
        if getattr(self, 'timestamps_synthetic', False):
            ts_warn_html = (
                "<div class='card warn'><strong>⚠ Timestamps could not be read from this file.</strong>"
                "<p>The date/time information was missing or unreadable, so the day-by-day trend and "
                "typical-day chart below are based on a substituted order and should not be read as real "
                "dates or times. The average levels remain valid.</p></div>"
            )

        # Optional custom notes from the user
        custom_html = ""
        if self.custom_section_heading or self.custom_section_body:
            body = "".join(f"<p>{_esc(ln)}</p>" for ln in (self.custom_section_body or '').splitlines() if ln.strip())
            custom_html = _section(self.custom_section_heading or "Additional Notes", "", body)

        html_parts = [
            "<!DOCTYPE html>", "<html lang='en'>", "<head>",
            "  <meta charset='UTF-8'>",
            "  <meta name='viewport' content='width=device-width, initial-scale=1.0'>",
            "  <title>Community Noise Report</title>",
            "  <script src='https://cdn.plot.ly/plotly-latest.min.js'></script>",
            "  <style>",
            "    *{box-sizing:border-box;margin:0;padding:0}",
            "    body{font-family:'Inter',-apple-system,Segoe UI,Arial,sans-serif;background:#eef2f7;color:#1e293b;line-height:1.6;font-size:15px}",
            "    .wrap{max-width:920px;margin:0 auto;padding:28px 20px 60px}",
            "    .card{background:#fff;border:1px solid #e3e9f1;border-radius:16px;box-shadow:0 4px 14px rgba(15,37,64,.06);padding:30px 32px;margin-bottom:22px}",
            "    .hero{background:linear-gradient(115deg,#13294a 0%,#1e3a5f 55%,#244c77 100%);color:#fff;border:none}",
            "    .hero h1{font-size:30px;font-weight:800;letter-spacing:-.02em;margin-bottom:6px}",
            "    .hero .sub{opacity:.9;font-size:14px}",
            "    .hero .meta{margin-top:14px;font-size:13px;opacity:.85;display:flex;flex-wrap:wrap;gap:6px 22px}",
            "    h2{font-size:20px;color:#13294a;letter-spacing:-.01em;margin-bottom:6px}",
            "    .sec-intro{color:#64748b;font-size:14px;margin-bottom:16px}",
            "    .badge{display:inline-block;font-weight:800;font-size:13px;letter-spacing:.05em;padding:5px 12px;border-radius:999px;margin-bottom:14px}",
            "    .summary p{margin:0 0 10px;color:#334155}",
            "    .summary ul{margin:6px 0 0 18px;color:#334155;font-size:14px}",
            "    .keygrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px}",
            "    .kc{background:#f7f9fc;border:1px solid #e3e9f1;border-radius:13px;padding:18px 18px}",
            "    .kc-val{font-family:'JetBrains Mono',monospace;font-size:30px;font-weight:700;color:#1e3a5f;letter-spacing:-.02em}",
            "    .kc-unit{font-size:14px;font-weight:600;color:#64748b;margin-left:3px}",
            "    .kc-label{font-weight:700;font-size:13px;color:#334155;margin-top:6px}",
            "    .kc-sub{font-size:12px;color:#94a3b8;margin-top:3px;line-height:1.45}",
            "    .verdict{padding:14px 18px;border-radius:11px;font-size:15px;margin-bottom:18px}",
            "    .plain-list{margin:0;padding-left:20px}",
            "    .plain-list li{margin-bottom:9px;color:#334155}",
            "    .warn{background:#fef2f2;border-color:#fca5a5;border-left:5px solid #dc2626}",
            "    .warn strong{color:#991b1b}.warn p{color:#7f1d1d;font-size:14px;margin-top:6px}",
            "    .chart-note{font-size:13px;color:#64748b;margin-top:8px;line-height:1.5}",
            "    .about{font-size:13px;color:#475569}.about div{padding:5px 0;border-bottom:1px solid #eef2f7;display:flex;gap:10px}",
            "    .about b{min-width:170px;color:#334155}",
            "    .footer{text-align:center;font-size:12px;color:#94a3b8;padding:22px 10px}",
            "    .disclaimer{font-size:12px;color:#94a3b8;line-height:1.6;margin-top:12px}",
            "  </style>", "</head>", "<body>", "  <div class='wrap'>",

            # ── Hero header ──
            "  <section class='card hero'>",
            "    <h1>Community Noise Report</h1>",
            f"    <div class='sub'>A plain-language summary of the noise measured at {place}.</div>",
            "    <div class='meta'>"
            f"<span>📍 {place}</span>"
            f"<span>🗓 {_esc(start_str)} → {_esc(end_str)}</span>"
            f"<span>📊 {n_days_v} day(s), {completeness_str} data captured</span>"
            f"<span>📄 Generated {datetime.now().strftime('%d %b %Y')}</span>"
            "</div>",
            "  </section>",

            ts_warn_html,

            # ── Headline: what we found ──
            "  <section class='card summary'>",
            "    <h2>What we found</h2>",
            f"    <span class='badge' style='background:{concern_bg};color:{concern_color}'>OVERALL: {concern_label}</span>",
            f"    {_summary_to_html(summary_text)}",
            "  </section>",

            # ── Key numbers ──
            _section("Your noise at a glance", "", f"<div class='keygrid'>{key_cards}</div>"),

            # ── How your noise compares ──
            _section(
                "How your noise compares",
                "The bar below places your average noise level next to everyday sounds and the World "
                "Health Organization (WHO) health guideline, so you can see where your location sits.",
                f"<div class='verdict' style='background:{verdict_bg};color:{verdict_clr}'>{verdict_txt}</div>"
                + _chart_html(fig_compare)
            ),

            # ── Day-by-day ──
            _section(
                "Day by day",
                "Each point is the average noise level for one day of monitoring. The dashed line is the "
                "WHO health guideline (53 dB) — days above it were louder than recommended.",
                _chart_html(fig_daily)
                + "<div class='chart-note'>A higher line means a louder day overall.</div>"
            ),

            # ── A typical day ──
            _section(
                "A typical day",
                "This shows the average noise for each hour of the day, combined across all monitored days. "
                "The shaded band is night-time (11 PM – 7 AM), when quiet matters most for sleep.",
                _chart_html(fig_typical)
                + "<div class='chart-note'>Use this to see when your location is usually loudest and quietest.</div>"
            ),

            # ── Health meaning ──
            _section("What this means for your health", "", health_html),

            # ── Actions ──
            _section("What you can do", "", action_html),

            custom_html,

            # ── About ──
            "  <section class='card'>",
            "    <h2>About this measurement</h2>",
            "    <div class='about'>",
            f"      <div><b>Location / device</b><span>{place}</span></div>",
            f"      <div><b>Monitoring period</b><span>{_esc(start_str)} to {_esc(end_str)} ({n_days_v} day(s))</span></div>",
            f"      <div><b>Data captured</b><span>{completeness_str} of the period</span></div>",
            f"      <div><b>Source file(s)</b><span>{source_label}</span></div>",
            f"      <div><b>Loudest single moment</b><span>{_hfmt(peak_v)} dB</span></div>",
            f"      <div><b>Quiet background level</b><span>{_hfmt(l90_v)} dB (the noise stays above this 90% of the time)</span></div>",
            "    </div>",
            "    <p class='disclaimer'>Noise is measured in A-weighted decibels (dB), matched to how human hearing works. "
            "Levels are energy-averaged (LAeq), the standard way to summarise changing noise. Guideline values come from "
            "the WHO Environmental Noise Guidelines (2018). This is a community summary for general understanding and "
            "research participation — it is not a clinical diagnosis or legal assessment. For a formal evaluation, consult "
            "a certified acoustic professional.</p>",
            "  </section>",

            # ── Footer ──
            "  <div class='footer'>",
            "    Environmental Noise Analysis Platform · Developed by Chandra Prakash Choudhary · "
            "PI: Dr. Ana María Rule, Johns Hopkins University",
            "  </div>",
            "  </div>", "</body>", "</html>",
        ]

        with open(report_path, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(p for p in html_parts if p))

        return report_path

    # ============================================================
    # PARTICIPANT-FRIENDLY CHART BUILDERS (HTML report)
    # ============================================================

    def _fig_compare_to_references(self, laeq):
        """Horizontal bar placing the measured average next to everyday sounds."""
        if laeq is None or not np.isfinite(laeq):
            return None
        refs = [
            ("Whisper / quiet bedroom", 30.0, '#cbd5e1'),
            ("Library / soft rain", 40.0, '#cbd5e1'),
            ("Normal conversation", 50.0, '#cbd5e1'),
            ("WHO health guideline", 53.0, '#f59e0b'),
            ("Your location", float(laeq), '#1e3a5f'),
            ("Busy street traffic", 70.0, '#cbd5e1'),
            ("Power tools (hearing risk)", 85.0, '#cbd5e1'),
        ]
        refs.sort(key=lambda r: r[1])
        labels = [r[0] for r in refs]
        values = [r[1] for r in refs]
        colors = [r[2] for r in refs]
        fig = go.Figure(go.Bar(
            x=values, y=labels, orientation='h',
            marker=dict(color=colors),
            text=[f"{v:.0f} dB" for v in values],
            textposition='outside',
            cliponaxis=False,
            hovertemplate='%{y}: %{x:.0f} dB(A)<extra></extra>',
        ))
        fig.update_layout(
            height=340, margin=dict(l=10, r=60, t=20, b=40),
            xaxis=dict(title='Noise level (dB)', range=[0, 95], gridcolor='rgba(0,0,0,0.06)', zeroline=False),
            yaxis=dict(automargin=True),
            plot_bgcolor='white', paper_bgcolor='white', showlegend=False,
        )
        return fig

    def _fig_daily_simple(self):
        """Simple day-by-day average noise trend vs the WHO guideline."""
        ds = self.daily_summary
        if ds is None or ds.empty or 'Average_L_EQ_dB' not in ds.columns:
            return None
        d = ds.dropna(subset=['Average_L_EQ_dB'])
        if d.empty:
            return None
        try:
            x = [pd.Timestamp(v).strftime('%d %b') for v in d['Date']]
        except Exception:
            x = [str(v) for v in d['Date']]
        y = [float(v) for v in d['Average_L_EQ_dB']]
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=x, y=y, mode='lines+markers', fill='tozeroy',
            line=dict(color='#1e3a5f', width=2.5), marker=dict(size=7, color='#1e3a5f'),
            fillcolor='rgba(30,58,95,0.08)', name='Daily average',
            hovertemplate='%{x}<br>%{y:.1f} dB(A)<extra></extra>',
        ))
        ymax = max(y + [53]) + 6
        ymin = min(y + [45]) - 4
        fig.add_hline(y=53, line=dict(color='#dc2626', width=1.5, dash='dash'),
                      annotation_text='WHO guideline 53 dB', annotation_position='top left',
                      annotation_font=dict(size=11, color='#b91c1c'))
        fig.update_layout(
            height=360, margin=dict(l=55, r=30, t=30, b=60),
            xaxis=dict(title='Date', automargin=True, gridcolor='rgba(0,0,0,0.06)'),
            yaxis=dict(title='Average noise (dB)', range=[ymin, ymax], gridcolor='rgba(0,0,0,0.06)', zeroline=False),
            plot_bgcolor='white', paper_bgcolor='white', showlegend=False,
        )
        return fig

    def _fig_typical_day(self):
        """24-hour average profile with the WHO night window shaded."""
        hs = self.hourly_summary
        if hs is None or hs.empty or 'Average_L_EQ_dB' not in hs.columns:
            return None
        d = hs.dropna(subset=['Average_L_EQ_dB']).sort_values('Hour')
        if d.empty:
            return None
        hours = [int(v) for v in d['Hour']]
        labels = [f"{hh:02d}:00" for hh in hours]
        y = [float(v) for v in d['Average_L_EQ_dB']]
        fig = go.Figure()
        fig.add_trace(go.Scatter(
            x=labels, y=y, mode='lines+markers', fill='tozeroy',
            line=dict(color='#1e3a5f', width=2.5), marker=dict(size=6, color='#1e3a5f'),
            fillcolor='rgba(30,58,95,0.07)', name='Hourly average',
            hovertemplate='%{x}<br>%{y:.1f} dB(A)<extra></extra>',
        ))
        ymax = max(y + [53]) + 6
        ymin = min(y + [40]) - 4
        # Shade the WHO night window (23:00–07:00) by category index.
        def _idx(hr):
            return hours.index(hr) if hr in hours else None
        shapes = []
        a, b = _idx(0), _idx(6)
        if a is not None and b is not None:
            shapes.append(dict(type='rect', xref='x', yref='paper', x0=a - 0.5, x1=b + 0.5,
                               y0=0, y1=1, fillcolor='rgba(30,58,95,0.07)', line=dict(width=0), layer='below'))
        c = _idx(23)
        if c is not None:
            shapes.append(dict(type='rect', xref='x', yref='paper', x0=c - 0.5, x1=c + 0.5,
                               y0=0, y1=1, fillcolor='rgba(30,58,95,0.07)', line=dict(width=0), layer='below'))
        fig.add_hline(y=53, line=dict(color='#dc2626', width=1.3, dash='dot'))
        fig.add_hline(y=45, line=dict(color='#d97706', width=1.3, dash='dot'))
        fig.update_layout(
            height=360, margin=dict(l=55, r=30, t=40, b=60), shapes=shapes,
            xaxis=dict(title='Hour of day', automargin=True, gridcolor='rgba(0,0,0,0.06)'),
            yaxis=dict(title='Average noise (dB)', range=[ymin, ymax], gridcolor='rgba(0,0,0,0.06)', zeroline=False),
            plot_bgcolor='white', paper_bgcolor='white', showlegend=False,
            annotations=[dict(xref='paper', yref='paper', x=0.01, y=0.98, showarrow=False,
                              text='Shaded = night (11 PM–7 AM)', font=dict(size=11, color='rgba(30,58,95,0.65)'),
                              xanchor='left', yanchor='top')],
        )
        return fig

    @staticmethod
    def _participant_guidance(lden, lnight, laeq):
        """Return (health_points, action_points) as plain-language bullet strings."""
        def _ok(v):
            return v is not None and np.isfinite(v)

        health, actions = [], []

        # Night-time / sleep
        if _ok(lnight) and lnight > 45:
            health.append(
                f"At night the noise averaged about {lnight:.0f} dB, above the WHO sleep-protection level "
                "of 45 dB. Night noise can disturb sleep even when you don't fully wake up, and over years "
                "is linked to higher blood pressure and heart strain.")
            actions.append("Keep bedroom windows closed at night and, if you can, sleep in a room facing away from the noise.")
            actions.append("Consider soft earplugs or a steady background sound (a fan) to mask sudden noises while sleeping.")
        elif _ok(lnight) and lnight > 40:
            health.append(
                f"Night-time noise averaged about {lnight:.0f} dB. This is within the WHO limit but above the "
                "level where the most sensitive sleepers can begin to notice effects.")
            actions.append("If your sleep feels disturbed, keeping bedroom windows closed at night can help.")
        elif _ok(lnight):
            health.append(
                f"Night-time noise averaged about {lnight:.0f} dB, within WHO sleep-protection guidance — "
                "disturbed sleep from outdoor noise is unlikely.")

        # Daytime / overall
        if _ok(lden) and lden > 53:
            health.append(
                f"Your overall day-and-night level of about {lden:.0f} dB is above the WHO health guideline "
                "of 53 dB. Long-term exposure at higher levels is associated with annoyance and increased "
                "cardiovascular risk.")
            actions.append("Spend relaxing or outdoor time during the quieter hours shown in the 'typical day' chart.")
        elif _ok(lden):
            health.append(
                f"Your overall day-and-night level of about {lden:.0f} dB is within the WHO health guideline of 53 dB.")
        elif _ok(laeq):
            health.append(
                f"The average noise level was about {laeq:.0f} dB. Day/night-weighted guideline figures could not "
                "be computed for this dataset.")

        # Universal
        actions.append(
            "Sensitive people — children, older adults, pregnant women, and anyone with heart or breathing "
            "conditions — feel noise effects sooner, so take extra care for them.")
        if not health:
            health.append("Noise levels were within general health guidance for the monitored period.")
        return health, actions

    # ============================================================
    # OPTIONAL CUSTOM SECTION (user notes, inserted after Section 4)
    # ============================================================

    def _add_custom_section(self, story, styles):
        heading = self.custom_section_heading or "Additional Notes"
        story.append(Paragraph(f"Additional Notes: {escape(heading)}", styles['h1']))
        if self.custom_section_body:
            for line in self.custom_section_body.splitlines():
                line = line.strip()
                if line:
                    story.append(Paragraph(escape(line), styles['BodyText']))
                    story.append(Spacer(1, 0.06 * inch))

    # ============================================================
    # SECTION 9: METHODOLOGICAL LIMITATIONS & DISCLAIMER
    # ============================================================

    def _add_section_9_disclaimer(self, story, styles):
        """Add Section 9: Methodological Limitations & Disclaimer"""
        story.append(Paragraph("Section 9: Methodological Limitations &amp; Disclaimer", styles['h1']))
        story.append(Spacer(1, 0.15 * inch))

        # Source Attribution
        story.append(Paragraph("<b>Source Attribution</b>", styles['h2']))
        story.append(Paragraph(
            "Acoustic sensors measure total environmental energy; they do not definitively identify specific noise sources "
            "(e.g., distinguishing a commercial aircraft from a heavy goods vehicle). Source-specific compliance requires "
            "cross-referencing with external databases (e.g., ADS-B flight tracking).",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.12 * inch))

        # Health vs. Legal Limits
        story.append(Paragraph("<b>Health vs. Legal Limits</b>", styles['h2']))
        story.append(Paragraph(
            "This report evaluates data against both biological health guidelines (WHO 2018) and local regulatory limits "
            "(e.g., Maryland COMAR). Passing local legal zoning limits does not inherently guarantee the absence of adverse "
            "physiological health impacts.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.12 * inch))

        # Equipment Calibration
        story.append(Paragraph("<b>Equipment Calibration</b>", styles['h2']))
        story.append(Paragraph(
            "The accuracy of these metrics is strictly dependent on the proper deployment, windshielding, and recent acoustic "
            "calibration of the monitoring hardware.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.12 * inch))

        # Not Medical Advice
        story.append(Paragraph("<b>Not Medical Advice</b>", styles['h2']))
        story.append(Paragraph(
            "This data is for environmental and epidemiological research purposes. It does not constitute a clinical medical "
            "diagnosis or formal legal counsel.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.12 * inch))

    # ============================================================
    # FORMATTING HELPERS (STRICT - NO TEXT/FLOAT FUSION)
    # ============================================================

    @staticmethod
    def _fmt_float(value, decimals: int = 2) -> str:
        """Format float to string with strict rounding. NO FUSION."""
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
        """Format dB value with unit. NO FUSION."""
        s = cls._fmt_float(value, 2)
        return "N/A" if s == "N/A" else f"{s} dB(A)"

    @classmethod
    def _fmt_db_plain(cls, value) -> str:
        """Format dB value for table cells (unit in header). NO FUSION."""
        return cls._fmt_float(value, 2)

    @staticmethod
    def _status_pass_fail(limit_db: float, measured_db) -> str:
        """Return PASS/FAIL status. NO FUSION."""
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
        """Return exceedance margin. NO FUSION."""
        try:
            if measured_db is None:
                return None
            v = float(measured_db)
            if not np.isfinite(v):
                return None
            return max(0.0, v - float(limit_db))
        except Exception:
            return None

    # ============================================================
    # ACOUSTIC COLUMN DETECTION
    # ============================================================

    def _resolve_acoustic_columns(self):
        """Map dataset columns into LEQ / L-Max / L-Min streams."""
        def _norm(name: str) -> str:
            return "".join(ch for ch in (name or "").lower() if ch.isalnum())

        ts_candidates = [
            c for c in self.df.columns
            if any(t in c.lower() for t in ["timestamp", "datetime", "date", "time"])
        ]
        ts_col = ts_candidates[0] if ts_candidates else None

        noise_cols = list(self.analyzer.noise_columns)

        lmax_col = None
        lmin_col = None
        for c in noise_cols:
            n = _norm(c)
            if lmax_col is None and (n.startswith("lmax") or "lmax" in n or n.endswith("maxdba")):
                lmax_col = c
            if lmin_col is None and (n.startswith("lmin") or "lmin" in n or n.endswith("mindba")):
                lmin_col = c

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
            non_peak = [c for c in noise_cols if c != lmax_col and c != lmin_col]
            leq_col = non_peak[0] if non_peak else (noise_cols[0] if noise_cols else None)

        return ts_col, leq_col, lmax_col, lmin_col

    def _get_timestamp_series(self, ts_col: str | None) -> pd.Series:
        """Return a usable timestamp series, recovering messy formats where possible.

        Sets ``self.timestamps_synthetic = True`` when no real timestamps could be
        read and a synthetic index was substituted, so report sections can warn
        instead of presenting fabricated dates as real.
        """
        from analysis.timestamp_utils import assess_timestamp_integrity
        if ts_col and ts_col in self.df.columns:
            verdict = assess_timestamp_integrity(self.df[ts_col])
            if verdict.time_metrics_valid and verdict.parsed is not None and verdict.parsed.notna().sum() > 0:
                self.timestamps_synthetic = False
                self.timestamp_integrity = verdict
                return verdict.parsed
            self.timestamp_integrity = verdict
        self.timestamps_synthetic = True
        return pd.Series(pd.date_range(start=datetime.now(), periods=len(self.df), freq="s"))

    def _get_numeric_series(self, col: str | None) -> pd.Series:
        if not col or col not in self.df.columns:
            return pd.Series(dtype=float)
        return pd.to_numeric(self.df[col], errors="coerce")

    # ============================================================
    # WHO COMPLIANCE CHECKER
    # ============================================================

    def _check_who_compliance_failures(self, ts: pd.Series, leq: pd.Series) -> bool:
        """Check if ANY WHO 2018 guideline is exceeded."""
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

    # ============================================================
    # SECTION 1: EXECUTIVE ACOUSTIC SUMMARY
    # ============================================================

    def _add_section_1_executive_summary(self, story, styles, *, ts: pd.Series, leq_col: str | None):
        story.append(Paragraph("Section 1: Executive Acoustic Summary", styles['h1']))

        # Device / Location identity
        if self.device_id:
            story.append(Paragraph(f"<b>Device / Location ID:</b> {escape(self.device_id)}", styles['BodyText']))

        # Dataset provenance — single or merged
        if self.source_files:
            story.append(Paragraph(
                f"<b>Dataset Composition:</b> Merged batch of {len(self.source_files)} file(s): "
                + ", ".join(escape(f) for f in self.source_files),
                styles['BodyText']
            ))
        else:
            story.append(Paragraph(
                f"<b>Source File:</b> {escape(os.path.basename(self.filepath))}",
                styles['BodyText']
            ))

        ts_valid = ts.dropna()
        if ts_valid.empty:
            date_range = "Unknown"
            duration = "Unknown"
            total_days = 0
        else:
            start = ts_valid.min()
            end = ts_valid.max()
            date_range = f"{start.strftime('%Y-%m-%d %H:%M:%S')} to {end.strftime('%Y-%m-%d %H:%M:%S')}"
            seconds = max(0.0, (end - start).total_seconds())
            hours = seconds / 3600.0
            total_days = seconds / (24 * 3600)
            duration = f"{total_days:.2f} days ({hours:.2f} hours)"

        story.append(Paragraph(f"<b>Measurement Date Range:</b> {escape(date_range)}", styles['BodyText']))
        story.append(Paragraph(f"<b>Total Duration:</b> {escape(duration)}", styles['BodyText']))
        story.append(Spacer(1, 0.12 * inch))

        leq = self._get_numeric_series(leq_col)
        if leq.dropna().empty:
            story.append(Paragraph("LEQ stream not available.", styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))
            return

        laeq = energetic_mean_db(leq)
        laeq_str = self._fmt_db(laeq)
        story.append(Paragraph(f"<b>24-Hour Energy Average (LAeq):</b> {escape(laeq_str)}", styles['BodyText']))
        story.append(Spacer(1, 0.1 * inch))

        # WHO compliance check
        who_fails = self._check_who_compliance_failures(ts, leq)

        try:
            laeq_v = float(laeq) if laeq is not None else float('nan')
        except Exception:
            laeq_v = float('nan')

        if np.isfinite(laeq_v):
            laeq_str = escape(self._fmt_float(laeq_v))
            if who_fails:
                interp = (
                    f"The environment exhibits an average continuous noise level of {laeq_str} dB(A), "
                    "<b>characterized by acoustic loading that exceeds WHO health guidelines.</b>"
                )
            else:
                if laeq_v < 55:
                    loading = "low"
                elif laeq_v < 65:
                    loading = "moderate"
                elif laeq_v < 75:
                    loading = "high"
                else:
                    loading = "very high"
                interp = (
                    f"The environment exhibits an average continuous noise level of {laeq_str} dB(A), "
                    f"characterized by {loading} acoustic loading."
                )
        else:
            interp = "The environment exhibits an average continuous noise level that could not be computed due to missing/invalid LEQ values."

        story.append(Paragraph(interp, styles['BodyText']))
        story.append(Spacer(1, 0.12 * inch))

        # Plain-English summary
        env = compute_ldn_lden(ts, leq) or {}
        lden_v   = env.get('Lden')
        lnight_v = env.get('Lnight')
        h = ts.dt.hour
        is_day   = (h >= 7) & (h < 22)
        is_night = ~is_day
        laeq_day_v   = energetic_mean_db(leq[is_day])   if is_day.any()   else None
        laeq_night_v = energetic_mean_db(leq[is_night]) if is_night.any() else None

        exc = exceedance_levels_db(leq.dropna().to_numpy()) or {}

        ts_valid = ts.dropna()
        start_str = ts_valid.min().strftime('%d %b %Y') if not ts_valid.empty else ''
        end_str   = ts_valid.max().strftime('%d %b %Y') if not ts_valid.empty else ''
        n_days_v  = int(round((ts_valid.max() - ts_valid.min()).total_seconds() / 86400)) if not ts_valid.empty else 0

        completeness = data_completeness_pct(ts_valid, actual_count=len(self.df))

        summary_text = ReportGeneratorV2.generate_plain_english_summary(
            laeq=laeq_v,
            lden=lden_v,
            lnight=lnight_v,
            laeq_day=laeq_day_v,
            laeq_night=laeq_night_v,
            laeq_min=float(leq.min()) if not leq.dropna().empty else None,
            laeq_max=float(leq.max()) if not leq.dropna().empty else None,
            l10=exc.get('L10'),
            l90=exc.get('L90'),
            start_date=start_str,
            end_date=end_str,
            duration_label=self._compute_duration_label(ts),
            data_completeness_pct=completeness,
            n_days=n_days_v,
        )

        # Determine box colour by concern level
        _sl = summary_text.lower()
        if 'concern level: high' in _sl or 'concern level: serious' in _sl:
            _box_bg, _box_border = '#FEF2F2', '#991b1b'
            _level_label, _level_fg = 'HIGH', '#991b1b'
        elif 'concern level: moderate' in _sl:
            _box_bg, _box_border = '#FFFBEB', '#92400e'
            _level_label, _level_fg = 'MODERATE', '#92400e'
        else:
            _box_bg, _box_border = '#EFF6FF', '#3D5A80'
            _level_label, _level_fg = 'LOW', '#166534'

        # Inner style — NO border, no background (the Table provides the single outer box)
        _inner_style = ParagraphStyle(
            'SummaryInner',
            parent=styles['BodyText'],
            fontSize=9,
            leading=14,
            spaceAfter=6,
        )

        story.append(Paragraph(
            "<b>Non-Technical Summary: Noise Exposure &amp; Health Assessment</b>", styles['h2']
        ))

        # Parse summary text into sections
        body_parts, who_part, concern_part = [], None, None
        for para_block in summary_text.split('\n\n'):
            lines = para_block.split('\n')
            header_lines = [l for l in lines if not l.strip().startswith('•')]
            bullet_lines = [l.strip().lstrip('•').strip() for l in lines if l.strip().startswith('•')]
            header_joined = ' '.join(header_lines).strip()
            first = header_joined.strip()
            if first.startswith('WHO '):
                who_part = (header_joined, bullet_lines)
            elif first.startswith('Overall Concern'):
                concern_part = header_joined
            else:
                if header_joined or bullet_lines:
                    body_parts.append((header_joined, bullet_lines))

        # Build content as separate inner Paragraphs inside one Table cell.
        # This is the only reliable way to get ONE outer border in ReportLab.
        _plain_style = ParagraphStyle(
            'SummaryPlain', parent=styles['BodyText'],
            fontSize=9, leading=13, spaceAfter=4,
        )
        _bold_style = ParagraphStyle(
            'SummaryBold', parent=_plain_style,
            fontName='Helvetica-Bold', spaceAfter=2,
        )

        inner_content = []

        # 1. Concern level — prominent coloured line
        if concern_part:
            explanation = concern_part
            for pfx in (f'Overall Concern Level: {_level_label}. ',
                        f'Overall Concern Level: {_level_label.capitalize()}. ',
                        'Overall Concern Level: '):
                if explanation.startswith(pfx):
                    explanation = explanation[len(pfx):]
                    break
            inner_content.append(Paragraph(
                f'<font color="{_level_fg}"><b>CONCERN LEVEL: {_level_label}</b></font>'
                f'  {escape(explanation)}',
                _plain_style,
            ))
            inner_content.append(Spacer(1, 4))

        # 2. Body paragraphs (dataset overview, noise level, variability)
        for (hdr, bullets) in body_parts:
            if hdr:
                inner_content.append(Paragraph(escape(hdr), _plain_style))
            for bl in bullets:
                inner_content.append(Paragraph(f'•  {escape(bl)}', _plain_style))

        # 3. WHO compliance section
        if who_part:
            inner_content.append(Spacer(1, 4))
            hdr, bullets = who_part
            inner_content.append(Paragraph(escape(hdr), _bold_style))
            for bl in bullets:
                inner_content.append(Paragraph(f'•  {escape(bl)}', _plain_style))

        # Wrap all inner content in a KeepTogether inside a single-cell Table
        # → one background, one border, no per-paragraph boxes
        summary_table = Table(
            [[ inner_content ]],
            colWidths=[9.0 * inch],
        )
        summary_table.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), colors.HexColor(_box_bg)),
            ('BOX',           (0, 0), (-1, -1), 1.2, colors.HexColor(_box_border)),
            ('TOPPADDING',    (0, 0), (-1, -1), 12),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
            ('LEFTPADDING',   (0, 0), (-1, -1), 14),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 14),
            ('VALIGN',        (0, 0), (-1, -1), 'TOP'),
        ]))
        story.append(summary_table)
        story.append(Spacer(1, 0.12 * inch))

        # ── Top 10 Peak Noise Events ──────────────────────────────────────────
        self._add_top_noise_events(story, styles, ts=ts, leq_col=leq_col)

    # ============================================================
    # SECTION 2: DATA QUALITY & COMPLETENESS
    # ============================================================

    # ── Top 10 Peak Noise Events ──────────────────────────────────────────────

    @staticmethod
    def _compute_top_noise_events(ts: pd.Series, leq: pd.Series, top_n: int = 10,
                                  min_separation_minutes: float = 5.0) -> list[dict]:
        """Detect discrete loud noise events and return the top N by peak level.

        An event is a **contiguous** run of readings at or above the 90th-percentile
        (L10) threshold: a reading below the threshold ends it, and only brief
        missing-sample time gaps are bridged — quiet periods are never merged into
        an event. For each event we record the exact moment its PEAK level occurred
        (``peak_time``, so it can be located in the raw data), the peak level, the
        event start/end, and the contiguous duration above the threshold.

        Events whose peak falls within ``min_separation_minutes`` of an
        already-selected louder event are skipped so the list contains distinct
        events. Returned sorted by peak level descending.
        """
        ts  = ts.dropna()
        leq = pd.to_numeric(leq, errors='coerce').reindex(ts.index)
        df_tmp = pd.DataFrame({'ts': ts.values, 'leq': leq.values}).dropna()
        df_tmp = df_tmp.sort_values('ts').reset_index(drop=True)
        if len(df_tmp) < 2:
            return []

        threshold = float(np.percentile(df_tmp['leq'], 90))
        times  = df_tmp['ts'].to_numpy()
        levels = df_tmp['leq'].to_numpy()
        above  = levels >= threshold
        if not above.any():
            return []

        # Bridge only short missing-sample gaps, never quiet (below-threshold)
        # periods — otherwise an all-day stretch of intermittent noise collapses
        # into one multi-hour "event" whose start bears no relation to the peak.
        diffs = np.diff(times).astype('timedelta64[s]').astype(float)
        interval_s = float(np.median(diffs)) if len(diffs) else 1.0
        bridge_s = max(interval_s * 3.0, 3.0)

        events: list[dict] = []
        n = len(df_tmp)
        i = 0
        while i < n:
            if not above[i]:
                i += 1
                continue
            j = i
            while (j + 1 < n and above[j + 1]
                   and (times[j + 1] - times[j]) / np.timedelta64(1, 's') <= bridge_s):
                j += 1
            seg_lv = levels[i:j + 1]
            seg_ts = times[i:j + 1]
            k = int(np.argmax(seg_lv))
            events.append({
                'start':      seg_ts[0],
                'end':        seg_ts[-1],
                'peak_time':  seg_ts[k],
                'peak':       float(seg_lv[k]),
                'duration_s': float((seg_ts[-1] - seg_ts[0]) / np.timedelta64(1, 's')),
            })
            i = j + 1

        events.sort(key=lambda e: e['peak'], reverse=True)

        # Keep only distinct events — peaks at least min_separation apart.
        sep = np.timedelta64(int(min_separation_minutes * 60), 's')
        selected: list[dict] = []
        for e in events:
            if all(abs(e['peak_time'] - s['peak_time']) > sep for s in selected):
                selected.append(e)
            if len(selected) >= top_n:
                break
        return selected

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        seconds = int(round(seconds))
        if seconds < 60:
            return f"{seconds}s" if seconds > 0 else "<1s"
        m, s = divmod(seconds, 60)
        h, m = divmod(m, 60)
        if h > 0:
            return f"{h}h {m:02d}m"
        return f"{m}m {s:02d}s"

    def _add_top_noise_events(self, story, styles, *, ts: pd.Series, leq_col: str | None):
        from reportlab.platypus import Table, TableStyle
        from reportlab.lib import colors as rl_colors

        leq = self._get_numeric_series(leq_col)
        if ts.dropna().empty or leq.dropna().empty:
            return

        events = self._compute_top_noise_events(ts, leq)
        if not events:
            return

        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph("<b>Top Peak Noise Events</b>", styles['h2']))
        story.append(Paragraph(
            "The table below lists the loudest discrete noise events detected during the "
            "monitoring period. Each event is a contiguous period at or above the 90th-percentile "
            "(L10) threshold; <b>Peak Time</b> is the exact moment the peak level occurred (so it can be "
            "located directly in the raw data), and <b>Duration</b> is how long levels stayed "
            "continuously above the threshold. Listed events are at least 5 minutes apart.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.06 * inch))

        header = ['#', 'Date', 'Day', 'Peak Time', 'Peak Level', 'Duration']
        rows   = [header]
        for i, ev in enumerate(events, 1):
            dt_peak   = pd.Timestamp(ev['peak_time'])
            dur_s     = ev['duration_s']
            dur_str   = self._fmt_duration(dur_s) if dur_s >= 60 else 'Brief (<1 min)'
            rows.append([
                str(i),
                dt_peak.strftime('%d %b %Y'),
                dt_peak.strftime('%A'),
                dt_peak.strftime('%H:%M:%S'),
                f"{ev['peak']:.1f} dB(A)",
                dur_str,
            ])

        col_widths = [0.3*inch, 1.0*inch, 0.9*inch, 0.9*inch, 1.0*inch, 0.9*inch]
        tbl = Table(rows, colWidths=col_widths)
        tbl.setStyle(TableStyle([
            ('BACKGROUND',  (0, 0), (-1, 0), rl_colors.HexColor('#1e3a5f')),
            ('TEXTCOLOR',   (0, 0), (-1, 0), rl_colors.white),
            ('FONTNAME',    (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE',    (0, 0), (-1, -1), 8),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [rl_colors.white, rl_colors.HexColor('#F0F4F8')]),
            ('GRID',        (0, 0), (-1, -1), 0.4, rl_colors.HexColor('#CBD5E1')),
            ('ALIGN',       (0, 0), (0, -1), 'CENTER'),
            ('ALIGN',       (4, 0), (5, -1), 'CENTER'),
            ('TOPPADDING',  (0, 0), (-1, -1), 4),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
        ]))
        story.append(tbl)
        story.append(Spacer(1, 0.15 * inch))

    # ── Top noise events for HTML report ──────────────────────────────────────

    def _top_noise_events_html(self, ts: pd.Series, leq_col: str | None) -> str:
        """Return an HTML table of top noise events for the HTML report, or '' if unavailable."""
        leq = self._get_numeric_series(leq_col)
        if ts.dropna().empty or leq.dropna().empty:
            return ''
        events = self._compute_top_noise_events(ts, leq)
        if not events:
            return ''

        rows_html = ''
        for i, ev in enumerate(events, 1):
            dt  = pd.Timestamp(ev['peak_time'])
            dur = self._fmt_duration(ev['duration_s']) if ev['duration_s'] >= 60 else 'Brief (&lt;1 min)'
            bg  = '#fff' if i % 2 == 0 else '#f8fafc'
            rows_html += (
                f"<tr style='background:{bg}'>"
                f"<td style='text-align:center'>{i}</td>"
                f"<td>{dt.strftime('%d %b %Y')}</td>"
                f"<td>{dt.strftime('%A')}</td>"
                f"<td>{dt.strftime('%H:%M:%S')}</td>"
                f"<td style='text-align:center;font-weight:600'>{ev['peak']:.1f} dB(A)</td>"
                f"<td style='text-align:center'>{dur}</td>"
                f"</tr>"
            )

        return (
            "<div class='card'>"
            "<h2>Top Peak Noise Events</h2>"
            "<p style='font-size:12px;color:#4b5563;margin-bottom:8px'>"
            "Loudest discrete events detected during the monitoring period. Each event is a "
            "contiguous period at or above the 90th percentile (L10); <b>Peak Time</b> is the exact "
            "moment the peak level occurred (locatable in the raw data) and <b>Duration</b> is how long "
            "levels stayed continuously above the threshold. Listed events are at least 5 minutes apart."
            "</p>"
            "<table style='width:100%;border-collapse:collapse;font-size:13px'>"
            "<thead><tr style='background:#1e3a5f;color:#fff'>"
            "<th style='padding:7px 6px'>#</th>"
            "<th style='padding:7px 6px;text-align:left'>Date</th>"
            "<th style='padding:7px 6px;text-align:left'>Day</th>"
            "<th style='padding:7px 6px;text-align:left'>Peak Time</th>"
            "<th style='padding:7px 6px'>Peak Level</th>"
            "<th style='padding:7px 6px'>Duration</th>"
            "</tr></thead>"
            f"<tbody>{rows_html}</tbody>"
            "</table></div>"
        )

    def _add_section_2_data_quality(self, story, styles, *, ts: pd.Series):
        story.append(Paragraph("Section 2: Data Quality & Completeness (QA/QC)", styles['h1']))

        ts_valid = ts.dropna()
        if ts_valid.empty:
            story.append(Paragraph("No valid timestamps found.", styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))
            return

        start = ts_valid.min()
        end = ts_valid.max()
        expected_seconds = max(0.0, (end - start).total_seconds())
        # Detect the actual logging interval instead of assuming 1 Hz, so a
        # logger sampling every 2 s / every minute is not falsely flagged as
        # having lost data.
        interval_s = _modal_interval_seconds(ts_valid)
        interval_s = interval_s if interval_s and interval_s > 0 else 1.0
        expected_samples = int(expected_seconds / interval_s) + 1
        actual_samples = len(self.df)
        uptime_pct = min(100.0, (100.0 * actual_samples / max(1, expected_samples)))
        interval_label = (f"{interval_s:.0f} s" if interval_s >= 1 else f"{interval_s:.3f} s")

        story.append(Paragraph(f"<b>Measurement Span:</b> {escape(start.strftime('%Y-%m-%d %H:%M:%S'))} to {escape(end.strftime('%Y-%m-%d %H:%M:%S'))}", styles['BodyText']))
        story.append(Paragraph(f"<b>Detected Logging Interval:</b> {escape(interval_label)}", styles['BodyText']))
        story.append(Paragraph(f"<b>Expected Samples:</b> {expected_samples:,}", styles['BodyText']))
        story.append(Paragraph(f"<b>Actual Samples in Dataset:</b> {actual_samples:,}", styles['BodyText']))
        story.append(Paragraph(f"<b>Uptime (Data Completeness):</b> {self._fmt_float(uptime_pct, 1)}%", styles['BodyText']))
        story.append(Spacer(1, 0.1 * inch))

        if uptime_pct < 90.0:
            story.append(Paragraph(
                f"<b style='color:red'>⚠ WARNING: High data loss detected ({100 - uptime_pct:.1f}% missing). Global averages may be skewed.</b>",
                styles['BodyText']
            ))
        else:
            story.append(Paragraph("✓ Data completeness is acceptable (≥90%).", styles['BodyText']))

        story.append(Spacer(1, 0.12 * inch))

        # ── Data Continuity Log ────────────────────────────────────────────────
        story.append(Paragraph("Data Continuity Log", styles['h2']))

        # If a pre-computed gap report from the merge step is available, use it directly.
        # Otherwise, run gap detection fresh on the current dataframe.
        if self.merge_gap_report:
            gd = self.merge_gap_report
            has_gaps = gd.get('gap_count', 0) > 0

            if self.source_files:
                merge_note = (
                    f"This dataset is a <b>merged batch</b> of {len(self.source_files)} file(s). "
                    f"Gap analysis was performed across all file boundaries to detect any data loss "
                    f"introduced at merge points or within individual files."
                )
                story.append(Paragraph(merge_note, styles['BodyText']))
                story.append(Spacer(1, 0.08 * inch))

            if not has_gaps:
                story.append(Paragraph(
                    "✓ Continuous temporal alignment verified across all merged files. No gaps detected.",
                    styles['BodyText']
                ))
            else:
                missing_min = self._fmt_float(gd.get('missing_seconds', 0) / 60.0, 1)
                summary_line = (
                    f"⚠ DATA LOSS DETECTED — {gd['gap_count']} gap(s) across merged dataset. "
                    f"Minor (device restart/calibration): {gd.get('minor_gap_count', 0)}. "
                    f"Major (power failure/system crash): {gd.get('major_gap_count', 0)}. "
                    f"Total missing data: {missing_min} minutes. "
                    f"Uptime: {gd.get('uptime_pct', 100)}%."
                )
                story.append(Paragraph(escape(summary_line), styles['BodyText']))
                story.append(Spacer(1, 0.06 * inch))
                for g in (gd.get('gaps') or []):
                    badge = g.get('category', '').upper()
                    label = g.get('label', '')
                    story.append(Paragraph(f"• [{badge}] {escape(label)}", styles['BodyText']))
        else:
            ts_col_dcl, _, _, _ = self._resolve_acoustic_columns()
            try:
                gap_report = detect_gaps(self.df, ts_col_dcl) if ts_col_dcl else None
            except Exception as _e:
                gap_report = None
                print(f"[Report] Gap detection failed: {_e}")

            if gap_report is None or gap_report.continuous:
                story.append(Paragraph(
                    "✓ Continuous temporal alignment verified. No gaps detected.",
                    styles['BodyText']
                ))
            else:
                gd = gap_report_to_dict(gap_report)
                summary_line = (
                    f"⚠ DATA LOSS DETECTED — Uptime: {gd['uptime_pct']}% — "
                    f"{gd['gap_count']} disruption(s) ({gd['minor_gap_count']} Minor, {gd['major_gap_count']} Major). "
                    f"Missing data: {self._fmt_float(gd['missing_seconds'] / 60.0, 1)} minutes total."
                )
                story.append(Paragraph(escape(summary_line), styles['BodyText']))
                story.append(Spacer(1, 0.06 * inch))
                for g in gd['gaps']:
                    badge = g['category'].upper()
                    label = g['label']
                    story.append(Paragraph(f"• [{badge}] {escape(label)}", styles['BodyText']))

        story.append(Spacer(1, 0.12 * inch))

    # ============================================================
    # SECTION 3: GLOBAL REGULATORY & HEALTH COMPLIANCE
    # ============================================================

    def _add_section_3_compliance(self, story, styles, *, ts: pd.Series, leq_col: str | None):
        story.append(Paragraph("Section 3: Regulatory &amp; Health Compliance", styles['h1']))
        story.append(Paragraph(
            "Standards sourced from WHO Environmental Noise Guidelines (2018), "
            "WHO Guidelines for Community Noise (1999), and Maryland COMAR 26.02.03.02. "
            "OSHA/NIOSH occupational standards are excluded — this is an environmental assessment.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.10 * inch))

        leq = self._get_numeric_series(leq_col)
        if leq.dropna().empty:
            story.append(Paragraph("LEQ stream not available.", styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))
            return

        env = compute_ldn_lden(ts, leq) or {}
        measured_lden   = env.get('Lden')
        measured_lnight = env.get('Lnight')
        laeq_overall    = energetic_mean_db(leq)

        h         = ts.dt.hour
        is_day    = (h >= 7) & (h < 22)
        is_night  = ~is_day
        # Maryland nighttime: 22:00–07:00; daytime: 07:00–22:00
        laeq_day   = energetic_mean_db(leq[is_day])   if is_day.any()   else None
        laeq_night = energetic_mean_db(leq[is_night]) if is_night.any() else None

        # LAmax (peak level, for WHO bedroom single-event check)
        _, _, lmax_col, _ = self._resolve_acoustic_columns()
        lmax_series = self._get_numeric_series(lmax_col)
        lamax = float(lmax_series.max()) if not lmax_series.dropna().empty else None

        try:
            results = evaluate_compliance(
                lden=measured_lden,
                lnight=measured_lnight,
                laeq=laeq_overall,
                laeq_day=laeq_day,
                laeq_night=laeq_night,
                lamax=lamax,
                environment=self.environment,
            )
        except Exception as _e:
            print(f"[Report] Compliance evaluation failed: {_e}")
            results = []

        if not results:
            story.append(Paragraph(
                "Compliance matrix could not be computed — timestamps or LEQ values may be missing.",
                styles['BodyText']
            ))
            story.append(Spacer(1, 0.12 * inch))
            return

        # Build table: header + one row per applicable standard
        # All cells use Paragraph so they can wrap within their fixed column width.
        cell_8 = ParagraphStyle('Comp8', parent=styles['BodyText'], fontSize=8, leading=10, alignment=TA_LEFT)
        hdr_style = ParagraphStyle('CompHdr', parent=styles['BodyText'], fontSize=8, leading=10,
                                   textColor=colors.whitesmoke, fontName='Helvetica-Bold', alignment=TA_CENTER)
        hdr_style_left = ParagraphStyle('CompHdrL', parent=hdr_style, alignment=TA_LEFT)
        num_style = ParagraphStyle('CompNum', parent=cell_8, alignment=TA_RIGHT)
        ctr_style = ParagraphStyle('CompCtr', parent=cell_8, alignment=TA_CENTER)

        assess_pass = ParagraphStyle('AssessPass', parent=cell_8,
                                     textColor=colors.HexColor('#15803d'), fontName='Helvetica-Bold')
        assess_fail = ParagraphStyle('AssessFail', parent=cell_8,
                                     textColor=colors.HexColor('#b91c1c'), fontName='Helvetica-Bold')
        assess_ref  = ParagraphStyle('AssessRef', parent=cell_8,
                                     textColor=colors.HexColor('#6b7280'), fontName='Helvetica-Oblique')

        header_row = [
            Paragraph("Regulatory Standard",    hdr_style_left),
            Paragraph("Metric",                 hdr_style_left),
            Paragraph("Measured\n(dB(A))",      hdr_style),
            Paragraph("Limit\n(dB(A))",         hdr_style),
            Paragraph("Assessment",             hdr_style_left),
        ]

        rows = []
        for r in results:
            delta = r['delta_db']
            if r.get('kind') == 'indicative':
                # Source-specific reference (aircraft/railway) — no compliance verdict.
                margin = abs(delta)
                rel = "above" if r['status'] == 'ABOVE' else "below"
                assess_txt = f"{self._fmt_float(margin, 1)} dB {rel} source reference — indicative only"
                assess_style = assess_ref
            elif r['status'] == 'PASS':
                margin = abs(delta)
                assess_txt = f"Within limit by {self._fmt_float(margin, 1)} dB — compliant"
                assess_style = assess_pass
            else:
                excess = abs(delta)
                assess_txt = f"Exceeds limit by {self._fmt_float(excess, 1)} dB — non-compliant"
                assess_style = assess_fail
            rows.append([
                Paragraph(escape(str(r['standard'])), cell_8),
                Paragraph(escape(str(r['metric'])),   cell_8),
                Paragraph(escape(self._fmt_db_plain(r['measured_db'])), num_style),
                Paragraph(escape(self._fmt_db_plain(r['limit_db'])),    num_style),
                Paragraph(assess_txt, assess_style),
            ])

        data = [header_row] + rows
        col_widths = [2.8*inch, 1.2*inch, 1.0*inch, 0.9*inch, 2.3*inch]
        table = Table(data, colWidths=col_widths, repeatRows=1)

        ts_cmds = [
            ('BACKGROUND',    (0, 0), (-1, 0), colors.HexColor('#1e3a5f')),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('TOPPADDING',    (0, 0), (-1, 0), 8),
            ('LEFTPADDING',   (0, 0), (-1, -1), 5),
            ('RIGHTPADDING',  (0, 0), (-1, -1), 5),
            ('ROWBACKGROUNDS',(0, 1), (-1, -1), [colors.HexColor('#F8FAFC'), colors.white]),
            ('GRID',          (0, 0), (-1, -1), 0.5, colors.HexColor('#D1D5DB')),
            ('VALIGN',        (0, 0), (-1, -1), 'MIDDLE'),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 6),
            ('TOPPADDING',    (0, 1), (-1, -1), 6),
        ]
        table.setStyle(TableStyle(ts_cmds))
        story.append(table)

        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph(
            "<i>A PASS under Maryland COMAR does not imply absence of health risk — "
            "WHO 2018 health-based thresholds are stricter than most local zoning limits.</i>",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.10 * inch))

        # WHO source-attribution limitation note
        who_note_style = ParagraphStyle(
            'WhoNote',
            parent=styles['BodyText'],
            fontSize=8,
            leading=11,
            backColor=colors.HexColor('#FFFBEB'),
            borderPadding=(7, 9, 7, 9),
            borderColor=colors.HexColor('#D97706'),
            borderWidth=1,
        )
        story.append(Paragraph(
            "<b>How to read these rows:</b> "
            "The road-traffic (Lden ≤ 53 dB(A), Lnight ≤ 45 dB(A)) and Maryland COMAR rows are evaluated as "
            "<b>compliance</b> checks against the total measured environmental level. "
            "The aircraft (Lden ≤ 45 dB(A)) and railway (Lden ≤ 54 dB(A)) rows are shown as "
            "<b>indicative reference comparisons only</b>: these WHO guidelines were derived from studies that "
            "attributed noise exclusively to a single source, but the sound level meter measures total combined "
            "acoustic energy and cannot confirm the source. They therefore report how the measured level sits "
            "relative to the reference, not a pass/fail verdict. "
            "Additionally, WHO 2018 intends Lden/Lnight to represent long-term annual average exposure; a "
            "measurement period of days or weeks is indicative only. "
            "This information is provided to prevent misinterpretation of the compliance results.",
            who_note_style
        ))
        story.append(Spacer(1, 0.12 * inch))

    # ============================================================
    # SECTION 4: SINGLE-EVENT SLEEP DISTURBANCE (L-MAX)
    # ============================================================

    def _add_section_4_sleep_disturbance(self, story, styles, *, ts: pd.Series, lmax_col: str | None):
        story.append(Paragraph("Section 4: Single-Event Sleep Disturbance (L-Max only)", styles['h1']))

        if not lmax_col or lmax_col not in self.df.columns:
            story.append(Paragraph("L-Max stream not available.", styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))
            return

        lmax = self._get_numeric_series(lmax_col)
        h = ts.dt.hour
        is_night = (h >= 23) | (h < 7)
        night = lmax[is_night]

        if night.dropna().empty:
            story.append(Paragraph("No valid nighttime L-Max samples found (23:00–07:00).", styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))
            return

        la_max_night = float(night.max())
        exceed_threshold = 60.0
        n_events = int((night > exceed_threshold).sum())
        n_total = int(night.notna().sum())
        pct = (100.0 * n_events / max(1, n_total))

        # Convert samples to minutes (1 Hz assumption)
        sampling_rate_hz = 1.0
        minutes_exceeding = n_events / (sampling_rate_hz * 60.0)

        story.append(Paragraph(
            f"<b>Nighttime hours isolated:</b> 23:00–07:00. Peak extraction uses <b>{escape(lmax_col)}</b> only.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph(
            f"<b>Absolute highest nighttime peak (LAmax):</b> {escape(self._fmt_db(la_max_night))}",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph(
            f"<b>Cumulative nighttime exposure exceeding {self._fmt_float(exceed_threshold)} dB(A) (outdoor facade threshold):</b> {self._fmt_float(minutes_exceeding, 1)} minutes ({pct:.1f}% of nighttime samples)",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph(
            "An outdoor façade level of 60 dB(A) generally translates to ~45 dB(A) indoors with partially open windows, the WHO threshold for physiological sleep awakening.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.12 * inch))

    # ============================================================
    # SECTION 5: DAILY SUMMARY MATRIX
    # ============================================================

    def _add_section_5_daily_matrix(self, story, styles):
        story.append(Paragraph("Section 5: Daily Summary Matrix (Multi-Week Variance)", styles['h1']))

        if self.daily_summary is None or self.daily_summary.empty:
            story.append(Paragraph("No daily summary data available.", styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))
            return

        # Build table with strict column boundaries
        data = [["Date", "24-Hr LAeq (dB(A))", "Daytime 07-22 (dB(A))", "Nighttime 22-07 (dB(A))", "Daily Lden (dB(A))"]]

        for idx, row in self.daily_summary.iterrows():
            date_str = row.get('Date', 'N/A')
            if isinstance(date_str, str):
                try:
                    date_str = pd.to_datetime(date_str).strftime('%Y-%m-%d')
                except:
                    pass
            
            laeq_24 = self._fmt_db_plain(row.get('Average_L_EQ_dB'))
            laeq_day = self._fmt_db_plain(row.get('Daytime_LAeq', 'N/A'))  # May not be in CSV
            laeq_night = self._fmt_db_plain(row.get('Nighttime_LAeq', 'N/A'))
            lden = self._fmt_db_plain(row.get('Daily_Lden', 'N/A'))

            data.append([str(date_str), laeq_24, str(laeq_day), str(laeq_night), str(lden)])

        table = Table(data, colWidths=[1.4*inch, 1.6*inch, 1.8*inch, 1.8*inch, 1.8*inch])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3D5A80')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F0F4F8')),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#DDDDDD')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
        ]))
        story.append(table)
        story.append(Spacer(1, 0.12 * inch))

    # ============================================================
    # SECTION 6: DIURNAL HOURLY PROFILE (24-HOUR CYCLE)
    # ============================================================

    def _compute_duration_label(self, ts: 'pd.Series | None' = None) -> str:
        """Return a human-readable measurement duration (e.g. '2 weeks', '10 days')."""
        if ts is not None:
            ts_valid = ts.dropna()
        else:
            ts_col, _, _, _ = self._resolve_acoustic_columns()
            ts_valid = self._get_timestamp_series(ts_col).dropna()

        if ts_valid.empty:
            return "Unknown Duration"

        span_days = (ts_valid.max() - ts_valid.min()).total_seconds() / 86400
        if span_days < 1:
            return "< 1 day"
        if span_days < 7:
            d = round(span_days)
            return f"{d} day{'s' if d != 1 else ''}"
        weeks = span_days / 7
        if abs(weeks - round(weeks)) <= 0.15:
            w = round(weeks)
            return f"{w} week{'s' if w != 1 else ''}"
        return f"{weeks:.1f} weeks"

    def _add_section_6_hourly_profile(self, story, styles, *, ts: 'pd.Series | None' = None):
        duration_label = self._compute_duration_label(ts)
        story.append(Paragraph(f"Section 6: Diurnal Hourly Profile for {duration_label}", styles['h1']))

        if self.hourly_summary is None or self.hourly_summary.empty:
            story.append(Paragraph("No hourly summary data available.", styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))
            return

        # Build table with strict columns
        data = [["Hour of Day", "Average LAeq (dB(A))", "Min (dB(A))", "Max (dB(A))", "Std Dev (dB)"]]

        for idx, row in self.hourly_summary.iterrows():
            hour = int(row.get('Hour', idx))
            hour_str = f"{hour:02d}:00"
            
            avg_laeq = self._fmt_db_plain(row.get('Average_L_EQ_dB'))
            min_val = self._fmt_db_plain(row.get('Min_L_EQ_dB'))
            max_val = self._fmt_db_plain(row.get('Max_L_EQ_dB'))
            std_dev = self._fmt_float(row.get('Std_Dev'), 2)

            data.append([hour_str, avg_laeq, min_val, max_val, std_dev])

        table = Table(data, colWidths=[1.2*inch, 1.8*inch, 1.6*inch, 1.6*inch, 1.4*inch])
        table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3D5A80')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 10),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F0F4F8')),
            ('GRID', (0, 0), (-1, -1), 1, colors.HexColor('#DDDDDD')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
            ('FONTSIZE', (0, 0), (-1, -1), 9),
        ]))
        story.append(table)
        story.append(Spacer(1, 0.12 * inch))

    # ============================================================
    # SECTION 7: STATISTICAL NOISE PROFILE (PERCENTILES)
    # ============================================================

    def _add_section_7_percentiles(self, story, styles, *, leq_col: str | None):
        story.append(Paragraph("Section 7: Statistical Noise Profile (Percentiles)", styles['h1']))

        leq = self._get_numeric_series(leq_col)
        if leq.dropna().empty:
            story.append(Paragraph("LEQ stream not available.", styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))
            return

        exc = exceedance_levels_db(leq.dropna().to_numpy()) or {}

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
        table = Table(data, colWidths=[1.0*inch, 1.4*inch, 5.2*inch])
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
        story.append(Spacer(1, 0.12 * inch))

    # ============================================================
    # SECTION 8: ADVANCED VISUALIZATIONS
    # ============================================================

    def _add_section_8_visualizations(self, story, styles, *, ts: pd.Series, leq_col: str | None, lmax_col: str | None, lmin_col: str | None):
        story.append(PageBreak())
        story.append(Paragraph("Section 8: Advanced Visualizations", styles['h1']))

        leq = self._get_numeric_series(leq_col)
        if leq.dropna().empty:
            story.append(Paragraph("LEQ stream not available; visualizations cannot be generated.", styles['BodyText']))
            return

        lmax = self._get_numeric_series(lmax_col) if (lmax_col and lmax_col in self.df.columns) else None
        lmin = self._get_numeric_series(lmin_col) if (lmin_col and lmin_col in self.df.columns) else None

        # Chart 1: Time Series with WHO band
        story.append(Paragraph("Chart 1: Time Series (LAeq with L-Max/L-Min envelope &amp; WHO limits)", styles['h2']))
        fig1 = self._fig_time_series_with_band(ts=ts, leq=leq, lmax=lmax, lmin=lmin)
        if fig1:
            img1 = self._plotly_fig_to_image(fig1, width_inch=9.4, height_inch=4.5)
            if img1:
                story.append(img1)
            else:
                story.append(Paragraph("<i>Unable to render chart image.</i>", styles['BodyText']))
        story.append(Paragraph(
            "<b>What it is:</b> This time series shows the continuous LAeq (equivalent continuous noise level) "
            "across the full measurement period. "
            "<b>How it is calculated:</b> Each plotted point is an energy-averaged LAeq over the resampling interval "
            "(adaptive: 15 min for short datasets, up to 1 day for multi-month records). The L-Max/L-Min dotted "
            "lines show the Maximum and Minimum envelope; the purple line is a 1-hour rolling median. "
            "<b>How to read it:</b> The dark line is the sustained acoustic load. Dashed red horizontal lines "
            "mark WHO 2018 road-traffic thresholds (Lden 53 dB; Lnight 45 dB). Any sustained period above these "
            "lines represents a documented health risk window.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.2 * inch))

        # Chart 2: Box-and-whisker (diurnal volatility)
        story.append(Paragraph("Chart 2: Diurnal Box-and-Whisker (Hourly LEQ Volatility)", styles['h2']))
        fig2 = self._fig_diurnal_box_whisker(ts=ts, leq=leq)
        if fig2:
            img2 = self._plotly_fig_to_image(fig2, width_inch=9.4, height_inch=5.0)
            if img2:
                story.append(img2)
            else:
                story.append(Paragraph("<i>Unable to render chart image.</i>", styles['BodyText']))
        story.append(Paragraph(
            "<b>What it is:</b> This box-and-whisker plot breaks down the LEQ noise levels for each of the 24 hours "
            "of the day, pooled across all measurement days in the dataset. "
            "<b>How it is calculated:</b> It uses the raw continuous LEQ data to show the full statistical spread — "
            "not a single average. The box spans the IQR (25th–75th percentile); the centre line is the median (L50). "
            "<b>How to read it:</b> A tall box indicates acoustically unpredictable conditions at that hour. "
            "Dots above the upper whisker are extreme transient events (e.g. sirens, lorries). "
            "Hours with short, low boxes are stable and quiet.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.2 * inch))

        # Chart 3: Temporal Heatmap
        story.append(Paragraph("Chart 3: Temporal Heatmap (LAeq Intensity by Date &amp; Hour)", styles['h2']))
        fig3 = self._fig_temporal_heatmap(ts=ts, leq=leq)
        if fig3:
            img3 = self._plotly_fig_to_image(fig3, width_inch=9.0, height_inch=4.8)
            if img3:
                story.append(img3)
            else:
                story.append(Paragraph("<i>Unable to render chart image.</i>", styles['BodyText']))
        story.append(Paragraph(
            "<b>What it is:</b> This heatmap shows the LAeq noise intensity for every hour of every day in the "
            "filtered dataset. Each coloured cell represents one calendar hour on one date. "
            "<b>How it is calculated:</b> Each cell is computed using strict logarithmic energy averaging (LAeq) — "
            "never an arithmetic mean. Green cells indicate quiet periods; red/orange cells exceed guideline levels. "
            "<b>How to read it:</b> Scan vertically to identify the noisiest times of day; scan horizontally to "
            "spot unusually loud or quiet individual days. A consistently red row at 07:00–09:00 indicates a "
            "chronic morning traffic peak. The Y-axis tick for each hour aligns precisely to the centre of its "
            "corresponding cell.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.2 * inch))

        # Chart 4: Diurnal Noise Fingerprint — KeepTogether so chart + caption stay on same page
        story.append(PageBreak())
        c4_parts = [Paragraph("Chart 4: Diurnal Noise Fingerprint (24-Hour Polar Radar)", styles['h2'])]
        try:
            fig4 = self._fig_diurnal_radar(ts=ts, leq=leq)
            if fig4:
                img4 = self._plotly_fig_to_image(fig4, width_inch=6.0, height_inch=5.5)
                c4_parts.append(img4 if img4 else Paragraph("<i>Unable to render polar chart image.</i>", styles['BodyText']))
            else:
                c4_parts.append(Paragraph("<i>Insufficient hourly data to generate diurnal radar chart.</i>", styles['BodyText']))
        except Exception as e:
            print(f"[Report] Diurnal radar chart failed: {str(e)[:120]}")
            c4_parts.append(Paragraph("<i>Diurnal radar chart could not be generated.</i>", styles['BodyText']))
        c4_parts.append(Paragraph(
            "<b>What it is:</b> A polar radar chart showing the mean LAeq noise level for each of the 24 hours of the day, "
            "plotted clockwise from midnight (00:00) around the circle. "
            "<b>How it is calculated:</b> All measurements falling within each clock hour are energy-averaged (LAeq) across "
            "every day in the dataset. "
            "<b>How to read it:</b> The polygon shape is the site's acoustic fingerprint. "
            "A lopsided polygon peaking at 07:00–09:00 and 17:00–19:00 signals commuter-traffic dominance. "
            "A uniformly expanded polygon indicates a continuous source (industrial, motorway). "
            "Dashed reference rings show WHO Lden 53 dB and Lnight 45 dB thresholds.",
            styles['BodyText']
        ))
        c4_parts.append(Spacer(1, 0.15 * inch))
        story.append(KeepTogether(c4_parts))

        # Chart 5: Weekly Noise Profile — only add PageBreak when chart will actually render
        try:
            fig5 = self._fig_weekly_radar(ts=ts, leq=leq)
        except Exception as _e5:
            print(f"[Report] Weekly radar chart failed: {str(_e5)[:120]}")
            fig5 = None

        story.append(PageBreak())
        c5_parts = [Paragraph("Chart 5: Weekly Noise Profile (Day-of-Week Radar)", styles['h2'])]
        if fig5 is not None:
            img5 = self._plotly_fig_to_image(fig5, width_inch=6.0, height_inch=5.5)
            c5_parts.append(img5 if img5 is not None else
                            Paragraph("<i>Unable to render weekly radar chart image.</i>", styles['BodyText']))
        else:
            c5_parts.append(Paragraph(
                "<i>Weekly radar chart requires data spanning at least 3 distinct calendar days "
                "covering multiple days of the week. The current dataset does not meet that threshold — "
                "extend the measurement period to see day-of-week noise patterns.</i>",
                styles['BodyText']
            ))
        c5_parts.append(Paragraph(
            "<b>What it is:</b> A 7-spoke radar chart showing the mean LAeq noise level for each day of the week "
            "(Monday–Sunday), with separate traces for daytime (07:00–22:00) and nighttime (22:00–07:00). "
            "<b>How it is calculated:</b> All measurements are grouped by day-of-week and time-of-day period, "
            "then energy-averaged (LAeq) across all occurrences of that combination in the dataset. "
            "<b>How to read it:</b> A wider daytime polygon confirms daytime activity dominates. "
            "Shorter weekend spokes vs weekday spokes indicate traffic/commercial noise. "
            "Equal spokes indicate a continuous 24/7 source (industrial or heavy road).",
            styles['BodyText']
        ))
        story.append(KeepTogether(c5_parts))

    # ============================================================
    # CHART GENERATION
    # ============================================================

    def _fig_time_series_with_band(self, *, ts: pd.Series, leq: pd.Series, lmax: pd.Series | None, lmin: pd.Series | None):
        df = pd.DataFrame({'ts': ts, 'leq': leq})
        if lmax is not None:
            df['lmax'] = lmax
        if lmin is not None:
            df['lmin'] = lmin

        df = df.dropna(subset=['ts', 'leq']).sort_values('ts')
        if df.empty:
            return None

        # Multi-week reports become unreadable if every raw point is plotted.
        # Resample adaptively to preserve the envelope while keeping the LEQ trace legible.
        span = df['ts'].iloc[-1] - df['ts'].iloc[0]
        if span <= pd.Timedelta(days=3):
            freq = '15min'
        elif span <= pd.Timedelta(days=14):
            freq = '1h'
        elif span <= pd.Timedelta(days=60):
            freq = '6h'
        else:
            freq = '1D'

        df = df.set_index('ts')

        def _energy_mean(series):
            clean = series.dropna()
            return energetic_mean_db(clean) if len(clean) else np.nan

        agg = {'leq': _energy_mean}
        if 'lmax' in df.columns:
            agg['lmax'] = 'max'
        if 'lmin' in df.columns:
            agg['lmin'] = 'min'

        df_plot = df.resample(freq).agg(agg).dropna(subset=['leq'])
        if df_plot.empty:
            df_plot = df.copy()

        # Rolling smooth computed on the already-resampled series (fast path).
        # Window of 4 resampled points provides ~1-hour smoothing at the default
        # 15-min freq, and proportionally wider smoothing for coarser resolutions.
        roll_w = min(4, max(1, len(df_plot)))
        rolling_1h = df_plot['leq'].rolling(window=roll_w, min_periods=1).median().dropna()

        fig = go.Figure()

        # Draw the envelope first so the LAeq trace stays visually dominant.
        if 'lmax' in df_plot.columns and 'lmin' in df_plot.columns:
            fig.add_trace(go.Scatter(
                x=df_plot.index,
                y=df_plot['lmax'],
                mode='lines',
                name='L-Max envelope',
                line=dict(color='rgba(244,162,97,0.55)', width=1.5, dash='dot'),
                hoverinfo='skip',
            ))
            fig.add_trace(go.Scatter(
                x=df_plot.index,
                y=df_plot['lmin'],
                mode='lines',
                name='L-Min envelope',
                line=dict(color='rgba(42,157,143,0.55)', width=1.5, dash='dot'),
                fill='tonexty',
                fillcolor='rgba(42,157,143,0.12)',
                hoverinfo='skip',
            ))

        fig.add_trace(go.Scatter(
            x=df_plot.index,
            y=df_plot['leq'],
            mode='lines',
            name='LAeq',
            line=dict(color='#111111', width=3),
            hovertemplate='%{x|%d %b %Y %H:%M}<br>LAeq: %{y:.1f} dB(A)<extra></extra>',
        ))

        if not rolling_1h.empty:
            fig.add_trace(go.Scatter(
                x=rolling_1h.index,
                y=rolling_1h.values,
                mode='lines',
                name='Rolling 1-Hour Median (L50)',
                line=dict(color='#8E44AD', width=2.5),
                hovertemplate='%{x|%d %b %Y %H:%M}<br>Rolling 1-Hour Median: %{y:.1f} dB(A)<extra></extra>',
            ))

        fig.add_hline(
            y=53.0,
            line_dash='dash',
            line_color='rgba(231,111,81,0.95)',
            annotation_text='WHO 24-Hr Threshold',
            annotation_position='top left'
        )
        fig.add_hline(
            y=45.0,
            line_dash='dash',
            line_color='rgba(231,111,81,0.7)',
            annotation_text='WHO 24-Hr Threshold',
            annotation_position='bottom left'
        )

        tickformat = '%d %b\n%H:%M' if span <= pd.Timedelta(days=3) else '%d %b'
        fig.update_layout(
            title='Chart 1: Time Series (LAeq with L-Max/L-Min envelope & WHO limits)',
            xaxis=dict(
                title='Time',
                tickformat=tickformat,
                showgrid=True,
                gridcolor='rgba(0,0,0,0.08)',
                zeroline=False,
                showspikes=True,
                spikemode='across',
                spikesnap='cursor',
                spikedash='dot',
                spikecolor='rgba(0,0,0,0.35)',
            ),
            yaxis=dict(
                title='Sound Level (dB(A))',
                showgrid=True,
                gridcolor='rgba(0,0,0,0.08)',
                zeroline=False,
            ),
            legend=dict(
                orientation='h',
                yanchor='bottom',
                y=1.08,
                xanchor='left',
                x=0,
                bgcolor='rgba(255,255,255,0.85)',
                bordercolor='rgba(0,0,0,0.08)',
                borderwidth=1,
            ),
            margin=dict(l=55, r=25, t=90, b=55),
            autosize=True,
            height=420,
            hovermode='x unified',
            plot_bgcolor='white',
            paper_bgcolor='white',
            annotations=[dict(
                text=f"Source file: {os.path.basename(self.filepath)}",
                xref='paper',
                yref='paper',
                x=1,
                y=-0.20,
                xanchor='right',
                yanchor='top',
                showarrow=False,
                font=dict(size=9, color='rgba(80,80,80,0.85)')
            )],
        )
        return fig

    def _fig_diurnal_box_whisker(self, *, ts: pd.Series, leq: pd.Series):
        """Generate a diurnal box-and-whisker chart for hourly LEQ volatility."""
        try:
            df = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
            if df.empty:
                return None
            df['hour'] = df['ts'].dt.hour
            fig = go.Figure()

            for h in range(24):
                hour_values = pd.to_numeric(df.loc[df['hour'] == h, 'leq'], errors='coerce').dropna()
                if hour_values.empty:
                    continue

                # Compute exact statistics from ALL data — no sampling, no accuracy loss.
                # Only the pre-computed summary (6 numbers) + actual outlier points are
                # sent to kaleido, so the JSON payload stays tiny regardless of dataset size.
                q1  = float(hour_values.quantile(0.25))
                q3  = float(hour_values.quantile(0.75))
                iqr = q3 - q1
                med = float(hour_values.median())
                lf  = float(max(hour_values.min(), q1 - 1.5 * iqr))
                uf  = float(min(hour_values.max(), q3 + 1.5 * iqr))
                label = f'{h:02d}:00'

                # Box with analytically computed stats (no raw data bulk)
                fig.add_trace(go.Box(
                    q1=[q1], median=[med], q3=[q3],
                    lowerfence=[lf], upperfence=[uf],
                    x=[label],
                    name=label,
                    marker=dict(color='#3D5A80'),
                    line=dict(color='#293241', width=1.5),
                    fillcolor='rgba(61, 90, 128, 0.35)',
                    showlegend=False,
                    boxpoints=False,
                    hovertemplate='Hour: %{x}<br>Median: %{median:.1f} dB(A)<extra></extra>',
                ))

                # Plot exact outliers (points beyond 1.5×IQR fences).
                # Outliers are typically rare — all of them are preserved.
                outliers = hour_values[(hour_values < lf) | (hour_values > uf)]
                if not outliers.empty:
                    fig.add_trace(go.Scatter(
                        x=[label] * len(outliers),
                        y=outliers.values,
                        mode='markers',
                        marker=dict(color='#3D5A80', size=4, opacity=0.55),
                        showlegend=False,
                        hovertemplate='Hour: %{x}<br>LEQ: %{y:.1f} dB(A) (outlier)<extra></extra>',
                    ))

            fig.update_layout(
                title='Chart 2: Diurnal Box-and-Whisker (Hourly LEQ Volatility)',
                xaxis=dict(
                    title='Hour of Day',
                    categoryorder='array',
                    categoryarray=[f'{h:02d}:00' for h in range(24)],
                    tickmode='array',
                    tickvals=[f'{h:02d}:00' for h in range(24)],
                    ticktext=[f'{h:02d}:00' for h in range(24)],
                    automargin=True,
                ),
                yaxis=dict(
                    title='LEQ dB(A)',
                    showgrid=True,
                    gridcolor='rgba(0,0,0,0.08)',
                    zeroline=False,
                ),
                margin=dict(l=55, r=25, t=80, b=90),
                autosize=True,
                height=500,
                paper_bgcolor='white',
                plot_bgcolor='white',
                annotations=[dict(
                    text=f"Source file: {os.path.basename(self.filepath)}",
                    xref='paper',
                    yref='paper',
                    x=1,
                    y=-0.24,
                    xanchor='right',
                    yanchor='top',
                    showarrow=False,
                    font=dict(size=9, color='rgba(80,80,80,0.85)')
                )],
            )
            return fig
        except Exception as e:
            print(f"[Report] Box plot failed: {str(e)[:100]}")
            return None

    def _fig_temporal_heatmap(self, *, ts: pd.Series, leq: pd.Series):
        """Generate temporal heatmap (date x hour)."""
        df = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
        if df.empty:
            return None

        # Use full datetime (floor to midnight) for dates to allow stable sorting
        df['date'] = pd.to_datetime(df['ts']).dt.floor('D')
        df['hour'] = pd.to_datetime(df['ts']).dt.hour

        def _grp_energetic_mean(x):
            return energetic_mean_db(x)

        agg = (
            df.groupby(['date', 'hour'])['leq']
            .apply(_grp_energetic_mean)
            .reset_index(name='laeq')
        )

        pivot = agg.pivot(index='hour', columns='date', values='laeq')
        # Ensure index covers 0..23 in order
        pivot = pivot.reindex(index=list(range(24)))

        # Ensure continuous date columns across min->max
        if pivot.columns.size > 0:
            min_d = pd.to_datetime(min(pivot.columns))
            max_d = pd.to_datetime(max(pivot.columns))
            all_dates = pd.date_range(min_d, max_d, freq='D')
            pivot = pivot.reindex(columns=all_dates)
        else:
            all_dates = []

        x = [d.strftime('%Y-%m-%d') for d in getattr(pivot, 'columns', [])]
        y = [f'{h:02d}:00' for h in pivot.index]
        # Convert to float and preserve NaNs where data is missing
        z = pivot.values.astype(float) if pivot.size > 0 else np.empty((24, 0))

        # y is already a list of HH:00 strings.  Passing y-strings (not integers)
        # makes Plotly treat the axis as categorical, placing each tick exactly at
        # its cell centre — eliminating the classic half-cell offset bug.
        fig = go.Figure(
            data=go.Heatmap(
                z=z, x=x, y=y,
                colorscale='Viridis',
                colorbar=dict(title='LAeq dB(A)'),
                hovertemplate='Date: %{x}<br>Hour: %{y}<br>LAeq: %{z:.1f} dB(A)<extra></extra>',
                xgap=1,
                ygap=1,
            )
        )

        fig.update_layout(
            title='Chart 3: Temporal Heatmap (LAeq intensity by date & hour)',
            xaxis_title='Date', yaxis_title='Hour of Day',
            margin=dict(l=65, r=20, t=70, b=70), height=500,
            plot_bgcolor='white',
            paper_bgcolor='white',
            annotations=[dict(
                text=f"Source file: {os.path.basename(self.filepath)}",
                xref='paper', yref='paper', x=1, y=-0.22,
                xanchor='right', yanchor='top', showarrow=False,
                font=dict(size=9, color='rgba(80,80,80,0.85)')
            )],
        )

        # Tick labels are the same string list used as y — zero-offset alignment.
        fig.update_yaxes(
            tickmode='array',
            tickvals=y,
            ticktext=y,
            automargin=True,
            tickfont=dict(size=9),
            autorange='reversed',
        )
        fig.update_xaxes(tickangle=-45, automargin=True)

        return fig

    def _fig_diurnal_radar(self, *, ts: pd.Series, leq: pd.Series):
        """
        24-spoke polar radar: energy-averaged LAeq by hour of day (0–23).
        Uses pre-computed hourly_summary when available, falls back to raw ts/leq.
        Returns None if fewer than 6 hours have data.
        """
        # Build hourly LAeq from summary if already computed, else from raw series
        hourly_leq: dict[int, float] = {}

        if self.hourly_summary is not None:
            # hourly_summary has index = hour integer (0-23) and an LAeq column
            hs = self.hourly_summary
            leq_col_name = next((c for c in hs.columns if 'leq' in c.lower() or 'laeq' in c.lower() or 'average' in c.lower()), None)
            if leq_col_name is None and len(hs.columns) > 0:
                leq_col_name = hs.columns[0]
            if leq_col_name:
                for idx_val in hs.index:
                    v = hs.loc[idx_val, leq_col_name]
                    try:
                        h = int(idx_val)
                        if 0 <= h <= 23 and not np.isnan(float(v)):
                            hourly_leq[h] = float(v)
                    except (ValueError, TypeError):
                        pass

        if len(hourly_leq) < 6:
            # Fall back to raw series
            df = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
            if df.empty:
                return None
            df['ts'] = pd.to_datetime(df['ts'], errors='coerce')
            df = df.dropna(subset=['ts'])
            df['hour'] = df['ts'].dt.hour
            for h, grp in df.groupby('hour')['leq']:
                vals = pd.to_numeric(grp, errors='coerce').dropna()
                if len(vals):
                    hourly_leq[int(h)] = energetic_mean_db(vals)

        if len(hourly_leq) < 6:
            return None

        hours = list(range(24))
        r_vals = [hourly_leq.get(h, np.nan) for h in hours]
        theta_labels = [f"{h:02d}:00" for h in hours]

        # Close the loop
        r_closed = r_vals + [r_vals[0]]
        theta_closed = theta_labels + [theta_labels[0]]

        valid = [v for v in r_vals if not np.isnan(v)]
        r_min = max(0, min(valid) - 5) if valid else 30
        r_max = max(valid) + 5 if valid else 90

        fig = go.Figure()

        # WHO reference rings
        who_lnight = 45.0
        who_lden   = 53.0
        for ref_val, ref_label, ref_color in [
            (who_lnight, "WHO Lnight 45 dB", "rgba(52,152,219,0.5)"),
            (who_lden,   "WHO Lden 53 dB",   "rgba(231,76,60,0.5)"),
        ]:
            r_ring = [ref_val] * 25
            fig.add_trace(go.Scatterpolar(
                r=r_ring,
                theta=theta_closed,
                mode='lines',
                name=ref_label,
                line=dict(color=ref_color, width=1.5, dash='dash'),
                showlegend=True,
            ))

        # Night band shading (22:00–07:00 spokes dimmed via a filled area near centre)
        night_hours = list(range(22, 24)) + list(range(0, 7))
        night_theta = [f"{h:02d}:00" for h in night_hours] + [f"{night_hours[0]:02d}:00"]
        night_r = [r_min + 1] * len(night_theta)
        fig.add_trace(go.Scatterpolar(
            r=night_r,
            theta=night_theta,
            mode='lines',
            fill='toself',
            fillcolor='rgba(44,62,80,0.10)',
            line=dict(color='rgba(0,0,0,0)', width=0),
            name='Night (22:00–07:00)',
            showlegend=True,
        ))

        # Main LAeq trace
        fig.add_trace(go.Scatterpolar(
            r=r_closed,
            theta=theta_closed,
            mode='lines+markers',
            fill='toself',
            fillcolor='rgba(52,152,219,0.25)',
            line=dict(color='rgba(41,128,185,1)', width=2.5),
            marker=dict(size=7, color='rgba(41,128,185,1)'),
            name='Mean LAeq',
        ))

        fig.update_layout(
            polar=dict(
                radialaxis=dict(
                    visible=True,
                    range=[r_min, r_max],
                    ticksuffix=' dB',
                    tickfont=dict(size=9),
                    gridcolor='rgba(180,180,180,0.5)',
                    linecolor='rgba(150,150,150,0.6)',
                ),
                angularaxis=dict(
                    direction='clockwise',
                    tickfont=dict(size=10),
                    gridcolor='rgba(180,180,180,0.4)',
                ),
                bgcolor='rgba(248,249,250,1)',
            ),
            title=dict(
                text='Diurnal Noise Fingerprint — Mean LAeq by Hour of Day',
                font=dict(size=14, color='#1a1a2e'),
                x=0.5,
            ),
            legend=dict(orientation='h', yanchor='bottom', y=-0.15, xanchor='center', x=0.5, font=dict(size=9)),
            paper_bgcolor='white',
            width=700,
            height=700,
            margin=dict(l=60, r=60, t=80, b=80),
        )
        return fig

    def _fig_weekly_radar(self, *, ts: pd.Series, leq: pd.Series):
        """
        7-spoke radar: energy-averaged LAeq by day of week, daytime vs nighttime.
        Returns None if data spans fewer than 7 distinct calendar days.
        """
        df = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
        if df.empty:
            return None
        df['ts'] = pd.to_datetime(df['ts'], errors='coerce')
        df = df.dropna(subset=['ts'])
        df['leq'] = pd.to_numeric(df['leq'], errors='coerce')
        df = df.dropna(subset=['leq'])

        # Require at least 3 distinct calendar dates covering at least 3 different days-of-week
        if df['ts'].dt.date.nunique() < 3 or df['ts'].dt.dayofweek.nunique() < 3:
            return None

        df['dow'] = df['ts'].dt.dayofweek          # 0=Mon … 6=Sun
        df['hour'] = df['ts'].dt.hour
        df['is_day'] = df['hour'].between(7, 21)   # 07:00–21:59 inclusive = daytime

        day_labels = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']

        day_leq  = {}
        night_leq = {}
        for dow in range(7):
            sub = df[df['dow'] == dow]
            day_sub   = sub[sub['is_day']]['leq'].dropna()
            night_sub = sub[~sub['is_day']]['leq'].dropna()
            day_leq[dow]   = energetic_mean_db(day_sub)   if len(day_sub)   else np.nan
            night_leq[dow] = energetic_mean_db(night_sub) if len(night_sub) else np.nan

        r_day   = [day_leq.get(d, np.nan)   for d in range(7)]
        r_night = [night_leq.get(d, np.nan) for d in range(7)]

        # Close the loop
        theta_closed = day_labels + [day_labels[0]]
        r_day_c   = r_day   + [r_day[0]]
        r_night_c = r_night + [r_night[0]]

        all_vals = [v for v in r_day + r_night if not np.isnan(v)]
        if not all_vals:
            return None
        r_min = max(0, min(all_vals) - 5)
        r_max = max(all_vals) + 5

        fig = go.Figure()

        # WHO reference rings
        for ref_val, ref_label, ref_color in [
            (45.0, "WHO Lnight 45 dB", "rgba(52,152,219,0.5)"),
            (53.0, "WHO Lden 53 dB",   "rgba(231,76,60,0.5)"),
        ]:
            fig.add_trace(go.Scatterpolar(
                r=[ref_val] * 8,
                theta=theta_closed,
                mode='lines',
                name=ref_label,
                line=dict(color=ref_color, width=1.5, dash='dash'),
                showlegend=True,
            ))

        fig.add_trace(go.Scatterpolar(
            r=r_day_c,
            theta=theta_closed,
            mode='lines+markers',
            fill='toself',
            fillcolor='rgba(230,126,34,0.25)',
            line=dict(color='rgba(211,84,0,1)', width=2.5),
            marker=dict(size=8, color='rgba(211,84,0,1)'),
            name='Daytime LAeq (07:00–22:00)',
        ))
        fig.add_trace(go.Scatterpolar(
            r=r_night_c,
            theta=theta_closed,
            mode='lines+markers',
            fill='toself',
            fillcolor='rgba(41,128,185,0.20)',
            line=dict(color='rgba(41,128,185,1)', width=2.5, dash='dot'),
            marker=dict(size=8, color='rgba(41,128,185,1)'),
            name='Nighttime LAeq (22:00–07:00)',
        ))

        fig.update_layout(
            polar=dict(
                radialaxis=dict(
                    visible=True,
                    range=[r_min, r_max],
                    ticksuffix=' dB',
                    tickfont=dict(size=9),
                    gridcolor='rgba(180,180,180,0.5)',
                    linecolor='rgba(150,150,150,0.6)',
                ),
                angularaxis=dict(
                    direction='clockwise',
                    tickfont=dict(size=11),
                    gridcolor='rgba(180,180,180,0.4)',
                ),
                bgcolor='rgba(248,249,250,1)',
            ),
            title=dict(
                text='Weekly Noise Profile — Daytime vs Nighttime LAeq by Day of Week',
                font=dict(size=14, color='#1a1a2e'),
                x=0.5,
            ),
            legend=dict(orientation='h', yanchor='bottom', y=-0.18, xanchor='center', x=0.5, font=dict(size=9)),
            paper_bgcolor='white',
            width=700,
            height=700,
            margin=dict(l=60, r=60, t=80, b=100),
        )
        return fig

    # ============================================================
    # PDF RENDERING & UTILITIES
    # ============================================================

    def _plotly_fig_to_image(self, fig, width_inch=8, height_inch=4):
        """Convert Plotly figure to ReportLab Image via kaleido."""
        if fig is None:
            return None
        try:
            scale = 1.25 if len(self.df) > 500_000 else 2
            img_bytes = fig.to_image(format="png", scale=scale)
            img = Image(io.BytesIO(img_bytes))
            aspect = img.imageHeight / img.imageWidth if img.imageWidth > 0 else 1
            img.drawWidth  = width_inch * inch
            img.drawHeight = (width_inch * inch) * aspect
            if img.drawHeight > height_inch * inch:
                img.drawHeight = height_inch * inch
                img.drawWidth  = (height_inch * inch) / aspect
            return img
        except Exception as e:
            print(f"[Report] Chart rendering failed: {str(e)[:120]}")
            return None

    def _get_pdf_styles(self):
        """Define PDF styles."""
        styles = getSampleStyleSheet()
        
        styles['Title'].fontSize = 24
        styles['Title'].leading = 30
        styles['Title'].alignment = TA_CENTER
        styles['Title'].textColor = colors.HexColor('#3D5A80')

        styles['h1'].textColor = colors.HexColor('#3D5A80')
        styles['h1'].fontSize = 16
        styles['h1'].leading = 20
        styles['h1'].spaceBefore = 12
        styles['h1'].spaceAfter = 8
        styles['h1'].keepWithNext = 1

        styles['h2'].textColor = colors.HexColor('#293241')
        styles['h2'].fontSize = 12
        styles['h2'].leading = 16
        styles['h2'].spaceBefore = 10
        styles['h2'].spaceAfter = 6
        styles['h2'].keepWithNext = 1

        styles['BodyText'].fontSize = 9
        styles['BodyText'].leading = 12
        styles['BodyText'].alignment = TA_LEFT

        styles.add(ParagraphStyle(name='SubTitle', fontSize=12, parent=styles['Normal'], 
                                 alignment=TA_CENTER, textColor=colors.HexColor('#666666')))
        styles.add(ParagraphStyle(name='Footer', fontSize=8, parent=styles['Normal'], 
                                 alignment=TA_CENTER, textColor=colors.grey))
        
        return styles

    def _add_page_template(self, canvas, doc):
        """Add header/footer."""
        canvas.saveState()
        footer_text = f"Page {doc.page} | Environmental Noise Analysis | {datetime.now().strftime('%Y-%m-%d')}"
        canvas.setFont('Helvetica', 8)
        canvas.drawCentredString(doc.width / 2 + doc.leftMargin, 0.30 * inch, footer_text)
        canvas.setFont('Helvetica', 7)
        canvas.setFillColorRGB(0.5, 0.5, 0.5)
        canvas.drawCentredString(
            doc.width / 2 + doc.leftMargin, 0.13 * inch,
            "Developed by Chandra Prakash Choudhary | PI: Dr. Ana María Rule, Associate Professor — Johns Hopkins University"
        )
        canvas.setStrokeColorRGB(0.239, 0.353, 0.502)
        canvas.setLineWidth(2)
        canvas.line(doc.leftMargin, doc.height + doc.topMargin + 0.2 * inch,
                    doc.width + doc.leftMargin, doc.height + doc.topMargin + 0.2 * inch)
        canvas.restoreState()

    def _add_pdf_header(self, story, styles, report_type):
        """Add report header."""
        story.append(Paragraph("Environmental Noise Analysis Report", styles['Title']))
        story.append(Spacer(1, 0.18 * inch))

        if self.device_id:
            story.append(Paragraph(f"Device / Location: {escape(self.device_id)}", styles['SubTitle']))

        # Source file(s)
        if self.source_files:
            files_str = " | ".join(escape(f) for f in self.source_files)
            story.append(Paragraph(f"Source Files ({len(self.source_files)}): {files_str}", styles['SubTitle']))
        else:
            story.append(Paragraph(f"Source File: {escape(os.path.basename(self.filepath))}", styles['SubTitle']))

        story.append(Paragraph(
            f"Report Type: {report_type.upper()} | Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            styles['SubTitle']
        ))
        story.append(Spacer(1, 0.3 * inch))
