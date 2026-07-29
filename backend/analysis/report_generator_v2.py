import pandas as pd
import numpy as np
from datetime import datetime
import math
import os
import re
import json
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from analysis.noise_analyzer import NoiseAnalyzer
from analysis.iso_epa_standards import StandardsAnalyzer
from analysis.standards_reference import who_2018_environmental_noise_guideline_levels
from analysis.chart_generator import AdvancedChartGenerator
from analysis.acoustics import (compute_ldn_lden, energetic_mean_db,
                                exceedance_levels_db, energy_concentration)
from analysis.gap_detector import detect_gaps, gap_report_to_dict, data_completeness_pct, _modal_interval_seconds
from analysis.compliance_matrix import evaluate_compliance
import io
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak, Table, TableStyle, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from xml.sax.saxutils import escape

# ── De-identification ────────────────────────────────────────────────────────
# Reports from this platform are distributed to residents and regulators, so
# they fall under human-subjects protection: no resident name, address, or other
# personal identifier may appear anywhere in them.
#
# The realistic leak is not the analysis — it is the FILENAME. Source files
# routinely carry the participant's name (this project's own raw data sits in
# directories named after the participant), and filenames were rendered verbatim in
# the dataset-composition line, the merge list, and the provenance block.
#
# Identifiers are therefore stripped by default and files referred to positionally.
# The SHA-256 of each file is retained, which preserves chain of custody — a
# reader can still verify byte-for-byte which file produced the report — without
# disclosing who it belongs to.

# Instrument provenance statement.
#
# Each logger is factory-calibrated and ships with its own individual certificate,
# which the study team retains. The report states this positively and offers the
# certificates on request rather than listing them: with a fleet of units, what a
# reader needs is to know WHICH device produced the data so the right certificate
# can be requested — hence the device identifier printed alongside. The
# manufacturer's IEC 61672-1 position is stated rather than glossed, because
# "meets the accuracy requirements of" is not the same as certified Class 1.
DEFAULT_INSTRUMENT_NOTE = (
    "Measurements were made with a Convergence Instruments NSRT_W_mk4 sound level "
    "logger, A-weighted. Each unit is factory-calibrated and supplied with its own "
    "individual manufacturer's certificate of calibration. Certificates are retained "
    "by the study team and are available on request; the device identifier above "
    "indicates which unit produced this dataset. The manufacturer states that these "
    "units meet the accuracy requirements of IEC 61672-1 but does not certify them as "
    "Class 1 or Class 2 instruments. Where a formal Class 1 determination is required, "
    "a certified meter with documented field calibration before and after the survey "
    "should be used."
)

# Labels that are safe to print: study codes, home letters, device serials.
_SAFE_LABEL_RE = re.compile(
    r'^(?:'
    r'home\s*[A-Z]|'                      # Home A, Home D
    r'(?:conv|pair|site|loc|dev|unit)\s*[-_]?\d{1,4}|'   # CONV001, SITE-12
    r'[A-Z]{1,6}[-_]?\d{1,6}|'            # MON-4471, SLM12
    r'\d{1,6}'                            # bare numeric id
    r')$',
    re.IGNORECASE,
)


def is_safe_label(value: str) -> bool:
    """True when ``value`` is a study code rather than a personal identifier."""
    v = str(value or '').strip()
    return bool(v) and bool(_SAFE_LABEL_RE.match(v))


