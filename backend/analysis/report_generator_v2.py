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
from analysis.gap_detector import detect_gaps, gap_report_to_dict
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
                 custom_section_heading: str = '', custom_section_body: str = ''):
        """
        Initialize report generator with ONLY the uploaded data.
        NO external CSV file loading - all summaries computed from df.
        """
        self.df = df.copy()
        self.filepath = filepath
        self.device_id = str(device_id or '').strip()
        self.source_files = list(source_files or [])
        self.merge_gap_report = merge_gap_report  # pre-computed gap dict from the merge step
        self.custom_section_heading = str(custom_section_heading or '').strip()
        self.custom_section_body = str(custom_section_body or '').strip()
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
        Generate a one-paragraph plain-English summary from acoustic statistics.

        All thresholds are sourced exclusively from:
          - WHO Environmental Noise Guidelines for the European Region (2018), Tables 1–3
            https://iris.who.int/bitstream/handle/10665/279952/9789289053563-eng.pdf
          - WHO Guidelines for Community Noise (Berglund et al., 1999)

        WHO 2018 strong-recommendation thresholds used:
          Road traffic: Lden ≤ 53 dB(A), Lnight ≤ 45 dB(A)
          LOAEL (first sleep effects): Lnight = 40 dB(A)
        """
        def _f(v, d=1):
            try:
                return f"{float(v):.{d}f}" if v is not None and np.isfinite(float(v)) else None
            except Exception:
                return None

        # ── 1. Noise level context (everyday analogies) ──────────────────────
        # Analogies based on ISO 226 reference levels and established acoustic
        # literature (Berglund et al. 1999, WHO 2018 explanatory notes)
        try:
            laeq_v = float(laeq) if laeq is not None and np.isfinite(float(laeq)) else None
        except Exception:
            laeq_v = None

        if laeq_v is None:
            return "A plain-English summary could not be generated because the average noise level (LAeq) could not be computed — check that the dataset contains valid numeric measurements."

        if laeq_v < 40:
            level_desc = "very quiet — comparable to a rural area at night or a library reading room"
        elif laeq_v < 50:
            level_desc = "quiet — comparable to a calm residential street at night or soft rainfall"
        elif laeq_v < 55:
            level_desc = "moderate — comparable to a typical residential neighbourhood during the day"
        elif laeq_v < 65:
            level_desc = "elevated — comparable to a busy urban street or a bustling café"
        elif laeq_v < 75:
            level_desc = "high — comparable to heavy road traffic or a passing freight train"
        else:
            level_desc = "very high — comparable to a construction zone or an expressway at close range"

        # ── 2. Date / duration context ───────────────────────────────────────
        date_ctx = ""
        if start_date and end_date:
            date_ctx = f" from {start_date} to {end_date}"
        elif duration_label:
            date_ctx = f" over {duration_label}"

        day_word = f"{n_days} day{'s' if n_days != 1 else ''}" if n_days > 0 else duration_label or "the measurement period"

        completeness_note = ""
        if data_completeness_pct is not None:
            try:
                cp = float(data_completeness_pct)
                if cp < 90:
                    completeness_note = f" Data completeness was {cp:.0f}% — some gaps exist and averages may slightly underestimate or overestimate true exposure."
                else:
                    completeness_note = f" Data completeness was {cp:.0f}%."
            except Exception:
                pass

        # ── 3. Day / night context ───────────────────────────────────────────
        # Note on time periods:
        # WHO Lden uses three sub-periods: day 07:00–19:00, evening 19:00–23:00 (+5 dB penalty),
        # night 23:00–07:00 (+10 dB penalty) — per EU Directive 2002/49/EC and WHO 2018.
        # WHO Lnight covers 23:00–07:00.
        # Maryland COMAR uses daytime 07:00–22:00 / nighttime 22:00–07:00.
        # The LAeq_day / LAeq_night values here use Maryland's 07:00–22:00 split.
        # WHO compliance in section 4 uses correctly-computed Lden and Lnight.
        day_night_ctx = ""
        try:
            if laeq_day is not None and laeq_night is not None:
                dv = float(laeq_day)
                nv = float(laeq_night)
                if np.isfinite(dv) and np.isfinite(nv):
                    diff = dv - nv
                    if diff > 5:
                        day_night_ctx = (
                            f" Daytime levels (07:00–22:00, Maryland COMAR period) averaged {_f(dv)} dB(A) and "
                            f"nighttime levels (22:00–07:00) averaged {_f(nv)} dB(A) — a {_f(abs(diff))} dB "
                            f"difference, indicating activity-driven or traffic-related noise."
                        )
                    elif diff < -3:
                        day_night_ctx = (
                            f" Daytime levels (07:00–22:00) averaged {_f(dv)} dB(A) and nighttime levels "
                            f"(22:00–07:00) averaged {_f(nv)} dB(A) — unusually, nighttime is louder than "
                            f"daytime, suggesting a nocturnal noise source."
                        )
                    else:
                        day_night_ctx = (
                            f" Daytime levels (07:00–22:00) averaged {_f(dv)} dB(A) and nighttime levels "
                            f"(22:00–07:00) averaged {_f(nv)} dB(A) — similar day and night levels suggest "
                            f"a relatively constant noise source."
                        )
        except Exception:
            pass

        # ── 4. WHO 2018 compliance ─────────────────────────────────────────────
        # WHO 2018 road-traffic strong recommendation: Lden ≤ 53 dB(A), Lnight ≤ 45 dB(A)
        # WHO 2018 LOAEL (first adverse sleep effect): Lnight = 40 dB(A)
        WHO_LDEN_LIMIT   = 53.0   # WHO 2018, Table 1
        WHO_LNIGHT_LIMIT = 45.0   # WHO 2018, Table 1
        WHO_LOAEL_NIGHT  = 40.0   # WHO 2018, Section 4.1

        compliance_parts = []
        concern_level = "LOW"

        try:
            if lden is not None and np.isfinite(float(lden)):
                lden_v = float(lden)
                if lden_v > WHO_LDEN_LIMIT:
                    excess = lden_v - WHO_LDEN_LIMIT
                    compliance_parts.append(
                        f"The 24-hour weighted average (Lden) of {_f(lden_v)} dB(A) "
                        f"exceeds the WHO 2018 road-traffic health guideline of {WHO_LDEN_LIMIT} dB(A) by {_f(excess)} dB"
                    )
                    concern_level = "HIGH" if excess >= 8 else "MODERATE-HIGH"
                else:
                    compliance_parts.append(
                        f"The 24-hour weighted average (Lden) of {_f(lden_v)} dB(A) "
                        f"is within the WHO 2018 road-traffic health guideline of {WHO_LDEN_LIMIT} dB(A)"
                    )
        except Exception:
            pass

        try:
            if lnight is not None and np.isfinite(float(lnight)):
                lnight_v = float(lnight)
                if lnight_v > WHO_LNIGHT_LIMIT:
                    excess = lnight_v - WHO_LNIGHT_LIMIT
                    compliance_parts.append(
                        f"the nighttime level (Lnight) of {_f(lnight_v)} dB(A) "
                        f"exceeds the WHO 2018 sleep-protection limit of {WHO_LNIGHT_LIMIT} dB(A) by {_f(excess)} dB"
                    )
                    if concern_level == "LOW":
                        concern_level = "MODERATE-HIGH"
                    elif concern_level == "MODERATE":
                        concern_level = "HIGH"
                elif lnight_v > WHO_LOAEL_NIGHT:
                    compliance_parts.append(
                        f"the nighttime level (Lnight) of {_f(lnight_v)} dB(A) "
                        f"is within the WHO 2018 limit of {WHO_LNIGHT_LIMIT} dB(A) but above the WHO "
                        f"lowest-observed-adverse-effect level (LOAEL) of {WHO_LOAEL_NIGHT} dB(A), "
                        f"at which initial sleep movement effects begin"
                    )
                    if concern_level == "LOW":
                        concern_level = "MODERATE"
                else:
                    compliance_parts.append(
                        f"the nighttime level (Lnight) of {_f(lnight_v)} dB(A) "
                        f"is below the WHO 2018 LOAEL of {WHO_LOAEL_NIGHT} dB(A) — no sleep effects expected"
                    )
        except Exception:
            pass

        # Fallback when Lden/Lnight not available — use LAeq as approximation with caveat
        if not compliance_parts:
            if laeq_v > 65:
                concern_level = "HIGH"
                compliance_parts.append(
                    f"the average noise level of {_f(laeq_v)} dB(A) suggests exceedance of WHO health guidelines "
                    f"(Lden/Lnight metrics were not computable — likely no timestamp data available)"
                )
            elif laeq_v > 53:
                concern_level = "MODERATE"
                compliance_parts.append(
                    f"the average noise level of {_f(laeq_v)} dB(A) is in a range that may exceed WHO Lden guidelines "
                    f"(Lden/Lnight metrics were not computable from this dataset)"
                )
            else:
                concern_level = "LOW"
                compliance_parts.append(
                    f"the average noise level of {_f(laeq_v)} dB(A) is below the WHO Lden threshold of 53 dB(A)"
                )

        # Normalise concern level
        if concern_level in ("MODERATE-HIGH",):
            concern_level = "HIGH"

        # ── 5. Variability note ───────────────────────────────────────────────
        variability_note = ""
        try:
            if l10 is not None and l90 is not None:
                l10_v = float(l10)
                l90_v = float(l90)
                if np.isfinite(l10_v) and np.isfinite(l90_v):
                    spread = l10_v - l90_v
                    if spread > 20:
                        variability_note = (
                            f" The noise environment is highly variable: levels exceeded 10% of the time (L10 = {_f(l10_v)} dB(A)) "
                            f"were {_f(spread)} dB higher than the background level (L90 = {_f(l90_v)} dB(A)), "
                            f"indicating frequent loud transient events such as passing vehicles or machinery."
                        )
                    elif spread > 12:
                        variability_note = (
                            f" Moderate variability was observed (L10 = {_f(l10_v)} dB(A), L90 = {_f(l90_v)} dB(A), "
                            f"spread = {_f(spread)} dB), suggesting intermittent noise sources alongside a background level."
                        )
                    else:
                        variability_note = (
                            f" The noise environment is relatively stable (L10–L90 spread = {_f(spread)} dB), "
                            f"consistent with a continuous or steady noise source."
                        )
        except Exception:
            pass

        # ── 6. Peak note ─────────────────────────────────────────────────────
        peak_note = ""
        try:
            if laeq_max is not None and np.isfinite(float(laeq_max)):
                peak_note = f" The highest single recorded level was {_f(float(laeq_max))} dB(A)."
        except Exception:
            pass

        # ── 7. Concern-level recommendation ──────────────────────────────────
        concern_map = {
            "LOW": (
                "LOW",
                "The acoustic environment is generally within WHO health-based guidelines. "
                "No immediate action is indicated, but periodic re-monitoring is advisable."
            ),
            "MODERATE": (
                "MODERATE",
                "Noise levels are within WHO guidelines but above the LOAEL for nighttime sleep effects. "
                "Continued monitoring is recommended, particularly for sensitive occupants such as children or elderly residents."
            ),
            "HIGH": (
                "HIGH",
                "WHO 2018 health-based guidelines are exceeded. Based on WHO evidence, prolonged exposure at this level "
                "is associated with increased risk of cardiovascular effects (hypertension, ischaemic heart disease) "
                "and impaired sleep quality. Professional acoustic assessment and noise-reduction measures are recommended."
            ),
            "SERIOUS": (
                "SERIOUS",
                "Noise levels significantly exceed WHO guidelines. WHO 2018 identifies strong cardiovascular and "
                "sleep health risks at these levels. Immediate professional acoustic assessment is strongly recommended."
            ),
        }

        if concern_level not in concern_map:
            concern_level = "HIGH" if laeq_v > 55 else "MODERATE"

        concern_tag, concern_rec = concern_map[concern_level]

        # ── Assemble paragraph ───────────────────────────────────────────────
        compliance_sentence = "; ".join(compliance_parts) + "."
        compliance_sentence = compliance_sentence[:1].upper() + compliance_sentence[1:]

        summary = (
            f"This dataset captures {day_word} of continuous outdoor noise monitoring{date_ctx}.{completeness_note} "
            f"The overall energy-average noise level (LAeq) was {_f(laeq_v)} dB(A) — {level_desc}.{day_night_ctx}"
            f"{variability_note}{peak_note} "
            f"{compliance_sentence} "
            f"Overall concern level: {concern_tag}. {concern_rec}"
        )
        return " ".join(summary.split())

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
        """Generate a comprehensive standalone HTML report with all sections, charts, and tables."""

        report_filename = f"noise_analysis_{report_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        report_dir = output_dir or os.path.dirname(self.filepath)
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, report_filename)

        ts_col, leq_col, lmax_col, lmin_col = self._resolve_acoustic_columns()
        ts  = self._get_timestamp_series(ts_col)
        leq = self._get_numeric_series(leq_col)
        lmax = self._get_numeric_series(lmax_col) if lmax_col else None
        lmin = self._get_numeric_series(lmin_col) if lmin_col else None

        ts_valid = ts.dropna()
        start_str = ts_valid.min().strftime('%Y-%m-%d %H:%M:%S') if not ts_valid.empty else 'N/A'
        end_str   = ts_valid.max().strftime('%Y-%m-%d %H:%M:%S') if not ts_valid.empty else 'N/A'
        duration_label = self._compute_duration_label(ts)
        n_days_v = int(round((ts_valid.max() - ts_valid.min()).total_seconds() / 86400)) if not ts_valid.empty else 0
        expected_s = max(0.0, (ts_valid.max() - ts_valid.min()).total_seconds()) if not ts_valid.empty else 0
        completeness = 100.0 * len(self.df) / max(1, expected_s) if expected_s > 0 else None

        # Acoustic metrics
        laeq_v   = energetic_mean_db(leq) if not leq.dropna().empty else None
        env      = compute_ldn_lden(ts, leq) or {} if not leq.dropna().empty else {}
        lden_v   = env.get('Lden')
        lnight_v = env.get('Lnight')
        h = ts.dt.hour
        is_day   = (h >= 7) & (h < 22)
        is_night = ~is_day
        laeq_day_v   = energetic_mean_db(leq[is_day])   if is_day.any()   else None
        laeq_night_v = energetic_mean_db(leq[is_night]) if is_night.any() else None
        exc = exceedance_levels_db(leq.dropna().to_numpy()) or {} if not leq.dropna().empty else {}

        # Plain-English summary
        summary_text = ReportGeneratorV2.generate_plain_english_summary(
            laeq=laeq_v, lden=lden_v, lnight=lnight_v,
            laeq_day=laeq_day_v, laeq_night=laeq_night_v,
            laeq_min=float(leq.min()) if not leq.dropna().empty else None,
            laeq_max=float(leq.max()) if not leq.dropna().empty else None,
            l10=exc.get('L10'), l90=exc.get('L90'),
            start_date=ts_valid.min().strftime('%d %b %Y') if not ts_valid.empty else '',
            end_date=ts_valid.max().strftime('%d %b %Y') if not ts_valid.empty else '',
            duration_label=duration_label,
            data_completeness_pct=completeness,
            n_days=n_days_v,
        )

        # Concern level colour for summary box
        concern_color = '#1e3a5f'
        concern_bg    = '#EFF6FF'
        if 'concern level: HIGH' in summary_text or 'concern level: SERIOUS' in summary_text:
            concern_color = '#991b1b'; concern_bg = '#FEF2F2'
        elif 'concern level: MODERATE' in summary_text:
            concern_color = '#92400e'; concern_bg = '#FFFBEB'

        # Compliance results for table
        try:
            compliance_results = evaluate_compliance(
                lden=lden_v, lnight=lnight_v, laeq=laeq_v,
                laeq_day=laeq_day_v, laeq_night=laeq_night_v,
                lamax=float(lmax.max()) if lmax is not None and not lmax.dropna().empty else None,
            )
        except Exception:
            compliance_results = []

        def _hfmt(v):
            try:
                fv = float(v)
                return f"{fv:.1f}" if fv is not None and np.isfinite(fv) else "N/A"
            except Exception:
                return "N/A"

        def _esc(s):
            return str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

        # ── Compliance table rows HTML ──
        comp_rows_html = ""
        for r in compliance_results:
            bg  = '#dcfce7' if r['status'] == 'PASS' else '#fee2e2'
            clr = '#166534' if r['status'] == 'PASS' else '#991b1b'
            delta = abs(r['delta_db'])
            margin_text = (f"Within limit by {delta:.1f} dB" if r['status'] == 'PASS'
                           else f"Exceeds limit by {delta:.1f} dB")
            comp_rows_html += (
                f"<tr>"
                f"<td>{_esc(r['standard'])}</td>"
                f"<td>{_esc(r['metric'])}</td>"
                f"<td style='text-align:right'>{_hfmt(r['measured_db'])}</td>"
                f"<td style='text-align:right'>{_hfmt(r['limit_db'])}</td>"
                f"<td style='background:{bg};color:{clr};font-weight:600;text-align:center'>"
                f"{r['status']} — {margin_text}</td>"
                f"</tr>"
            )

        # ── Percentile table rows ──
        pct_rows_html = ""
        pct_defs = {
            "L5":  "Exceeded 5% of the time — captures peak / transient events",
            "L10": "Exceeded 10% of the time — frequent high-noise events",
            "L50": "Exceeded 50% of the time — median acoustic level",
            "L90": "Exceeded 90% of the time — background / ambient noise floor",
            "L95": "Exceeded 95% of the time — near-constant background level",
        }
        for k in ["L5", "L10", "L50", "L90", "L95"]:
            v = exc.get(k)
            pct_rows_html += (
                f"<tr><td><b>{k}</b></td>"
                f"<td style='text-align:right'>{_hfmt(v)}</td>"
                f"<td>{pct_defs.get(k, '')}</td></tr>"
            )

        # ── Daily summary table rows ──
        daily_rows_html = ""
        if self.daily_summary is not None and not self.daily_summary.empty:
            for _, row in self.daily_summary.iterrows():
                d = row.get('Date', '')
                try:
                    d = pd.Timestamp(d).strftime('%Y-%m-%d')
                except Exception:
                    d = str(d)
                daily_rows_html += (
                    f"<tr>"
                    f"<td>{_esc(d)}</td>"
                    f"<td style='text-align:right'>{_hfmt(row.get('Average_L_EQ_dB'))}</td>"
                    f"<td style='text-align:right'>{_hfmt(row.get('Daytime_LAeq'))}</td>"
                    f"<td style='text-align:right'>{_hfmt(row.get('Nighttime_LAeq'))}</td>"
                    f"<td style='text-align:right'>{_hfmt(row.get('Daily_Lden'))}</td>"
                    f"</tr>"
                )

        # ── Charts ──
        charts_specs = [
            ("Chart 1: Time Series — LAeq with L-Max/L-Min Envelope & WHO Limits",
             self._fig_time_series_with_band(ts=ts, leq=leq, lmax=lmax, lmin=lmin),
             "Shows the full LAeq time series across the measurement period with the L-Max/L-Min envelope and WHO guideline reference lines (Lden 53 dB, Lnight 45 dB). Each point is an energy-averaged LAeq over an adaptive resampling interval."),
            ("Chart 2: Diurnal Box-and-Whisker — Hourly LAeq Volatility",
             self._fig_diurnal_box_whisker(ts=ts, leq=leq),
             "Shows the statistical spread of noise levels for each hour of the day, pooled across all measurement days. The box is the IQR (25th–75th percentile); the centre line is the median. Tall boxes indicate acoustically unpredictable hours."),
            ("Chart 3: Temporal Heatmap — LAeq Intensity by Date & Hour",
             self._fig_temporal_heatmap(ts=ts, leq=leq),
             "Each cell shows the energy-averaged LAeq for a specific hour on a specific date. Green = quiet; orange/red = approaching or exceeding WHO limits. Scan vertically to identify noisiest times of day; horizontally to spot unusual days."),
            ("Chart 4: Diurnal Noise Fingerprint — 24-Hour Polar Radar",
             self._fig_diurnal_radar(ts=ts, leq=leq),
             "Polar radar showing the mean LAeq for each of the 24 clock hours. A bulge at 07:00–09:00 and 17:00–19:00 indicates commuter traffic dominance. A uniform ring indicates a continuous source."),
            ("Chart 5: Weekly Noise Profile — Day-of-Week Radar",
             self._fig_weekly_radar(ts=ts, leq=leq),
             "7-spoke radar comparing daytime (07:00–22:00) vs nighttime (22:00–07:00) LAeq for each day of the week. Shorter weekend spokes vs weekday spokes indicate traffic/commercial noise. Requires at least 7 days of data."),
        ]

        source_label = (_esc(", ".join(self.source_files)) if self.source_files
                        else _esc(os.path.basename(self.filepath)))
        device_line  = f"<div class='meta'>Device / Location: <b>{_esc(self.device_id)}</b></div>" if self.device_id else ""
        completeness_str = f"{completeness:.0f}%" if completeness is not None else "N/A"
        uptime_warn  = (" <span style='color:#b91c1c'>⚠ High data loss</span>" if (completeness or 100) < 90 else "")

        html_parts = [
            "<!DOCTYPE html>",
            "<html lang='en'>",
            "<head>",
            "  <meta charset='UTF-8'>",
            "  <meta name='viewport' content='width=device-width, initial-scale=1.0'>",
            f"  <title>Noise Analysis Report</title>",
            "  <script src='https://cdn.plot.ly/plotly-3.4.0.min.js'></script>",
            "  <style>",
            "    *{box-sizing:border-box;margin:0;padding:0}",
            "    body{font-family:Arial,sans-serif;background:#f6f8fb;color:#1f2937;font-size:14px}",
            "    .container{max-width:1200px;margin:0 auto;padding:24px}",
            "    .card{background:#fff;border-radius:12px;box-shadow:0 4px 16px rgba(15,23,42,0.08);padding:28px;margin-bottom:22px}",
            "    h1{font-size:26px;color:#1e3a5f;margin-bottom:6px}",
            "    h2{font-size:18px;color:#1e3a5f;margin-bottom:14px;padding-bottom:6px;border-bottom:2px solid #e5e7eb}",
            "    h3{font-size:14px;color:#374151;margin:16px 0 8px 0}",
            "    .meta{color:#6b7280;font-size:12px;margin-top:4px}",
            "    .summary-box{border-radius:8px;padding:16px 20px;line-height:1.7;font-size:14px}",
            "    .metrics-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:14px;margin-top:14px}",
            "    .metric-card{background:#f8fafc;border:1px solid #e5e7eb;border-radius:8px;padding:14px;text-align:center}",
            "    .metric-val{font-size:28px;font-weight:700;color:#1e3a5f}",
            "    .metric-lbl{font-size:11px;color:#6b7280;margin-top:4px}",
            "    table{width:100%;border-collapse:collapse;font-size:13px;margin-top:12px}",
            "    th{background:#1e3a5f;color:#fff;padding:9px 12px;text-align:left;font-weight:600}",
            "    td{padding:8px 12px;border-bottom:1px solid #e5e7eb;vertical-align:middle}",
            "    tr:nth-child(even) td{background:#f8fafc}",
            "    .chart-wrap{margin-top:18px}",
            "    .chart-note{font-size:12px;color:#6b7280;margin-top:8px;line-height:1.5}",
            "    .who-note{background:#fffbeb;border:1px solid #d97706;border-radius:6px;padding:14px;font-size:13px;line-height:1.6;margin-top:14px}",
            "    .who-note b{color:#92400e}",
            "    .footer{text-align:center;font-size:11px;color:#9ca3af;margin-top:30px;padding:16px}",
            "  </style>",
            "</head>",
            "<body>",
            "  <div class='container'>",

            # ── Header ──
            "    <div class='card'>",
            "      <h1>Environmental Noise Analysis Report</h1>",
            device_line,
            f"      <div class='meta'>Source file(s): {source_label}</div>",
            f"      <div class='meta'>Measurement period: {_esc(start_str)} to {_esc(end_str)} ({_esc(duration_label)})</div>",
            f"      <div class='meta'>Data completeness: {completeness_str}{uptime_warn}</div>",
            f"      <div class='meta'>Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}</div>",
            "    </div>",

            # ── Section 1: Plain-English Summary ──
            "    <div class='card'>",
            "      <h2>Section 1: Non-Technical Summary — Noise Exposure &amp; Health Assessment</h2>",
            f"      <div class='summary-box' style='background:{concern_bg};border:1px solid {concern_color};color:#1f2937'>",
            f"        {_esc(summary_text)}",
            "      </div>",
            "    </div>",

            # ── Section 2: Key Acoustic Metrics ──
            "    <div class='card'>",
            "      <h2>Section 2: Key Acoustic Metrics</h2>",
            "      <div class='metrics-grid'>",
            f"        <div class='metric-card'><div class='metric-val'>{_hfmt(laeq_v)}</div><div class='metric-lbl'>LAeq — 24-hr Energy Average (dB(A))</div></div>",
            f"        <div class='metric-card'><div class='metric-val'>{_hfmt(lden_v)}</div><div class='metric-lbl'>Lden — Day-Evening-Night Weighted (dB(A))<br><span style='font-size:10px'>WHO limit: 53 dB(A)</span></div></div>",
            f"        <div class='metric-card'><div class='metric-val'>{_hfmt(lnight_v)}</div><div class='metric-lbl'>Lnight — Nighttime Average 23:00–07:00 (dB(A))<br><span style='font-size:10px'>WHO limit: 45 dB(A)</span></div></div>",
            f"        <div class='metric-card'><div class='metric-val'>{_hfmt(laeq_day_v)}</div><div class='metric-lbl'>LAeq Day — 07:00–22:00 (dB(A))</div></div>",
            f"        <div class='metric-card'><div class='metric-val'>{_hfmt(laeq_night_v)}</div><div class='metric-lbl'>LAeq Night — 22:00–07:00 (dB(A))</div></div>",
            f"        <div class='metric-card'><div class='metric-val'>{_hfmt(float(leq.max()) if not leq.dropna().empty else None)}</div><div class='metric-lbl'>LAmax — Absolute Peak (dB(A))</div></div>",
            f"        <div class='metric-card'><div class='metric-val'>{_hfmt(float(leq.min()) if not leq.dropna().empty else None)}</div><div class='metric-lbl'>LAmin — Absolute Floor (dB(A))</div></div>",
            "      </div>",
            "    </div>",

            # ── Section 3: Regulatory Compliance ──
            "    <div class='card'>",
            "      <h2>Section 3: Regulatory &amp; Health Compliance</h2>",
            "      <p style='font-size:13px;color:#374151;margin-bottom:6px'>Standards: WHO Environmental Noise Guidelines (2018), WHO Community Noise Guidelines (1999), Maryland COMAR 26.02.03.02.</p>",
            "      <table>",
            "        <thead><tr><th>Regulatory Standard</th><th>Metric</th><th style='text-align:right'>Measured (dB(A))</th><th style='text-align:right'>Limit (dB(A))</th><th style='text-align:center'>Assessment</th></tr></thead>",
            f"        <tbody>{comp_rows_html}</tbody>",
            "      </table>",
            "      <div class='who-note'>",
            "        <b>Important note on WHO 2018 Road Traffic and Aircraft Noise rows:</b> "
            "The WHO 2018 road-traffic (Lden ≤ 53 dB(A), Lnight ≤ 45 dB(A)) and aircraft (Lden ≤ 45 dB(A), Lnight ≤ 40 dB(A)) "
            "guidelines are source-specific standards derived from studies that attributed noise exclusively to those sources. "
            "The NSTRW MK4 sensor measures total combined acoustic energy and cannot identify or separate individual noise sources. "
            "A 'NON-COMPLIANT' result here means total measured noise from all sources exceeds the WHO threshold — not that road "
            "traffic or aircraft alone is responsible. Furthermore, WHO 2018 intends these metrics to represent long-term annual "
            "average exposure; a measurement period of days or weeks is indicative only.",
            "      </div>",
            "    </div>",

            # ── Section 4: Statistical Noise Profile (Percentiles) ──
            "    <div class='card'>",
            "      <h2>Section 4: Statistical Noise Profile — Exceedance Percentiles</h2>",
            "      <table>",
            "        <thead><tr><th>Percentile</th><th style='text-align:right'>Value (dB(A))</th><th>Definition</th></tr></thead>",
            f"        <tbody>{pct_rows_html}</tbody>",
            "      </table>",
            "    </div>",
        ]

        # ── Optional custom section ──
        if self.custom_section_heading or self.custom_section_body:
            heading_text = _esc(self.custom_section_heading or "Additional Notes")
            body_lines = "".join(
                f"<p style='margin-bottom:8px;line-height:1.6'>{_esc(ln)}</p>"
                for ln in (self.custom_section_body or '').splitlines()
                if ln.strip()
            )
            html_parts += [
                "    <div class='card'>",
                f"      <h2>Additional Notes: {heading_text}</h2>",
                body_lines,
                "    </div>",
            ]

        # ── Section 5: Daily Summary Matrix ──
        if daily_rows_html:
            html_parts += [
                "    <div class='card'>",
                "      <h2>Section 5: Daily Summary Matrix</h2>",
                "      <table>",
                "        <thead><tr><th>Date</th><th style='text-align:right'>24-hr LAeq (dB(A))</th><th style='text-align:right'>Daytime 07–22 (dB(A))</th><th style='text-align:right'>Nighttime 22–07 (dB(A))</th><th style='text-align:right'>Daily Lden (dB(A))</th></tr></thead>",
                f"        <tbody>{daily_rows_html}</tbody>",
                "      </table>",
                "    </div>",
            ]

        # ── Section 6: Advanced Visualisations ──
        html_parts += [
            "    <div class='card'>",
            "      <h2>Section 6: Advanced Visualisations</h2>",
            "      <p style='font-size:13px;color:#374151'>All charts are computed directly from the uploaded dataset using logarithmic energy-averaging (LAeq). WHO guideline lines are shown where applicable.</p>",
        ]
        for title, fig, note in charts_specs:
            html_parts.append(f"      <div class='chart-wrap'><h3>{_esc(title)}</h3>")
            if fig is None:
                html_parts.append("        <p style='color:#6b7280;font-style:italic'>Chart unavailable — insufficient data.</p>")
            else:
                html_parts.append(fig.to_html(full_html=False, include_plotlyjs=False))
            html_parts.append(f"        <div class='chart-note'>{_esc(note)}</div></div>")
        html_parts.append("    </div>")

        # ── Footer ──
        html_parts += [
            "    <div class='footer'>",
            "      Environmental Noise Analysis Platform | "
            "Developed by Chandra Prakash Choudhary | "
            "PI: Dr. Ana María Rule, Associate Professor, Johns Hopkins University | "
            f"Generated {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            "    </div>",
            "  </div>",
            "</body>",
            "</html>",
        ]

        with open(report_path, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(html_parts))

        return report_path

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
        if ts_col and ts_col in self.df.columns:
            ts = pd.to_datetime(self.df[ts_col], errors="coerce", dayfirst=True, cache=True)
            if ts.notna().sum() > 0:
                return ts
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

        expected_s = max(0.0, (ts_valid.max() - ts_valid.min()).total_seconds()) if not ts_valid.empty else 0
        completeness = 100.0 * len(self.df) / max(1, expected_s) if expected_s > 0 else None

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

        summary_box_style = ParagraphStyle(
            'SummaryBox',
            parent=styles['BodyText'],
            fontSize=9,
            leading=13,
            backColor=colors.HexColor('#EFF6FF'),
            borderPadding=(8, 10, 8, 10),
            borderColor=colors.HexColor('#3D5A80'),
            borderWidth=1,
            borderRadius=4,
        )
        story.append(Paragraph("<b>Non-Technical Summary: Noise Exposure &amp; Health Assessment</b>", styles['h2']))
        story.append(Paragraph(escape(summary_text), summary_box_style))
        story.append(Spacer(1, 0.12 * inch))

    # ============================================================
    # SECTION 2: DATA QUALITY & COMPLETENESS
    # ============================================================

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
        expected_samples = int(expected_seconds) + 1  # Assume 1 Hz sampling
        actual_samples = len(self.df)
        uptime_pct = (100.0 * actual_samples / max(1, expected_samples))

        story.append(Paragraph(f"<b>Measurement Span:</b> {escape(start.strftime('%Y-%m-%d %H:%M:%S'))} to {escape(end.strftime('%Y-%m-%d %H:%M:%S'))}", styles['BodyText']))
        story.append(Paragraph(f"<b>Expected Samples (1 Hz polling):</b> {expected_samples:,}", styles['BodyText']))
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
            if r['status'] == 'PASS':
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
            "<b>Important note on WHO 2018 Road Traffic and Aircraft Noise rows:</b> "
            "The WHO 2018 guidelines for road traffic (Lden ≤ 53 dB(A), Lnight ≤ 45 dB(A)) and aircraft noise "
            "(Lden ≤ 45 dB(A), Lnight ≤ 40 dB(A)) are source-specific standards — they were derived from "
            "epidemiological studies that attributed noise exclusively to road vehicles or aircraft. "
            "The NSTRW MK4 sensor measures total combined acoustic energy from all sources in the environment; "
            "it cannot identify or separate individual sources. Therefore, if these rows show NON-COMPLIANT, it "
            "means the total measured noise exceeds the WHO threshold — not that road traffic or aircraft noise "
            "alone is responsible. Additionally, WHO 2018 intends these metrics to represent long-term annual "
            "average exposure; a measurement period of days or weeks is indicative only. "
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
        """Convert Plotly figure to ReportLab Image.

        Tries kaleido/orca first (high-fidelity), then falls back to a
        matplotlib re-render so charts always appear in the PDF even on
        servers where Chromium/kaleido is unavailable (e.g. Render free tier).
        """
        if fig is None:
            return None

        img_bytes = self._try_plotly_export(fig)
        if img_bytes is None:
            img_bytes = self._matplotlib_fallback(fig, width_inch, height_inch)
        if img_bytes is None:
            return None

        try:
            img = Image(io.BytesIO(img_bytes))
            aspect = img.imageHeight / img.imageWidth if img.imageWidth > 0 else 1
            img.drawWidth = width_inch * inch
            img.drawHeight = (width_inch * inch) * aspect
            if img.drawHeight > height_inch * inch:
                img.drawHeight = height_inch * inch
                img.drawWidth = (height_inch * inch) / aspect
            return img
        except Exception as e:
            print(f"[Report] ReportLab image wrap failed: {type(e).__name__}: {e}")
            return None

    def _try_plotly_export(self, fig):
        """Attempt kaleido → orca → plotly.io. Returns PNG bytes or None."""
        scale = 1.25 if len(self.df) > 500_000 else 2
        for engine in ("kaleido", "orca"):
            try:
                return fig.to_image(format="png", scale=scale, engine=engine)
            except Exception as e:
                print(f"[Report] {engine} engine failed: {type(e).__name__}: {e}")
        try:
            import plotly.io as pio
            return pio.to_image(fig, format="png")
        except Exception as e:
            print(f"[Report] plotly.io fallback failed: {type(e).__name__}: {e}")
        return None

    def _matplotlib_fallback(self, fig, width_inch, height_inch):
        """Render a Plotly figure to PNG using matplotlib (no browser required).

        Extracts trace data directly from the Plotly figure object and draws
        equivalent charts with matplotlib/Agg. Handles scatter/line, box,
        heatmap, and polar (scatterpolar) trace types.
        """
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.ticker as mticker

        BG   = '#f8f9fa'
        PALETTE = ['#3D5A80', '#EE6C4D', '#98C1D9', '#E0FBFC', '#293241',
                   '#F4A261', '#2A9D8F', '#E9C46A', '#264653', '#A8DADC']

        try:
            layout = fig.layout
            traces = fig.data
            if not traces:
                return None

            # Detect chart family
            types = {t.type for t in traces}
            is_heatmap = 'heatmap' in types
            is_polar   = 'scatterpolar' in types or 'barpolar' in types

            if is_polar:
                mfig, ax = plt.subplots(figsize=(width_inch, height_inch),
                                        subplot_kw={'projection': 'polar'})
            else:
                mfig, ax = plt.subplots(figsize=(width_inch, height_inch))

            mfig.patch.set_facecolor(BG)
            if not is_polar:
                ax.set_facecolor(BG)

            # ── title ─────────────────────────────────────────────────
            title_obj = getattr(layout, 'title', None)
            title_text = ''
            if title_obj and hasattr(title_obj, 'text') and title_obj.text:
                title_text = title_obj.text
            if title_text:
                mfig.suptitle(title_text, fontsize=10, fontweight='bold',
                              color='#3D5A80', y=1.01)

            # ── heatmap ───────────────────────────────────────────────
            if is_heatmap:
                tr = next(t for t in traces if t.type == 'heatmap')
                z = np.array([[v if v is not None else np.nan for v in row]
                              for row in (tr.z or [])])
                if z.size == 0:
                    plt.close(mfig); return None
                im = ax.imshow(z, aspect='auto', cmap='RdYlGn_r', origin='upper')
                mfig.colorbar(im, ax=ax, label='dB(A)', pad=0.02)
                x_labels = list(tr.x or [])
                y_labels = list(tr.y or [])
                if x_labels:
                    step = max(1, len(x_labels) // 10)
                    ax.set_xticks(range(0, len(x_labels), step))
                    ax.set_xticklabels([str(x_labels[i]) for i in range(0, len(x_labels), step)],
                                       rotation=45, ha='right', fontsize=7)
                if y_labels:
                    ax.set_yticks(range(len(y_labels)))
                    ax.set_yticklabels([str(v) for v in y_labels], fontsize=7)

            # ── polar / radar ─────────────────────────────────────────
            elif is_polar:
                for i, tr in enumerate(traces):
                    color = PALETTE[i % len(PALETTE)]
                    theta_raw = list(getattr(tr, 'theta', None) or [])
                    r_raw     = list(getattr(tr, 'r',     None) or [])
                    if not theta_raw or not r_raw:
                        continue
                    # Convert hour labels to radians
                    try:
                        theta_rad = [float(t) / 24 * 2 * np.pi for t in theta_raw]
                    except (TypeError, ValueError):
                        theta_rad = np.linspace(0, 2 * np.pi, len(theta_raw), endpoint=False).tolist()
                    r_vals = []
                    for v in r_raw:
                        try: r_vals.append(float(v))
                        except (TypeError, ValueError): r_vals.append(0.0)
                    theta_rad.append(theta_rad[0])
                    r_vals.append(r_vals[0])
                    label = getattr(tr, 'name', None) or f'Series {i+1}'
                    ax.plot(theta_rad, r_vals, color=color, linewidth=1.8, label=label)
                    ax.fill(theta_rad, r_vals, color=color, alpha=0.08)
                ax.set_theta_direction(-1)
                ax.set_theta_offset(np.pi / 2)
                hour_ticks = np.linspace(0, 2 * np.pi, 24, endpoint=False)
                ax.set_xticks(hour_ticks)
                ax.set_xticklabels([f'{h:02d}h' for h in range(24)], fontsize=6)
                ax.grid(True, alpha=0.3)
                handles, labels_ = ax.get_legend_handles_labels()
                if handles:
                    ax.legend(fontsize=7, loc='upper right',
                              bbox_to_anchor=(1.35, 1.1), framealpha=0.7)

            # ── scatter / line / bar / box ────────────────────────────
            else:
                for i, tr in enumerate(traces):
                    color = PALETTE[i % len(PALETTE)]
                    label = getattr(tr, 'name', None) or f'Series {i+1}'

                    if tr.type in ('scatter', 'scattergl'):
                        x_raw = list(tr.x or [])
                        y_raw = [v if v is not None else np.nan for v in (tr.y or [])]
                        if not x_raw or not y_raw:
                            continue
                        mode  = getattr(tr, 'mode', 'lines') or 'lines'
                        fill  = getattr(tr, 'fill', None)
                        line_ = getattr(tr, 'line', None)
                        dash  = getattr(line_, 'dash', 'solid') or 'solid'
                        lw    = min(float(getattr(line_, 'width', 1.5) or 1.5), 3.0)
                        ls    = {'dot': ':', 'dash': '--', 'dashdot': '-.', 'solid': '-'}.get(dash, '-')
                        # Detect fill/band traces (envelope) — draw as shaded area
                        if fill and fill.startswith('to'):
                            ax.fill_between(x_raw, y_raw, alpha=0.12, color=color)
                        else:
                            if 'lines' in mode:
                                ax.plot(x_raw, y_raw, color=color, linewidth=lw,
                                        linestyle=ls, label=label, alpha=0.9)
                            if 'markers' in mode and 'lines' not in mode:
                                ax.scatter(x_raw, y_raw, color=color, s=6, label=label)

                    elif tr.type == 'bar':
                        x_raw = list(tr.x or [])
                        y_raw = [v if v is not None else 0 for v in (tr.y or [])]
                        if x_raw and y_raw:
                            ax.bar(x_raw, y_raw, color=color, alpha=0.8, label=label)
                            plt.xticks(rotation=45, ha='right', fontsize=7)

                    elif tr.type in ('box', 'violin'):
                        y_all = list(tr.y or [])
                        x_cat = list(tr.x or [])
                        y_all = [v for v in y_all if v is not None]
                        if not y_all:
                            continue
                        if x_cat:
                            x_cat = [v for v in x_cat if v is not None]
                            groups: dict = {}
                            for xi, yi in zip(x_cat, y_all):
                                groups.setdefault(str(xi), []).append(float(yi))
                            positions = sorted(groups.keys(),
                                               key=lambda k: int(k) if k.isdigit() else k)
                            arrays   = [groups[p] for p in positions]
                            bp = ax.boxplot(arrays, labels=positions, patch_artist=True,
                                            widths=0.6, medianprops=dict(color='#EE6C4D', linewidth=2))
                            for patch in bp['boxes']:
                                patch.set_facecolor(color)
                                patch.set_alpha(0.55)
                            plt.xticks(rotation=45, ha='right', fontsize=7)
                        else:
                            bp = ax.boxplot([y_all], labels=[label], patch_artist=True,
                                            medianprops=dict(color='#EE6C4D', linewidth=2))
                            bp['boxes'][0].set_facecolor(color)
                            bp['boxes'][0].set_alpha(0.55)

                # ── axis labels ───────────────────────────────────────
                xaxis = getattr(layout, 'xaxis', None)
                yaxis = getattr(layout, 'yaxis', None)
                xlabel = ''
                ylabel = ''
                if xaxis and getattr(xaxis, 'title', None):
                    xlabel = getattr(xaxis.title, 'text', '') or ''
                if yaxis and getattr(yaxis, 'title', None):
                    ylabel = getattr(yaxis.title, 'text', '') or ''
                if xlabel:
                    ax.set_xlabel(xlabel, fontsize=9)
                if ylabel:
                    ax.set_ylabel(ylabel, fontsize=9)

                ax.tick_params(labelsize=8)
                ax.grid(True, alpha=0.25, linewidth=0.5)
                # Auto-rotate x-tick labels if they are dates or long strings
                try:
                    mfig.autofmt_xdate(rotation=30, ha='right')
                except Exception:
                    pass

                handles, labels_ = ax.get_legend_handles_labels()
                if handles and len(handles) <= 8:
                    ax.legend(fontsize=7, loc='best', framealpha=0.7)

            plt.tight_layout(pad=0.8)
            buf = io.BytesIO()
            mfig.savefig(buf, format='png', dpi=150, bbox_inches='tight',
                         facecolor=BG)
            buf.seek(0)
            img_bytes = buf.read()
            plt.close(mfig)
            print(f"[Report] matplotlib fallback succeeded ({len(img_bytes)//1024} KB)")
            return img_bytes

        except Exception as e:
            import traceback
            print(f"[Report] matplotlib fallback failed: {type(e).__name__}: {e}")
            traceback.print_exc()
            try:
                plt.close('all')
            except Exception:
                pass
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