def deidentify_label(value: str, fallback: str) -> tuple[str, bool]:
    """Return ``(label, was_redacted)`` for a user-supplied identifier.

    Study codes pass through unchanged; anything else is replaced by ``fallback``.
    """
    return (str(value).strip(), False) if is_safe_label(value) else (fallback, True)


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
                 custom_section_heading: str = '', custom_section_body: str = '', environment: str = 'outdoor',
                 deidentify: bool = True, instrument_note: str | None = None):
        """
        Initialize report generator with ONLY the uploaded data.
        NO external CSV file loading - all summaries computed from df.

        environment : 'outdoor' (default) or 'indoor' — controls whether the WHO
        indoor bedroom guidelines are evaluated in the compliance matrix.
        """
        self.df = df.copy()
        self.filepath = filepath
        self.deidentify = bool(deidentify)
        self.instrument_note = (instrument_note or DEFAULT_INSTRUMENT_NOTE).strip()

        # Strip personal identifiers ONCE, here, so every render path (PDF, HTML,
        # DOCX, provenance) is covered and no future section can reintroduce a
        # raw filename by reading the attribute directly.
        raw_device_id = str(device_id or '').strip()
        raw_sources = [str(f) for f in (source_files or [])]
        self.redactions: list[str] = []

        if self.deidentify:
            if raw_device_id:
                label, redacted = deidentify_label(raw_device_id, 'Monitoring location (identifier withheld)')
                self.device_id = label
                if redacted:
                    self.redactions.append('device/location identifier')
            else:
                self.device_id = ''

            self.source_files = []
            for i, name in enumerate(raw_sources, start=1):
                stem = os.path.splitext(os.path.basename(name))[0]
                stem = re.sub(r'^\d{8}_\d{6}_', '', stem)   # strip upload prefix
                self.source_files.append(stem if is_safe_label(stem) else f'Source file {i}')
            if any(s.startswith('Source file ') for s in self.source_files):
                self.redactions.append('source file names')
        else:
            self.device_id = raw_device_id
            self.source_files = raw_sources
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
        n_gaps: int = 0,
        total_gap_hours: float | None = None,
        environment: str = 'outdoor',
        truncation_warning: bool = False,
        timestamps_unusable: bool = False,
        lamax: float | None = None,
        logging_interval_s: float | None = None,
        energy_dominance: dict | None = None,
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

        # Describe the LEQ averaging interval in words, so a peak attributed to
        # the LEQ stream says what it is the average of.
        _int_s = _v(logging_interval_s)
        if _int_s is None or _int_s <= 0:
            interval_word = "logged-interval"
        elif _int_s < 1:
            interval_word = f"{_int_s:.2f}-second"
        elif _int_s < 60:
            interval_word = f"{_int_s:.0f}-second"
        else:
            interval_word = f"{_int_s / 60.0:.0f}-minute"

        laeq_v = _v(laeq)
        if laeq_v is None:
            return (
                "A plain-English summary could not be generated because the average noise level "
                "(LAeq) could not be computed. Please check that the dataset contains valid "
                "numeric measurements."
            )

        # ── 1. Opening paragraph ─────────────────────────────────────────────
        # When the timeline is not real, state that plainly instead of naming
        # dates that were invented by the parser.
        if timestamps_unusable:
            para_ts = (
                "The date and time information in this file could not be read, so the "
                "measurement period, the day-by-day breakdown, and every time-dependent "
                "result (Lden, Ldn, Lnight, the day/night split, the diurnal profile and the "
                "heatmap) have been withheld rather than estimated. The overall average level "
                "and the statistical percentiles below remain valid, because they do not "
                "depend on when each sample was taken. Re-export the file with a full "
                "'YYYY-MM-DD HH:MM:SS' timestamp column to obtain the full assessment."
            )
            level_only = (
                f"The whole-record energy-average level (LAeq) was {_f(laeq_v)} dB(A)."
            )
            parts_u = [para_ts, level_only]
            if _v(laeq_max) is not None:
                parts_u.append(f"The highest single recorded level was {_f(_v(laeq_max))} dB(A).")
            return "\n\n".join(parts_u)

        date_ctx = ""
        if start_date and end_date:
            date_ctx = f" from {start_date} to {end_date}"
        elif duration_label:
            date_ctx = f" over {duration_label}"

        day_word = (f"{n_days} day{'s' if n_days != 1 else ''}" if n_days > 0
                    else duration_label or "the measurement period")

        # Completeness must never round UP to "100%". A record that is 99.6%
        # complete contains a real outage (1 hour, in one dataset here); printing
        # "100%" erases it and, combined with the word "continuous", asserts an
        # unbroken record that does not exist.
        completeness_note = ""
        is_continuous = True
        if data_completeness_pct is not None:
            try:
                cp = float(data_completeness_pct)
                # Floor to 1 dp so 99.96 -> "99.9%", never "100%".
                cp_shown = math.floor(cp * 10.0) / 10.0
                if cp_shown >= 100.0:
                    completeness_note = " The record is complete, with no detected gaps."
                else:
                    is_continuous = False
                    severity = ("Averages may under- or over-estimate true exposure"
                                if cp_shown < 90.0 else
                                "The affected periods are excluded from all averages")
                    completeness_note = (
                        f" Data completeness was {cp_shown:.1f}%, so the record contains gaps. "
                        f"{severity}."
                    )
            except Exception:
                pass

        if n_gaps:
            is_continuous = False
            _ng = int(n_gaps)
            gap_detail = (f" {_ng} interruption was detected" if _ng == 1
                          else f" {_ng} interruptions were detected")
            if total_gap_hours:
                gap_detail += f", totalling {float(total_gap_hours):.1f} hours"
            completeness_note += gap_detail + "."

        # "continuous" is a factual claim about the record, not a figure of
        # speech — only make it when the data actually support it.
        monitoring_word = "continuous " if is_continuous else ""
        placement = "indoor" if str(environment or "outdoor").strip().lower() == "indoor" else "outdoor"

        para1 = (
            f"This dataset covers {day_word} of {monitoring_word}{placement} noise monitoring{date_ctx}."
            f"{completeness_note}"
        )
        if truncation_warning:
            para1 += (
                " Note: the source file appears to have been truncated at the spreadsheet row "
                "limit, so the true monitoring period is longer than the period reported here."
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

        # Label the averaging period honestly: this is the energy average over the
        # WHOLE record, which is rarely 24 hours.
        period_label = (f"{n_days}-day" if n_days and n_days != 1 else
                        ("24-hour" if n_days == 1 else "whole-record"))
        level_sentence = (
            f"The {period_label} energy-average level (LAeq) was {_f(laeq_v)} dB(A), "
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
                # A sound level meter records level, not source. Statements about
                # WHAT caused the noise ("traffic-related", "a nocturnal source")
                # are not supported by the measurement and must not appear in a
                # report intended for public or evidentiary use. Describe the
                # measured pattern only.
                base = (f" Daytime levels (07:00–22:00) averaged {_f(dv)} dB(A) and nighttime "
                        f"levels (22:00–07:00) averaged {_f(nv)} dB(A).")
                if diff > 5:
                    day_night_sentence = base + (
                        f" Daytime exceeded nighttime by {_f(abs(diff))} dB, indicating a "
                        f"pronounced diurnal pattern."
                    )
                elif diff < -3:
                    day_night_sentence = base + (
                        f" Nighttime exceeded daytime by {_f(abs(diff))} dB, an inverted diurnal "
                        f"pattern. Identifying the contributing source requires observations "
                        f"beyond sound level data alone."
                    )
                else:
                    day_night_sentence = base + (
                        f" The two differ by {_f(abs(diff))} dB, indicating a broadly steady "
                        f"level across the day-night cycle."
                    )
        except Exception:
            pass

        # Name the stream the peak came from. ``laeq_max`` is the maximum of the
        # LEQ series — itself an average over each logging interval — so calling
        # it "the highest single recorded level" understates the true peak and
        # mislabels it. On one 126-day record the LEQ max was 104.0 dB while the
        # instrument's L-Max stream reached 105.3 dB.
        peak_sentence = ""
        lamax_v = _v(lamax)
        if lamax_v is not None:
            peak_sentence = (f" The highest single sound level recorded (L-Max) was "
                             f"{_f(lamax_v)} dB(A).")
        elif _v(laeq_max) is not None:
            peak_sentence = (f" The highest {interval_word} average level recorded (LEQ) was "
                             f"{_f(_v(laeq_max))} dB(A). Instantaneous peaks within those intervals "
                             f"were higher; an L-Max stream is required to report them.")

        # Disclose when the average rests on a handful of samples. Without this a
        # single unverified reading can carry the whole verdict silently.
        dominance_sentence = ""
        if energy_dominance and energy_dominance.get('dominated'):
            top1 = energy_dominance.get('top1_energy_pct') or 0.0
            excl = energy_dominance.get('laeq_excluding_top01pct')
            n_top = energy_dominance.get('n_top01pct') or 0
            n_all = energy_dominance.get('n') or 0
            bits = []
            if top1 >= 10.0:
                bits.append(f"the single loudest sample alone accounts for {top1:.0f}% of the "
                            f"total measured sound energy")
            if excl is not None and n_all:
                bits.append(f"excluding the loudest {n_top:,} of {n_all:,} samples, the average "
                            f"falls to {_f(excl)} dB(A)")
            detail = "; ".join(bits)
            dominance_sentence = (
                f" This average is driven by a small number of very loud samples: {detail}. "
                f"Energy averaging is dominated by the loudest events, so a brief incident — or a "
                f"single spurious reading from a knock or from clipping — moves it substantially. "
                f"The levels above are reported as measured; before relying on them, confirm from "
                f"the raw record that the loudest events are genuine acoustic events."
            )

        para2 = level_sentence + day_night_sentence + peak_sentence + dominance_sentence

        # ── 3. Variability paragraph ─────────────────────────────────────────
        para3 = ""
        try:
            l10_v = _v(l10)
            l90_v = _v(l90)
            if l10_v is not None and l90_v is not None:
                spread = l10_v - l90_v
                # Describe the measured spread; do not name sources the meter
                # cannot identify (no "passing vehicles", no "heavy machinery").
                if spread > 20:
                    para3 = (
                        f"The noise environment was highly variable. The level exceeded 10% of the "
                        f"time (L10 = {_f(l10_v)} dB(A)) was {_f(spread)} dB above the residual "
                        f"background level (L90 = {_f(l90_v)} dB(A)), indicating frequent loud "
                        f"transient events above a much quieter baseline."
                    )
                elif spread > 12:
                    para3 = (
                        f"The noise environment showed moderate variability "
                        f"(L10 = {_f(l10_v)} dB(A), L90 = {_f(l90_v)} dB(A), spread = {_f(spread)} dB), "
                        f"indicating intermittent events above a steady residual background level."
                    )
                else:
                    para3 = (
                        f"The noise environment was relatively stable, with an L10-to-L90 spread "
                        f"of only {_f(spread)} dB (L10 = {_f(l10_v)} dB(A), L90 = {_f(l90_v)} dB(A)), "
                        f"indicating a largely steady level with few loud transient events."
                    )
        except Exception:
            pass

        # ── 4. WHO compliance bullet points ─────────────────────────────────
        WHO_LDEN_LIMIT   = 53.0   # WHO 2018, Table 1 (road traffic, Lden)
        WHO_LNIGHT_LIMIT = 45.0   # WHO 2018, Table 1 (road traffic, Lnight)
        # 40 dB Lnight,outside is the LOAEL established in the WHO Night Noise
        # Guidelines for Europe (2009), which WHO 2018 carries forward.
        WHO_LOAEL_NIGHT  = 40.0

        # Ordered severity so the worst finding wins, rather than the last one.
        _RANK = {"LOW": 0, "MODERATE": 1, "MODERATE-HIGH": 2, "HIGH": 3, "SERIOUS": 4}

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
                # Graduated by how far the guideline is exceeded. Previously every
                # tier here was rewritten to HIGH further down, so a 0.1 dB and a
                # 15 dB exceedance produced an identical verdict.
                if excess >= 10:
                    concern_level = "SERIOUS"
                elif excess >= 5:
                    concern_level = "HIGH"
                else:
                    concern_level = "MODERATE-HIGH"
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
                    f"Nighttime level (Lnight, 23:00–07:00): {_f(lnight_v)} dB(A). "
                    f"Exceeds the WHO 2018 sleep-protection limit of {WHO_LNIGHT_LIMIT} dB(A) "
                    f"by {_f(excess)} dB."
                )
                night_tier = ("SERIOUS" if excess >= 10 else
                              "HIGH" if excess >= 5 else "MODERATE-HIGH")
                if _RANK.get(night_tier, 0) > _RANK.get(concern_level, 0):
                    concern_level = night_tier
            elif lnight_v > WHO_LOAEL_NIGHT:
                bullets.append(
                    f"Nighttime level (Lnight, 23:00–07:00): {_f(lnight_v)} dB(A). "
                    f"Within the WHO 2018 limit of {WHO_LNIGHT_LIMIT} dB(A) but above the "
                    f"lowest-observed-adverse-effect level (LOAEL) of {WHO_LOAEL_NIGHT} dB(A), "
                    f"at which initial sleep disturbance effects begin."
                )
                if _RANK.get("MODERATE", 0) > _RANK.get(concern_level, 0):
                    concern_level = "MODERATE"
            else:
                bullets.append(
                    f"Nighttime level (Lnight, 23:00–07:00): {_f(lnight_v)} dB(A). "
                    f"Below the {WHO_LOAEL_NIGHT} dB(A) lowest-observed-adverse-effect level "
                    f"(WHO Night Noise Guidelines for Europe, 2009), the level below which WHO "
                    f"does not identify adverse sleep effects in the general population."
                )

        if not bullets:
            # No Lden/Lnight available. LAeq is NOT comparable to the WHO limits:
            # Lden adds +5 dB to evening and +10 dB to night samples, so Lden is
            # always >= LAeq — often by 3-6 dB on these datasets. Declaring a
            # record "below the WHO Lden threshold" on the strength of its LAeq
            # therefore produces false passes. State the limitation instead of
            # rendering a verdict that the available metric cannot support.
            note = ("Lden and Lnight could not be computed for this dataset because usable "
                    "timestamps were unavailable. WHO 2018 guidelines are defined on Lden and "
                    "Lnight, which apply +5 dB (evening) and +10 dB (night) penalties, so they "
                    "cannot be inferred from LAeq alone and no compliance verdict is issued here.")
            if laeq_v > 65:
                concern_level = "HIGH"
                bullets.append(
                    f"Whole-record average level (LAeq): {_f(laeq_v)} dB(A). This is high enough "
                    f"that a WHO guideline exceedance is likely once Lden/Lnight are available. {note}"
                )
            elif laeq_v > WHO_LDEN_LIMIT:
                concern_level = "MODERATE"
                bullets.append(
                    f"Whole-record average level (LAeq): {_f(laeq_v)} dB(A). This already exceeds "
                    f"the {WHO_LDEN_LIMIT} dB(A) Lden guideline value before any evening or night "
                    f"penalty is applied, so an exceedance is likely. {note}"
                )
            else:
                concern_level = "MODERATE"
                bullets.append(
                    f"Whole-record average level (LAeq): {_f(laeq_v)} dB(A). {note} "
                    f"Re-export the file with a full date and time column to obtain a verdict."
                )

        bullet_lines = "\n".join(f"  • {b}" for b in bullets)
        para4 = f"WHO 2018 Health Guideline Compliance:\n{bullet_lines}"

        # ── 5. Concern-level paragraph ───────────────────────────────────────
        concern_map = {
            "LOW": (
                "The acoustic environment is generally within WHO health-based guidelines. "
                "No immediate action is indicated, but periodic re-monitoring is advisable."
            ),
            "MODERATE": (
                "Noise levels are within WHO guidelines but above the lowest level at which sleep "
                "effects are observed. Continued monitoring is recommended, particularly for "
                "sensitive occupants such as children or elderly residents."
            ),
            # A guideline exceedance of less than 5 dB. This tier existed in the
            # severity ranking but had no text, so it fell through to the
            # "within WHO guidelines" wording above — directly contradicting the
            # bullets immediately preceding it, which read "Exceeds ... by 2.0 dB".
            "MODERATE-HIGH": (
                "WHO 2018 health-based guidelines are exceeded, by less than 5 dB. Exceedances of "
                "this size are close to the 1-3 dB combined measurement uncertainty that ISO 1996-2 "
                "associates with environmental noise measurement, so the margin should not be read "
                "as precise. Continued monitoring is recommended, and a certified acoustic "
                "assessment would establish the exceedance more firmly."
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
        
        # Sections 4-6 and 8 are entirely date/time-based. With a synthetic index
        # they would tabulate and plot dates that do not exist — the substituted
        # index starts at datetime.now(), so a March recording was printed as a
        # late-July one. Withhold them rather than caveat them.
        time_based_ok = not getattr(self, 'timestamps_synthetic', False)

        if time_based_ok:
            # Section 4: Single-Event Sleep Disturbance
            self._add_section_4_sleep_disturbance(story, styles, ts=ts, lmax_col=lmax_col)
            story.append(Spacer(1, 0.2 * inch))

        # Optional custom section (user-provided notes) — inserted after Section 4
        if self.custom_section_heading or self.custom_section_body:
            self._add_custom_section(story, styles)
            story.append(Spacer(1, 0.2 * inch))

        if time_based_ok:
            # Section 5: Daily Summary Matrix
            self._add_section_5_daily_matrix(story, styles)
            story.append(PageBreak())

            # Section 6: Diurnal Hourly Profile
            self._add_section_6_hourly_profile(story, styles, ts=ts)
            story.append(Spacer(1, 0.2 * inch))
        else:
            self._add_withheld_time_sections_notice(story, styles)
            story.append(Spacer(1, 0.2 * inch))

        # Section 7: Statistical Profile — level distribution only, no time
        # dependence, so it remains valid and is always included.
        self._add_section_7_percentiles(story, styles, leq_col=leq_col)
        story.append(Spacer(1, 0.2 * inch))

        if time_based_ok:
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
        # True instantaneous peak comes from the L-Max stream. peak_v is the
        # loudest LEQ *interval average*, which is always lower — reporting it
        # as the "loudest single moment" contradicted the summary above, which
        # correctly quotes L-Max (77.8 vs 84.7 dB on one record).
        lamax_v  = self._lamax_value(lmax_col)
        exc = exceedance_levels_db(leq_clean.to_numpy()) or {} if not leq_clean.empty else {}
        l90_v = exc.get('L90')

        # Participant-friendly headline numbers.
        #
        # The previous headline card showed the share of individual one-second
        # samples at or below 53 dB and labelled it "Time within the health
        # guideline". That is a category error with a misleading direction: 53 dB
        # is the WHO 2018 **Lden** guideline — a duration-weighted annual average
        # carrying +5 dB evening and +10 dB night penalties — so it cannot be
        # evaluated against instantaneous samples. On a home whose Lden of 58.0 dB
        # exceeds the guideline by 5 dB, that card read "73% within the health
        # guideline", telling a resident they were largely compliant when they
        # were not.
        #
        # The honest headline is the guideline comparison itself.
        WHO_LDEN_GUIDELINE = 53.0   # WHO 2018 road-traffic Lden guideline
        guideline_excess = (float(lden_v) - WHO_LDEN_GUIDELINE) if lden_v is not None else None

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
            environment=getattr(self, 'environment', 'outdoor'),
            truncation_warning=bool(getattr(self.df, 'attrs', {}).get('truncated_at_row_limit')),
            timestamps_unusable=bool(getattr(self, 'timestamps_synthetic', False)),
            lamax=self._lamax_value(lmax_col),
            logging_interval_s=self._logging_interval_s(ts),
            energy_dominance=energy_concentration(leq_clean),
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
        fig_compare = self._fig_compare_to_references(laeq_v, lden=lden_v)
        fig_daily   = self._fig_daily_simple()
        fig_typical = self._fig_typical_day()

        # ── Plain-language guidance ──
        health_points, action_points = self._participant_guidance(lden_v, lnight_v, laeq_v)

        # ── Plain within-guideline verdict ──
        # Only Lden may be compared against the 53 dB guideline. Falling back to
        # LAeq produces false passes: Lden applies +5 dB to evening and +10 dB to
        # night samples, so it is always the higher figure — on these datasets by
        # 3-6 dB. A home reading LAeq 52 / Lden 57 would have been declared
        # "within the guideline" while exceeding it by 4 dB.
        if lden_v is not None and np.isfinite(lden_v):
            if lden_v <= WHO_LDEN_GUIDELINE:
                verdict_txt = (f"Your overall day-and-night noise level (Lden) is {lden_v:.0f} dB, which is "
                               f"<strong>within</strong> the World Health Organization health guideline of "
                               f"{WHO_LDEN_GUIDELINE:.0f} dB.")
                verdict_bg, verdict_clr = '#dcfce7', '#166534'
            else:
                verdict_txt = (f"Your overall day-and-night noise level (Lden) is {lden_v:.0f} dB, which is "
                               f"<strong>{lden_v - WHO_LDEN_GUIDELINE:.0f} dB above</strong> the World Health "
                               f"Organization health guideline of {WHO_LDEN_GUIDELINE:.0f} dB.")
                verdict_bg, verdict_clr = '#fee2e2', '#991b1b'
        else:
            verdict_txt = (
                "An overall guideline comparison could not be computed for this dataset. "
                "The World Health Organization guideline applies to Lden, a day-evening-night "
                "average that needs readable date and time information; that information could "
                "not be read from this file, and it cannot be inferred from the average level alone."
            )
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
        # self.source_files is de-identified in __init__; the single-file fallback
        # must go through the same guard rather than printing the raw filename.
        source_label = (_esc(", ".join(self.source_files)) if self.source_files
                        else _esc(self._figure_source_label()))
        # Floor, never round. Rounding 99.6% to "100%" contradicted the summary
        # directly below, which reports the same figure as 99.6% with gaps, and
        # erased a real one-hour outage from the header a reader sees first.
        completeness_str = (
            f"{math.floor(completeness * 10) / 10:.1f}%".replace(".0%", "%")
            if completeness is not None else "N/A"
        )
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
            _keycard(
                (f"+{guideline_excess:.1f}" if guideline_excess is not None and guideline_excess > 0
                 else f"{guideline_excess:.1f}" if guideline_excess is not None else "N/A"),
                " dB",
                ("Above the health guideline" if guideline_excess is not None and guideline_excess > 0
                 else "Below the health guideline" if guideline_excess is not None
                 else "Health guideline comparison"),
                (f"your 24-hour weighted average (Lden) is {lden_v:.1f} dB against the WHO "
                 f"guideline of {WHO_LDEN_GUIDELINE:.0f} dB"
                 if lden_v is not None else
                 "needs readable date and time information to calculate"),
            ),
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
                "The bar below places your day-evening-night average (Lden) next to everyday sounds "
                "and the World "
                "Health Organization (WHO) health guideline, so you can see where your location sits.",
                f"<div class='verdict' style='background:{verdict_bg};color:{verdict_clr}'>{verdict_txt}</div>"
                + _chart_html(fig_compare)
            ),

            # ── Day-by-day ──
            _section(
                "Day by day",
                "Each point is the average level (LAeq) for one day of monitoring, so you can see "
                "which days were louder and whether the level is steady across the period. The dashed "
                "line marks 53 dB for reference. It is the WHO guideline value, but that guideline "
                "applies to Lden — a whole-period average that adds a penalty to evening and night "
                "hours — so a single day rising above the line is a louder day, not a breach. Your "
                "guideline comparison is the one shown above.",
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
            f"      <div><b>Monitoring period</b><span>{_esc(start_str)} to {_esc(end_str)} ({n_days_v} days)</span></div>",
            f"      <div><b>Data captured</b><span>{completeness_str} of the period</span></div>",
            f"      <div><b>Source file(s)</b><span>{source_label}</span></div>",
            (f"      <div><b>Loudest single moment (L-Max)</b><span>{_hfmt(lamax_v)} dB</span></div>"
             if lamax_v is not None else
             f"      <div><b>Loudest interval average (LAeq)</b><span>{_hfmt(peak_v)} dB</span></div>"),
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

    def _fig_compare_to_references(self, laeq, lden=None):
        """Horizontal bar placing the measured level next to everyday sounds.

        The WHO 53 dB bar is drawn only when Lden is available, and the home's
        bar then shows Lden too. Previously this chart plotted the home's LAeq
        beside the WHO guideline bar: on a home whose LAeq was 52.8 and whose
        Lden was 58.0, the two bars rendered at equal length, showing a resident
        sitting exactly at the guideline when they exceeded it by 5 dB. LAeq
        omits the +5 dB evening and +10 dB night penalties that Lden applies, so
        the two are not comparable.
        """
        have_lden = lden is not None and np.isfinite(lden)
        measured = float(lden) if have_lden else (float(laeq) if laeq is not None and np.isfinite(laeq) else None)
        if measured is None:
            return None

        refs = [
            ("Whisper / quiet bedroom", 30.0, '#cbd5e1'),
            ("Library / soft rain", 40.0, '#cbd5e1'),
            ("Normal conversation", 50.0, '#cbd5e1'),
            ("Busy street traffic", 70.0, '#cbd5e1'),
            ("Power tools (hearing risk)", 85.0, '#cbd5e1'),
        ]
        if have_lden:
            refs.append(("WHO health guideline (Lden)", 53.0, '#f59e0b'))
            refs.append(("Your home (Lden)", measured, '#1e3a5f'))
        else:
            # No guideline bar without the metric it is defined on.
            refs.append(("Your home (average level)", measured, '#1e3a5f'))
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
            xaxis=dict(title='Noise level, dB(A)', range=[0, 95], gridcolor='rgba(0,0,0,0.06)', zeroline=False),
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
            yaxis=dict(title='Average noise, dB(A)', range=[ymin, ymax], gridcolor='rgba(0,0,0,0.06)', zeroline=False),
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
            yaxis=dict(title='Average noise, dB(A)', range=[ymin, ymax], gridcolor='rgba(0,0,0,0.06)', zeroline=False),
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

    def _add_withheld_time_sections_notice(self, story, styles):
        """Explain which sections were withheld because there is no real timeline."""
        story.append(Paragraph("Sections 4-6 and 8: Withheld", styles['h1']))
        story.append(Paragraph(
            "The single-event sleep-disturbance analysis, the daily summary matrix, the diurnal "
            "hourly profile and the advanced time-based visualisations have all been withheld "
            "from this report. Each depends on knowing when every sample was recorded, and the "
            "date and time information in this file could not be read. Presenting them would "
            "mean tabulating and plotting dates and hours that were generated by the software "
            "rather than measured by the instrument. "
            "The overall average level, the statistical percentile profile, and the level "
            "distribution remain valid and are reported in full, because they do not depend on "
            "the timing of individual samples. To obtain the complete assessment, re-export the "
            "source file keeping the full 'YYYY-MM-DD HH:MM:SS' timestamp column.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.12 * inch))

    @staticmethod
    def _file_sha256(path: str) -> str | None:
        """SHA-256 of the source file, for chain of custody.

        Lets any reader confirm that the file this report was produced from is
        byte-identical to the file they hold. Returns None if unreadable.
        """
        import hashlib
        try:
            h = hashlib.sha256()
            with open(path, 'rb') as f:
                for chunk in iter(lambda: f.read(1024 * 1024), b''):
                    h.update(chunk)
            return h.hexdigest()
        except Exception:
            return None

    def _add_provenance_section(self, story, styles):
        """Data provenance and chain of custody.

        A report offered as evidence must let a reader establish what was
        measured, by what instrument, and that the file analysed is the file they
        hold. Logger exports carry no instrument metadata, so the serial number
        and calibration date cannot be read from the data. They are not invented
        or passed over in silence either: the report names the instrument, states
        that each unit carries its own certificate, offers those on request, and
        prints the device identifier so a reader knows which one to ask for.
        """
        story.append(Paragraph("Data Provenance &amp; Chain of Custody", styles['h2']))

        src = os.path.basename(self.filepath or '')
        digest = self._file_sha256(self.filepath) if self.filepath else None

        # The filename is the most common carrier of a participant identifier, so
        # it is withheld under de-identification. The SHA-256 below is what makes
        # the analysis verifiable — a reader holding the original file can confirm
        # it is byte-for-byte the file analysed here — and it discloses nothing
        # about whose home it is.
        if self.deidentify and not is_safe_label(os.path.splitext(src)[0]):
            src_label = "Withheld (see SHA-256 below for verification)"
        else:
            src_label = src or "Not recorded"

        rows = [
            ("Source file", src_label),
            ("SHA-256 of source file", digest or "Not computed"),
            ("Samples analysed", f"{len(self.df):,}"),
            ("Device / location identifier", self.device_id or "Not supplied"),
            ("Sensor placement", self.environment),
            ("Report generated", datetime.now().strftime('%Y-%m-%d %H:%M:%S')),
        ]
        if self.source_files:
            rows.insert(1, ("Merged from", f"{len(self.source_files)} file(s): "
                                           + ", ".join(str(f) for f in self.source_files)))

        for label, value in rows:
            story.append(Paragraph(f"<b>{escape(label)}:</b> {escape(str(value))}", styles['BodyText']))
        story.append(Spacer(1, 0.10 * inch))

        if self.deidentify:
            detail = (" The following were withheld: " + ", ".join(self.redactions) + "."
                      if self.redactions else "")
            story.append(Paragraph(
                "<b>De-identification.</b> This report contains no participant name, address or "
                "other personal identifier. Files are referred to by position rather than by "
                f"filename.{escape(detail)} Verification does not depend on those names: the "
                "SHA-256 digest above identifies the analysed file uniquely, so a reader holding "
                "the original can confirm it is the file this report was produced from.",
                styles['BodyText']
            ))
            story.append(Spacer(1, 0.10 * inch))

        story.append(Paragraph(
            f"<b>Instrument and calibration.</b> {escape(self.instrument_note)} "
            "The data file itself carries no instrument metadata, so the serial number, the "
            "calibration date and the deployment geometry (microphone height, orientation and "
            "distance from any reflecting facade) are not reproduced in this report and are held "
            "with the study records. The device identifier above is the key to those records: it "
            "identifies which unit produced this dataset, and therefore which certificate applies. "
            "Levels reported here are reproducible from the source file independently of that "
            "documentation.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.12 * inch))

    def _add_section_9_disclaimer(self, story, styles):
        """Add Section 9: Methodological Limitations & Disclaimer"""
        story.append(Paragraph("Section 9: Methodological Limitations &amp; Disclaimer", styles['h1']))
        story.append(Spacer(1, 0.15 * inch))

        self._add_provenance_section(story, styles)

        # Averaging period — the most commonly overlooked limitation.
        story.append(Paragraph("<b>Averaging Period</b>", styles['h2']))
        story.append(Paragraph(
            "The WHO 2018 Lden and Lnight guideline values are defined on a LONG-TERM average, "
            "conventionally a full year. The Lden and Lnight reported here are computed over the "
            "duration of this measurement only, which is very much shorter. They are therefore an "
            "indication of conditions during the monitored period, not a determination of "
            "long-term exposure, and a single unusually quiet or unusually busy week will move "
            "them. Seasonal variation, weekday/weekend composition and weather during the "
            "measurement all affect the result. Comparisons against the WHO values in this report "
            "should be read with that limitation in mind.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.12 * inch))

        # Measurement uncertainty.
        story.append(Paragraph("<b>Measurement Uncertainty</b>", styles['h2']))
        story.append(Paragraph(
            "No numerical uncertainty budget is stated in this report: deriving one requires the "
            "deployment geometry and meteorological conditions, which are held with the study "
            "records rather than in the data file. For context, ISO 1996-2 notes that the combined "
            "standard uncertainty of an environmental noise measurement is typically of the order of "
            "1 to 3 dB once instrument tolerance, microphone position, source variability and "
            "meteorological conditions are accounted for. Differences between values in this report "
            "smaller than that should not be treated as meaningful, and any comparison close to a "
            "guideline or limit should be interpreted accordingly rather than as a definitive "
            "pass or fail.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.12 * inch))

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

        from analysis.timestamp_utils import resolve_time_column
        ts_col = resolve_time_column(self.df)

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

    def _figure_source_label(self) -> str:
        """Caption text identifying the data behind a figure.

        Chart annotations are rendered into the image, so a raw filename here
        survives every text-level de-identification check and reaches the reader
        as pixels. Source files routinely carry a participant's name, so the
        stem is printed only when it is a study code.
        """
        stem = os.path.splitext(os.path.basename(self.filepath or ""))[0]
        stem = re.sub(r"^\d{8}_\d{6}_", "", stem)
        if not self.deidentify or is_safe_label(stem):
            return stem or "not recorded"
        return self.device_id or "withheld"

    def _lamax_value(self, lmax_col: str | None) -> float | None:
        """Highest reading in the instrument's L-Max stream, or None if absent.

        LAmax must come from L-Max. The maximum of the LEQ series is the loudest
        interval *average*, which is always lower than the true peak.
        """
        if not lmax_col or lmax_col not in self.df.columns:
            return None
        s = pd.to_numeric(self.df[lmax_col], errors='coerce').dropna()
        return float(s.max()) if not s.empty else None

    @staticmethod
    def _logging_interval_s(ts: pd.Series) -> float | None:
        """Modal spacing between samples, in seconds."""
        try:
            v = _modal_interval_seconds(pd.to_datetime(ts, errors='coerce').dropna())
            return float(v) if v and v > 0 else None
        except Exception:
            return None

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
                f"<b>Source File:</b> {escape(self._figure_source_label())}",
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

        # A synthetic index carries no real calendar information: it begins at
        # datetime.now(), so printing it as a "Measurement Date Range" would
        # attribute the recording to the day the report happened to be run.
        if getattr(self, 'timestamps_synthetic', False):
            story.append(Paragraph(
                "<b>Measurement Date Range:</b> Not available — the date/time column in this "
                "file could not be read. The number of samples and their levels are known; "
                "when they were recorded is not.", styles['BodyText']))
            story.append(Paragraph(
                f"<b>Samples Analysed:</b> {len(self.df):,}", styles['BodyText']))
        else:
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
        # The LAeq here spans the whole record, which is rarely 24 hours.
        _n_days = None
        try:
            _tsv = ts.dropna()
            if len(_tsv) > 1:
                _n_days = int(round((_tsv.max() - _tsv.min()).total_seconds() / 86400.0))
        except Exception:
            pass
        _perlbl = (f"{_n_days}-Day" if _n_days and _n_days > 1 else "24-Hour" if _n_days == 1 else "Whole-Record")
        story.append(Paragraph(f"<b>{_perlbl} Energy Average (LAeq):</b> {escape(laeq_str)}", styles['BodyText']))
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
                    f"The equivalent continuous sound level (LAeq) over the measurement period was "
                    f"{laeq_str} dB(A). <b>The WHO 2018 guidelines are exceeded</b> — see Section 3, "
                    f"which evaluates Lden and Lnight, the metrics those guidelines are defined on."
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
                    f"The equivalent continuous sound level (LAeq) over the measurement period was "
                    f"{laeq_str} dB(A), a {loading} level for a residential environment."
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
            environment=getattr(self, 'environment', 'outdoor'),
            truncation_warning=bool(getattr(self.df, 'attrs', {}).get('truncated_at_row_limit')),
            # A synthetic index is NOT a timeline. Without this gate the report
            # printed invented dates and a full WHO verdict computed from
            # datetime.now(), directly beside the warning saying the timestamps
            # could not be read.
            timestamps_unusable=bool(getattr(self, 'timestamps_synthetic', False)),
            lamax=self._lamax_value(self._resolve_acoustic_columns()[2]),
            logging_interval_s=self._logging_interval_s(ts),
            energy_dominance=energy_concentration(leq.dropna()),
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
        # Every row of this table is a timestamped event. With a synthetic index
        # the levels would be real but the dates and times invented, which is
        # exactly the combination a reader is least able to detect.
        if not getattr(self, 'timestamps_synthetic', False):
            self._add_top_noise_events(story, styles, ts=ts, leq_col=leq_col)
        else:
            story.append(Paragraph(
                "<b>Top Noise Events:</b> Withheld. The loudest levels in this dataset are known, "
                "but the time at which each occurred is not, and an event table without reliable "
                "timestamps cannot be checked against the raw record.",
                styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))

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

        if getattr(self, 'timestamps_synthetic', False):
            story.append(Paragraph(
                "<b>Measurement Span:</b> Not available — timestamps unreadable. Sample counts "
                "below are exact; completeness cannot be assessed without knowing the intended "
                "recording period.", styles['BodyText']))
        else:
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

        # Every standard in this section (WHO Lden/Lnight, COMAR day/night) is
        # defined on a specific time window. With no real timestamps there is no
        # window, so a verdict here would be an assertion about a day that was
        # invented by the parser. Withhold the whole section rather than print a
        # PASS/FAIL that cannot be defended.
        if getattr(self, 'timestamps_synthetic', False):
            story.append(Paragraph(
                "<b>Compliance assessment withheld.</b> Every standard applied in this report "
                "(WHO 2018 Lden and Lnight, Maryland COMAR daytime and nighttime limits) is "
                "defined over a specific time-of-day window. The date and time information in "
                "this file could not be read, so those windows cannot be established and no "
                "compliance verdict can be issued. The overall average level and the statistical "
                "percentiles elsewhere in this report remain valid. Re-export the source file "
                "with a full 'YYYY-MM-DD HH:MM:SS' timestamp column to obtain a compliance "
                "assessment.",
                styles['BodyText']
            ))
            story.append(Spacer(1, 0.12 * inch))
            return

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

        # Convert sample count to duration using the MEASURED logging interval.
        # A hardcoded 1 Hz assumption reported the sample count divided by 60 as
        # "minutes", which is wrong by the ratio of the true interval to 1 s — a
        # logger recording every 2 s understated the duration twofold, and one
        # recording every minute understated it sixtyfold.
        interval_s = _modal_interval_seconds(ts.dropna())
        interval_s = interval_s if interval_s and interval_s > 0 else None
        minutes_exceeding = (n_events * interval_s / 60.0) if interval_s else None

        story.append(Paragraph(
            f"<b>Nighttime hours isolated:</b> 23:00–07:00. Peak extraction uses <b>{escape(lmax_col)}</b> only.",
            styles['BodyText']
        ))
        story.append(Paragraph(
            "This section uses the WHO night window of 23:00–07:00, which is the basis of the "
            "Lnight guideline. The Maryland COMAR night limit assessed in Section 3 is defined "
            "over 22:00–07:00, so the two windows differ by one hour by design.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph(
            f"<b>Absolute highest nighttime peak (LAmax):</b> {escape(self._fmt_db(la_max_night))}",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.08 * inch))
        if minutes_exceeding is not None:
            duration_txt = (f"{self._fmt_float(minutes_exceeding, 1)} minutes "
                            f"(measured logging interval {interval_s:.0f} s)")
        else:
            duration_txt = ("not calculable — the logging interval could not be determined, "
                            "so the sample count cannot be converted to a duration")
        story.append(Paragraph(
            f"<b>Cumulative nighttime exposure exceeding {self._fmt_float(exceed_threshold)} dB(A) "
            f"(outdoor facade):</b> {duration_txt}. "
            f"{n_events:,} of {n_total:,} nighttime samples ({pct:.1f}%).",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph(
            "The 60 dB(A) outdoor figure is used because WHO sets its single-event sleep-disturbance "
            "guideline indoors, at 45 dB(A) LAmax (WHO Guidelines for Community Noise, 1999; carried "
            "forward in the Night Noise Guidelines for Europe, 2009). Converting it to an outdoor "
            "facade level assumes roughly 15 dB of attenuation through a partially open window, the "
            "value WHO uses for that purpose. Actual attenuation depends on the construction, glazing "
            "and window position of the specific dwelling and was not measured here, so this "
            "comparison is indicative. A direct indoor measurement is required to establish indoor "
            "exposure.",
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
            "never an arithmetic mean. Green cells indicate quieter hours; red and orange cells indicate louder "
            "hours. Cell colour reflects the measured level only: a single hour cannot be compared against the "
            "WHO guidelines, which are defined on Lden and Lnight rather than on individual hours. Blank cells "
            "are hours with no data. "
            "<b>How to read it:</b> Scan vertically to identify the noisiest times of day; scan horizontally to "
            "spot unusually loud or quiet individual days. A consistently red row at a given hour indicates a "
            "recurring daily pattern; identifying what produces it requires observations beyond sound level data. "
            "The Y-axis tick for each hour aligns precisely to the centre of its corresponding cell.",
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
            "<b>How to read it:</b> The polygon shape summarises the site's daily level pattern. "
            "A lopsided polygon with morning and late-afternoon peaks indicates activity concentrated at "
            "those hours; a uniformly expanded polygon indicates a level that is broadly steady around the "
            "clock. What produces either pattern cannot be determined from sound level data alone and "
            "requires corroborating observation. "
            "The dashed rings mark 45 dB and 53 dB for visual orientation only. They are the WHO guideline "
            "VALUES, but those guidelines are defined on Lnight and Lden — a night-long and a 24-hour "
            "penalty-weighted average respectively — so an individual hour rising above a ring is not an "
            "exceedance. The compliance assessment in Section 3 evaluates the correct metrics.",
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
            "<b>How to read it:</b> A wider daytime polygon shows that daytime levels exceed nighttime levels. "
            "Shorter weekend than weekday spokes indicate a level that falls at weekends, and roughly equal "
            "spokes indicate a level that does not vary by day of week. These are descriptions of the measured "
            "pattern; attributing any of them to a particular source requires evidence beyond sound level data.",
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

        # KEEP empty resample bins as NaN. Dropping them would hand plotly a
        # gap-free series, and it would draw a straight line straight across an
        # outage — rendering hours of missing data as though they were measured.
        # A NaN breaks the line, so gaps are visible as gaps.
        df_plot = df.resample(freq).agg(agg)
        if df_plot['leq'].notna().sum() == 0:
            df_plot = df.copy()

        # Rolling smooth computed on the already-resampled series (fast path).
        # Window of 4 resampled points provides ~1-hour smoothing at the default
        # 15-min freq, and proportionally wider smoothing for coarser resolutions.
        # Rolling smooth. min_periods=1 lets the window emit a value even where
        # the underlying bin is empty, and the old .dropna() then removed the
        # NaNs that would have broken the line — so this trace was drawn straight
        # across every outage while the LAeq trace beneath it correctly broke.
        # Keep the NaNs and mask the smooth wherever there is no measurement.
        roll_w = min(4, max(1, len(df_plot)))
        rolling_1h = df_plot['leq'].rolling(window=roll_w, min_periods=1).median()
        rolling_1h = rolling_1h.where(df_plot['leq'].notna())

        fig = go.Figure()

        # Draw the envelope first so the LAeq trace stays visually dominant.
        if 'lmax' in df_plot.columns and 'lmin' in df_plot.columns:
            fig.add_trace(go.Scatter(
                x=df_plot.index,
                y=df_plot['lmax'],
                mode='lines',
                name='L-Max envelope',
                line=dict(color='rgba(244,162,97,0.55)', width=1.5, dash='dot'),
                connectgaps=False,
                hoverinfo='skip',
            ))
            fig.add_trace(go.Scatter(
                x=df_plot.index,
                y=df_plot['lmin'],
                mode='lines',
                name='L-Min envelope',
                line=dict(color='rgba(42,157,143,0.55)', width=1.5, dash='dot'),
                connectgaps=False,
                # No 'tonexty' fill. Plotly fills between this trace and the
                # previous one by pairing points positionally, and once NaN gaps
                # are present (which they must be, so outages are not drawn as
                # data) that pairing breaks: the rendered figure showed a large
                # triangular wedge spanning several days that corresponded to no
                # measurement at all. The two dotted envelope lines carry the same
                # information without inventing a shape.
                hoverinfo='skip',
            ))

        fig.add_trace(go.Scatter(
            x=df_plot.index,
            y=df_plot['leq'],
            mode='lines',
            name='LAeq',
            line=dict(color='#111111', width=3),
            connectgaps=False,
            hovertemplate='%{x|%d %b %Y %H:%M}<br>LAeq: %{y:.1f} dB(A)<extra></extra>',
        ))

        if rolling_1h.notna().any():
            fig.add_trace(go.Scatter(
                x=rolling_1h.index,
                y=rolling_1h.values,
                mode='lines',
                name='Rolling 1-Hour Median (L50)',
                line=dict(color='#8E44AD', width=2.5),
                connectgaps=False,
                hovertemplate='%{x|%d %b %Y %H:%M}<br>Rolling 1-Hour Median: %{y:.1f} dB(A)<extra></extra>',
            ))

        # Fixed orientation levels. Both lines previously read "WHO 24-Hr
        # Threshold", which was wrong twice: 45 dB is the Lnight guideline, not a
        # 24-hour one, and neither guideline applies to the LAeq trace plotted
        # here — Lden and Lnight are penalty-weighted long-term averages.
        fig.add_hline(
            y=53.0,
            line_dash='dash',
            line_color='rgba(231,111,81,0.95)',
            annotation_text='53 dB reference',
            annotation_position='top left'
        )
        fig.add_hline(
            y=45.0,
            line_dash='dash',
            line_color='rgba(231,111,81,0.7)',
            annotation_text='45 dB reference',
            annotation_position='bottom left'
        )

        tickformat = '%d %b\n%H:%M' if span <= pd.Timedelta(days=3) else '%d %b'
        fig.update_layout(
            title=dict(text='Chart 1: Time Series — LAeq with L-Max/L-Min envelope',
                       y=0.96, yanchor='top'),
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
                y=1.02,
                xanchor='left',
                x=0,
                bgcolor='rgba(255,255,255,0.85)',
                bordercolor='rgba(0,0,0,0.08)',
                borderwidth=1,
            ),
            margin=dict(l=55, r=25, t=115, b=55),
            autosize=True,
            height=420,
            hovermode='x unified',
            plot_bgcolor='white',
            paper_bgcolor='white',
            annotations=[dict(
                text=f"Source: {self._figure_source_label()}",
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

                # Points beyond the 1.5×IQR fences.
                #
                # At 1 Hz an hour-of-day column holds ~45,000 samples across a
                # multi-week record, so "outliers" number in the thousands and
                # plotting every one rendered a solid vertical bar that hid the
                # whiskers and the box itself. Thin them to a readable sample
                # while ALWAYS keeping the extremes, so the plotted range still
                # spans the true minimum and maximum and no reader is misled
                # about how far the tail reaches.
                outliers = hour_values[(hour_values < lf) | (hour_values > uf)]
                if not outliers.empty:
                    vals = np.sort(outliers.to_numpy())
                    cap = 300
                    if vals.size > cap:
                        # Even coverage of the tail, endpoints pinned.
                        idx = np.unique(np.linspace(0, vals.size - 1, cap).astype(int))
                        vals = vals[idx]
                    fig.add_trace(go.Scatter(
                        x=[label] * len(vals),
                        y=vals,
                        mode='markers',
                        marker=dict(color='#3D5A80', size=3, opacity=0.35),
                        showlegend=False,
                        hovertemplate='Hour: %{x}<br>LEQ: %{y:.1f} dB(A) (beyond 1.5×IQR)<extra></extra>',
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
                    text=f"Source: {self._figure_source_label()}",
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
                text=f"Source: {self._figure_source_label()}",
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

        # Orientation rings.
        #
        # These are drawn at the WHO guideline VALUES, but this chart plots the
        # energy-average LAeq of each hour of day — and neither WHO guideline is a
        # per-hour limit. Lden is a single 24-hour figure carrying +5 dB evening
        # and +10 dB night penalties; Lnight is the average across 23:00-07:00 as
        # a whole. An individual hour sitting above a ring is therefore NOT an
        # exceedance, and labelling the rings "WHO Lnight" / "WHO Lden" invited
        # exactly that reading — a category error in the most persuasive form,
        # a picture. Named as reference levels instead; the actual verdict lives
        # in the compliance section, computed on the correct metrics.
        for ref_val, ref_label, ref_color in [
            (45.0, "45 dB reference", "rgba(52,152,219,0.5)"),
            (53.0, "53 dB reference", "rgba(231,76,60,0.5)"),
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
        # Draw the night wedge to the OUTER edge, not 1 dB above the axis
        # minimum — at the centre it was invisible, so the legend advertised a
        # band the reader could not see.
        night_r = [r_max] * len(night_theta)
        fig.add_trace(go.Scatterpolar(
            r=night_r,
            theta=night_theta,
            mode='lines',
            fill='toself',
            fillcolor='rgba(44,62,80,0.07)',
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
                text='Mean LAeq by Hour of Day',
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

        # Orientation rings. These sit at the WHO guideline VALUES, but this chart
        # plots per-day-of-week LAeq averages — not Lden or Lnight, which are
        # penalty-weighted long-term averages. A spoke crossing a ring is not an
        # exceedance. Same reasoning as the diurnal radar above.
        for ref_val, ref_label, ref_color in [
            (45.0, "45 dB reference", "rgba(52,152,219,0.5)"),
            (53.0, "53 dB reference", "rgba(231,76,60,0.5)"),
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
            story.append(Paragraph(f"Source File: {escape(self._figure_source_label())}", styles['SubTitle']))

        story.append(Paragraph(
            f"Report Type: {report_type.upper()} | Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            styles['SubTitle']
        ))
        story.append(Spacer(1, 0.3 * inch))
