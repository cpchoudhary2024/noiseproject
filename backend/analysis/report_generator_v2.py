# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
import pandas as pd
import numpy as np
from datetime import datetime
import math
import os
import re
import json
import logging
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from analysis.noise_analyzer import NoiseAnalyzer
from analysis.iso_epa_standards import StandardsAnalyzer
from analysis.standards_reference import who_2018_environmental_noise_guideline_levels
from analysis.chart_generator import AdvancedChartGenerator
from analysis.acoustics import (compute_ldn_lden, energetic_mean_db,
                                exceedance_levels_db, energy_concentration,
                                time_above_level_by_period, time_above_level_in_window,
                                nightly_lnight, LDEN_DEFAULT)
from analysis.gap_detector import detect_gaps, gap_report_to_dict, data_completeness_pct, _modal_interval_seconds
from analysis.docx_from_story import render_story_to_docx, PageTrackingDocTemplate
from analysis.compliance_matrix import (evaluate_compliance,
                                        MD_RESIDENTIAL_DAY, MD_RESIDENTIAL_NIGHT,
                                        WHO_ROAD_LDEN, WHO_ROAD_LNIGHT)
import io
from typing import NamedTuple
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, PageBreak, Table, TableStyle, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_LEFT, TA_RIGHT
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)

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
    r'(?:home|house)\s*[A-Z]|'            # Home A, House D
    r'(?:conv|pair|site|loc|dev|unit)\s*[-_]?\d{1,4}|'   # CONV001, SITE-12
    r'[A-Z]{1,6}[-_]?\d{1,6}|'            # MON-4471, SLM12
    r'\d{1,6}'                            # bare numeric id
    r')$',
    re.IGNORECASE,
)


# Matches the home/house labels above, so they can be normalised for display
# ("home a" -> "Home A") before being printed into a chart title.
_HOME_LABEL_RE = re.compile(r'^(home|house)\s*([A-Za-z])$', re.IGNORECASE)


def is_safe_label(value: str) -> bool:
    """True when ``value`` is a study code rather than a personal identifier."""
    v = str(value or '').strip()
    return bool(v) and bool(_SAFE_LABEL_RE.match(v))


# Averaging intervals offered for the Chart 1 time history.
#
# Only intervals that are conventional reporting units in environmental
# acoustics: the 15-minute LAeq (short-term monitoring), the 1-hour LAeq (the
# standard long-term interval, and the interval Lden, Lnight and the COMAR
# period limits are built from), and the 24-hour LAeq (the daily figure). The
# 6-hour average previously used here is not a reporting interval in acoustics —
# it was chosen only to reduce the point count — so a reader could not relate a
# plotted point to any metric in the rest of the report. It is no longer offered.
_TS_BIN_CHOICES: tuple[tuple[pd.Timedelta, str, str], ...] = (
    (pd.Timedelta(minutes=15), '15min', '15-minute'),
    (pd.Timedelta(hours=1),    '1h',    '1-hour'),
    (pd.Timedelta(days=1),     '1D',    '24-hour'),
)

# Above roughly this many points a line stops reading as a trace and fills in as
# a solid band, hiding both the shape and the envelope behind it.
_TS_MAX_POINTS = 1500


def ts_resample_rule(span: pd.Timedelta) -> tuple[str, str]:
    """Choose the Chart 1 averaging interval for a record of length ``span``.

    The finest standard interval that keeps the trace legible. Selecting on a
    point budget rather than on fixed day thresholds means the choice stays
    correct for any record length, including the ones between the thresholds
    that a fixed ladder handles badly.

    Parameters
    ----------
    span : pandas.Timedelta
        Elapsed time between the first and last measurement.

    Returns
    -------
    tuple of (str, str)
        ``(pandas_freq, human_label)`` — e.g. ``('1h', '1-hour')``.
    """
    for interval, freq, label in _TS_BIN_CHOICES:
        if span / interval <= _TS_MAX_POINTS:
            return freq, label
    return _TS_BIN_CHOICES[-1][1], _TS_BIN_CHOICES[-1][2]


def ts_rolling_window(span: pd.Timedelta, bin_interval: pd.Timedelta) -> tuple[pd.Timedelta, str]:
    """Choose the smoothing window for the Chart 1 trend line.

    Environmental noise is dominated by the diurnal cycle, so the window is set
    to the cycle it should remove rather than to a fixed number of bins:

    * **24 hours** for any record of three days or more. One full cycle, so the
      day/night oscillation averages out and what remains is the day-to-day
      trend — the quantity a trend line on a multi-day record should show.
    * **1 hour** for records shorter than three days. There are too few cycles
      to average over, and the diurnal shape is the finding rather than
      something to remove, so the line only takes out sample-to-sample scatter.
    * **7 days** once the bins are themselves daily, where a 24-hour window
      would be a single bin and the trend line would duplicate the LAeq trace.

    Returns
    -------
    tuple of (pandas.Timedelta, str)
        The window and its label, e.g. ``'24-hour'``.
    """
    if bin_interval >= pd.Timedelta(days=1):
        return pd.Timedelta(days=7), '7-day'
    if span < pd.Timedelta(days=3):
        return pd.Timedelta(hours=1), '1-hour'
    return pd.Timedelta(days=1), '24-hour'


# Baseline y-axis window for the Chart 1 time series, in dB(A).
#
# Anchored rather than autoscaled so the 45 dB and 53 dB reference lines occupy
# the same position in every report and two reports remain comparable by eye.
# Chosen to hold the range of environmental noise these loggers record while
# leaving clear headroom above 53 and below 45. It is a floor on the window, not
# a clip: data outside it extends the axis.
Y_AXIS_BASE_WINDOW_DB: tuple[float, float] = (30.0, 90.0)

# Chart typography, in POINTS AT FINAL PRINT SIZE.
#
# Plotly font sizes are pixels on the figure canvas, and the canvas is then
# scaled to fit its box on the page — so the same numeric size renders at a
# different physical size in every chart, depending on how wide its canvas is
# relative to its print box. Setting pixels directly is therefore not a way to
# control legibility: a 13 px label is 12.6 pt in a 700 px canvas printed 9.4 in
# wide, and 6.2 pt in a 1095 px canvas printed 7.3 in wide.
#
# These are declared in points and converted to canvas pixels by
# ``_size_fig_for_print``, which is the only place that knows both numbers.
# Every value clears the 8 pt floor this project requires for publication
# figures.
CHART_TITLE_PT = 13.0
CHART_LEGEND_PT = 9.5
CHART_AXIS_TITLE_PT = 10.5
CHART_TICK_PT = 9.0
CHART_ANNOTATION_PT = 9.0
CHART_FONT_COLOR = '#1F2933'


def ts_resample_ladder_text() -> str:
    """State the averaging interval as the record lengths it applies to.

    Computed from the point budget rather than written out, so the sentence in
    the report always describes what ``ts_resample_rule`` actually does. A
    reader wants to know which interval their own record gets, not the rule the
    code applies to work it out.
    """
    parts = []
    for i, (interval, _freq, label) in enumerate(_TS_BIN_CHOICES):
        if i == len(_TS_BIN_CHOICES) - 1:
            parts.append(f"{label} beyond that")
            break
        # Longest record this interval still fits inside the budget, in whole
        # days — floored, so the stated bound is one the rule really honours.
        max_days = int((interval * _TS_MAX_POINTS).total_seconds() // 86400)
        parts.append(f"{label} for records up to {max_days} days")
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


class TimeSeriesBinning(NamedTuple):
    """How Chart 1 was binned, so the caption can state it rather than guess.

    Attributes
    ----------
    freq : str
        pandas resample rule actually used, e.g. ``'1h'``.
    bin_label : str
        Bin length in words, e.g. ``'1-hour'``.
    window_label : str
        Wall-clock span of the rolling-median window, e.g. ``'4-hour'``.
    """
    freq: str = ''
    bin_label: str = ''
    window_label: str = ''


def deidentify_label(value: str, fallback: str) -> tuple[str, bool]:
    """Return ``(label, was_redacted)`` for a user-supplied identifier.

    Study codes pass through unchanged; anything else is replaced by ``fallback``.
    """
    return (str(value).strip(), False) if is_safe_label(value) else (fallback, True)


class ChartExportUnavailable(RuntimeError):
    """The server cannot rasterise Plotly figures, so a report would have no figures."""


_CHART_EXPORT_OK = False


def ensure_chart_export() -> None:
    """Render a one-point figure; raise ``ChartExportUnavailable`` if that fails.

    Called before any report is built. Each chart's own failure path degrades
    to a placeholder, so without this check a broken export engine (e.g. a
    Plotly/Kaleido version mismatch) ships a report with no figures and no error.
    """
    global _CHART_EXPORT_OK
    if _CHART_EXPORT_OK:
        return
    try:
        go.Figure(go.Scatter(x=[0], y=[0])).to_image(format='png', width=40, height=40)
    except Exception as e:
        logger.exception("Chart export engine unavailable")
        raise ChartExportUnavailable(
            "Figures cannot be rendered on this server, so the report was not generated. "
            f"Chart export failed: {str(e).strip().splitlines()[0][:200]}"
        ) from e
    _CHART_EXPORT_OK = True


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
        # Measured values only. Descriptive bands ("elevated") and everyday
        # analogies ("a busy café") are editorial and are not part of any
        # cited standard, so they are not stated.
        # Label the averaging period honestly: this is the energy average over the
        # WHOLE record, which is rarely 24 hours.
        period_label = (f"{n_days}-day" if n_days and n_days != 1 else
                        ("24-hour" if n_days == 1 else "whole-record"))
        level_sentence = (
            f"The {period_label} energy-average level (LAeq) was {_f(laeq_v)} dB(A)."
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
                if diff >= 0:
                    day_night_sentence = base + f" Daytime exceeded nighttime by {_f(diff)} dB."
                else:
                    day_night_sentence = base + f" Nighttime exceeded daytime by {_f(-diff)} dB."
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
        l10_v = _v(l10)
        l90_v = _v(l90)
        if l10_v is not None and l90_v is not None:
            para3 = (
                f"The level exceeded 10% of the time (L10) was {_f(l10_v)} dB(A) and the level "
                f"exceeded 90% of the time (L90) was {_f(l90_v)} dB(A), a spread of "
                f"{_f(l10_v - l90_v)} dB."
            )

        # ── 4. WHO compliance bullet points ─────────────────────────────────
        WHO_LDEN_LIMIT   = 53.0   # WHO 2018, Table 1 (road traffic, Lden)
        WHO_LNIGHT_LIMIT = 45.0   # WHO 2018, Table 1 (road traffic, Lnight)
        # 40 dB Lnight,outside is the LOAEL established in the WHO Night Noise
        # Guidelines for Europe (2009), which WHO 2018 carries forward.
        WHO_LOAEL_NIGHT  = 40.0

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
                    f"Exceeds the WHO 2018 road-traffic guideline of {WHO_LNIGHT_LIMIT} dB(A) "
                    f"by {_f(excess)} dB."
                )
            elif lnight_v > WHO_LOAEL_NIGHT:
                bullets.append(
                    f"Nighttime level (Lnight, 23:00–07:00): {_f(lnight_v)} dB(A). "
                    f"Within the WHO 2018 road-traffic guideline of {WHO_LNIGHT_LIMIT} dB(A) but above the "
                    f"lowest-observed-adverse-effect level (LOAEL) of {WHO_LOAEL_NIGHT} dB(A), "
                    f"at which initial sleep disturbance effects begin."
                )
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
            bullets.append(f"Whole-record average level (LAeq): {_f(laeq_v)} dB(A). {note}")

        bullet_lines = "\n".join(f"  • {b}" for b in bullets)
        para4 = f"WHO 2018 Health Guideline Compliance:\n{bullet_lines}"

        # ── Assemble with paragraph separators ──────────────────────────────
        parts = [para1, para2]
        if para3:
            parts.append(para3)
        parts.append(para4)
        return "\n\n".join(parts)

    def _compute_summaries(self):
        """
        Compute daily and hourly summaries DIRECTLY from the uploaded dataset.
        This ensures 100% data accuracy and isolation.
        """
        ts_col, leq_col, lmax_col, lmin_col = self._resolve_acoustic_columns()
        
        if not leq_col or leq_col not in self.df.columns:
            logger.warning("No LEQ column found; cannot compute summaries")
            return
        
        ts = self._get_timestamp_series(ts_col)
        leq = self._get_numeric_series(leq_col)
        
        if ts.empty or leq.empty:
            logger.warning("Insufficient timestamp or LEQ data; cannot compute summaries")
            return
        
        df_data = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
        if df_data.empty:
            logger.warning("No valid ts/leq pairs; cannot compute summaries")
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
            
            # Lden for the day (with penalties).
            #
            # The WHOLE calendar day, not the daytime slice. Lden is defined over
            # 24 hours as day + evening + night, and compute_ldn_lden correctly
            # refuses to return a value when a constituent period has no data —
            # so passing only 07:00-22:00 left the night component empty and
            # produced None for every single day. The Lden column of the daily
            # matrix has been blank ever since.
            _lden_out = compute_ldn_lden(group['ts'], leq_vals)
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
            logger.info(f"Computed daily summary: {len(self.daily_summary)} days")
        
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
            logger.info(f"Computed hourly summary: {len(self.hourly_summary)} hours")
    
    # Page geometry, shared by the PDF and Word paths so both documents lay out
    # on the same sheet with the same text block.
    TECHNICAL_PAGE = dict(width_in=11.0, height_in=8.5,
                          margins_in=(0.5, 0.5, 0.75, 0.75))
    RESIDENT_PAGE = dict(width_in=8.5, height_in=11.0,
                         margins_in=(0.6, 0.6, 0.6, 0.6))

    def generate_pdf_report(self, report_type='comprehensive', output_dir: str | None = None):
        """Generate the 8-section publication-grade PDF report."""
        report_filename = f"noise_analysis_{report_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        report_dir = output_dir or os.path.dirname(self.filepath)
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, report_filename)

        doc = self._technical_doc_template(report_path)
        story = self._build_technical_story(doc, report_type)
        doc.build(story, onFirstPage=self._add_page_template, onLaterPages=self._add_page_template)
        return report_path

    def generate_docx_report(self, report_type='comprehensive', output_dir: str | None = None):
        """The same report as :meth:`generate_pdf_report`, as an editable Word file.

        Built from the identical story, so the two documents cannot diverge.
        """
        report_filename = f"noise_analysis_{report_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
        report_dir = output_dir or os.path.dirname(self.filepath)
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, report_filename)

        doc = self._technical_doc_template(io.BytesIO())
        story = self._build_technical_story(doc, report_type)
        page_of = self._paginate(doc, story)
        return render_story_to_docx(
            story, report_path,
            page_width_in=self.TECHNICAL_PAGE['width_in'],
            page_height_in=self.TECHNICAL_PAGE['height_in'],
            margins_in=self.TECHNICAL_PAGE['margins_in'],
            footer_lines=self._footer_lines(),
            page_of=page_of,
        )

    def _technical_doc_template(self, path: str) -> SimpleDocTemplate:
        g = self.TECHNICAL_PAGE
        top, bottom, left, right = g['margins_in']
        return PageTrackingDocTemplate(
            path, pagesize=(g['width_in'] * inch, g['height_in'] * inch),
            topMargin=top * inch, bottomMargin=bottom * inch,
            leftMargin=left * inch, rightMargin=right * inch,
        )

    @staticmethod
    def _paginate(doc, story: list) -> dict[int, int]:
        """Lay the story out and report which page each flowable landed on.

        The layout is thrown away — only the page numbers are wanted. Building
        against a BytesIO keeps it off disk. ReportLab consumes the story list
        during build, so a copy is passed and the caller's list stays intact for
        the Word renderer to walk.
        """
        doc.build(list(story))
        return dict(doc.flowable_pages)

    @staticmethod
    def _footer_lines() -> tuple[str, ...]:
        """Footer text, matching what _add_page_template draws on the PDF."""
        return (
            f"Environmental Noise Analysis | {datetime.now().strftime('%Y-%m-%d')}",
            "Developed by Chandra Prakash Choudhary | PI: Dr. Ana María Rule, "
            "Associate Professor — Johns Hopkins University",
        )

    def _build_technical_story(self, doc, report_type='comprehensive') -> list:
        """Assemble the technical report as a ReportLab story.

        The single definition of the report's content. Both the PDF and the Word
        renderer consume what this returns, so a change here reaches both.
        """
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
            # No forced break here. Section 5's table is as long as the record,
            # so a fixed break left whatever it did not use blank — 60% of a
            # page on a one-week record. Section 6 now flows on behind it and
            # starts a page of its own only when it genuinely needs one.
            story.append(Spacer(1, 0.2 * inch))

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

        return story

    # ============================================================
    # RESIDENT REPORT (two pages, three charts)
    # ============================================================

    def _display_location_label(self) -> str:
        """The location label as it should be printed, or '' if none is printable.

        Only study codes reach print — the same allow-list the rest of the report
        de-identifies against — so a free-text entry cannot leak into a chart
        title, where it would be baked into the image and survive every
        text-level check. ``home a`` is normalised to ``Home A``.
        """
        label = str(self.device_id or '').strip()
        if not label or not is_safe_label(label):
            return ''
        m = _HOME_LABEL_RE.match(label)
        return f"{m.group(1).capitalize()} {m.group(2).upper()}" if m else label

    def _chart_location_suffix(self) -> str:
        """`' at Home A'` for chart titles, or `''` when no label was supplied."""
        label = self._display_location_label()
        return f" at {label}" if label else ""

    # Name given to the provenance annotation, so it can be found and removed
    # without disturbing any other annotation on the figure.
    SOURCE_ANNOTATION_NAME = 'figure-source'
    # Reference-line labels drawn outside the plot frame, in the right margin.
    REF_LABEL_ANNOTATION_NAME = 'ref-line-label'

    @staticmethod
    def _apply_chart_typography(fig, *, title: str):
        """Embolden the title. Sizes are applied later by ``_size_fig_for_print``,
        which is the only place that knows the print box the canvas maps onto.

        Parameters
        ----------
        title : str
            Plain title text. Emboldened here — do not pass markup.
        """
        fig.update_layout(title=dict(text=f"<b>{title}</b>", font=dict(color=CHART_FONT_COLOR)))
        return fig

    @staticmethod
    def _scale_fig_fonts(fig, *, px_per_pt: float):
        """Set every font on ``fig`` from the point sizes declared at module level.

        Parameters
        ----------
        px_per_pt : float
            Canvas pixels per printed point, i.e. ``(canvas_px / inches) / 72``.
        """
        def px(pt: float) -> int:
            return max(1, int(round(pt * px_per_pt)))

        tick_font = dict(size=px(CHART_TICK_PT), color=CHART_FONT_COLOR)
        fig.update_layout(
            title=dict(font=dict(size=px(CHART_TITLE_PT), color=CHART_FONT_COLOR)),
            legend=dict(font=dict(size=px(CHART_LEGEND_PT), color=CHART_FONT_COLOR)),
            font=tick_font,
        )
        axis_title = dict(font=dict(size=px(CHART_AXIS_TITLE_PT), color=CHART_FONT_COLOR))
        # Polar charts have no cartesian axes; update_xaxes is a silent no-op on
        # them and would leave their tick text at the plotly default.
        if any(getattr(tr, 'type', '') in ('scatterpolar', 'barpolar') for tr in fig.data):
            fig.update_polars(radialaxis=dict(tickfont=tick_font),
                              angularaxis=dict(tickfont=tick_font))
        else:
            # automargin lets plotly measure the rendered tick labels and grow
            # the margin to fit them. Without it the axis title is placed at a
            # fixed offset and the tick numbers overprint it as soon as the type
            # size or the number of digits changes — which is exactly what
            # happened when these fonts were scaled up for print.
            fig.update_xaxes(title=axis_title, tickfont=tick_font, automargin=True)
            fig.update_yaxes(title=axis_title, tickfont=tick_font, automargin=True)
        fig.update_traces(colorbar=dict(
            title=dict(font=dict(size=px(CHART_AXIS_TITLE_PT), color=CHART_FONT_COLOR)),
            tickfont=tick_font,
        ), selector=dict(type='heatmap'))
        for ann in fig.layout.annotations:
            ann.font.size = px(CHART_ANNOTATION_PT)
        return fig

    def _add_source_annotation(self, fig, *, y: float = -0.20):
        """Append the provenance caption to a figure.

        Uses ``add_annotation``, never ``update_layout(annotations=[...])``:
        plotly merges array properties element-wise, so passing a one-element
        list overwrote the annotations ``add_hline`` had already created. That
        silently replaced the "53 dB reference" and "45 dB reference" labels
        with two stacked copies of this caption — the reference lines have been
        rendering unlabelled ever since.
        """
        fig.add_annotation(
            name=self.SOURCE_ANNOTATION_NAME,
            text=f"Source: {self._figure_source_label()}",
            xref='paper', yref='paper',
            x=1, y=y,
            xanchor='right', yanchor='top',
            showarrow=False,
            font=dict(size=9, color='rgba(80,80,80,0.85)'),
        )
        return fig

    @staticmethod
    def _stack_height(flowables: list, avail_width: float) -> float:
        """Total laid-out height of ``flowables`` at ``avail_width``, in points.

        ``wrap`` is what the platypus frame itself calls to place each flowable,
        so this measures the real rendered height — including text that wraps to
        an extra line — rather than an estimate that drifts with the content.
        """
        total = 0.0
        for f in flowables:
            try:
                total += f.wrap(avail_width, 0x7FFFFFFF)[1]
                total += getattr(f, 'getSpaceBefore', lambda: 0)()
                total += getattr(f, 'getSpaceAfter', lambda: 0)()
            except Exception:
                # A flowable that cannot be measured is skipped rather than
                # allowed to abort the report; the floor on chart height keeps
                # the result sane if that ever costs us a few points.
                continue
        return total

    def _chart_image(self, fig, *, width_inch: float, height_inch: float, drop_source: bool = True):
        """Size a figure to its print box and convert it to a ReportLab image.

        The single path every chart in every report goes through, so text on the
        page is the same physical size in all of them.

        The in-image provenance caption is dropped by default and printed in the
        figure caption instead: it sits at a fixed fraction below the axis, so
        it was clipped by the frame whenever a chart's margins or type size
        changed. Caption text cannot be clipped.
        """
        sized = self._size_fig_for_print(fig, width_inch=width_inch, height_inch=height_inch,
                                         drop_source=drop_source)
        return self._plotly_fig_to_image(sized, width_inch=width_inch, height_inch=height_inch)

    def _size_fig_for_print(self, fig, *, width_inch: float, height_inch: float,
                            dpi: int = 300, drop_source: bool = False):
        """Fix a figure's pixel canvas to the print box at ``dpi`` and size its fonts.

        Plotly's default 700×N canvas made figures render narrower than the text
        column: ``_plotly_fig_to_image`` preserves aspect, so a tall default
        canvas hits the height cap and the width shrinks to match. Sizing the
        canvas to the print box instead means the figure fills the column at the
        requested resolution — and fixes the physical size of a point of text,
        which is what makes the type scale meaningful.

        Parameters
        ----------
        width_inch, height_inch : float
            Print box on the page.
        dpi : int
            Target resolution of the rendered image, 300 minimum for print.
        drop_source : bool
            Remove the in-image provenance caption, for callers that print it in
            the figure caption instead.
        """
        if fig is None:
            return None
        # _plotly_fig_to_image multiplies the canvas by this on export.
        export_scale = 1.25 if len(self.df) > 500_000 else 2
        canvas_w = int(round(width_inch * dpi / export_scale))
        fig.update_layout(
            width=canvas_w,
            height=int(round(height_inch * dpi / export_scale)),
            autosize=False,
        )
        if drop_source:
            # Drop only the provenance caption, where the caller prints it as
            # text instead. Everything else stays: clearing the whole list would
            # also take the "53 dB reference" / "45 dB reference" labels that
            # add_hline puts here. Direct assignment, because
            # update_layout(annotations=[]) is a no-op on an array property.
            fig.layout.annotations = tuple(
                a for a in fig.layout.annotations if a.name != self.SOURCE_ANNOTATION_NAME
            )
        # Fonts last, so they are sized against the canvas just set.
        px_per_pt = (canvas_w / width_inch) / 72.0
        self._scale_fig_fonts(fig, px_per_pt=px_per_pt)

        # Reserve room under a polar chart for its horizontal legend. A polar
        # chart draws its angular tick labels on the circle's perimeter, so the
        # bottom labels and the legend compete for the same band — and unlike a
        # cartesian axis a polar one has no automargin to settle it. The radar
        # layouts set that band in pixels against plotly's default 700 px
        # canvas; this method then resizes the canvas and scales the fonts up
        # for print, leaving the band too small. That is what clipped the 11:00
        # to 13:00 labels on Chart 4 and hid "Friday" behind the legend on
        # Chart 5. Size the band from the legend's own metrics instead, and take
        # the legend's fill off so it can never cover a label it overlaps.
        if any(getattr(tr, 'type', '') in ('scatterpolar', 'barpolar') for tr in fig.data):
            canvas_h = int(round(height_inch * dpi / export_scale))
            row_px = 1.8 * CHART_LEGEND_PT * px_per_pt
            names = [str(tr.name or '') for tr in fig.data
                     if getattr(tr, 'showlegend', True) is not False and tr.name]
            if names:
                # 0.55 em per character plus 4 em for the swatch and gutter.
                entry_px = [(len(n) * 0.55 + 4.0) * CHART_LEGEND_PT * px_per_pt for n in names]
                inner_w = max(1.0, canvas_w - (fig.layout.margin.l or 0) - (fig.layout.margin.r or 0))
                rows = max(1, math.ceil(sum(entry_px) / inner_w))
                band = int(round(rows * row_px + 0.6 * row_px))
                if band > (fig.layout.margin.b or 0):
                    fig.update_layout(margin=dict(b=band))
                plot_h = max(1, canvas_h - (fig.layout.margin.t or 0) - band)
                fig.update_layout(legend=dict(
                    orientation='h', xanchor='center', x=0.5,
                    yanchor='top', y=-(0.35 * row_px / plot_h),
                    bgcolor='rgba(0,0,0,0)', borderwidth=0,
                ))

        # Reserve room for any label parked in the right margin. Its width scales
        # with the font, so a fixed margin clips it as soon as the type size
        # changes — which is what cut the "dB" off "53 dB" on Chart 1.
        margin_labels = [a for a in fig.layout.annotations
                         if a.name == self.REF_LABEL_ANNOTATION_NAME]
        if margin_labels:
            widest = max(len(str(a.text or '')) for a in margin_labels)
            # 0.62 em per character is a safe upper bound for digits and capitals
            # in this face; the leading gap keeps the text clear of the frame.
            needed = int(round(widest * 0.62 * CHART_ANNOTATION_PT * px_per_pt)) + int(12 * px_per_pt)
            current = fig.layout.margin.r or 0
            if needed > current:
                fig.update_layout(margin=dict(r=needed))
        return fig

    def _exceedance_summary(self, ts: pd.Series, leq: pd.Series) -> dict:
        """How often the measured level sat above each applicable limit.

        Two different questions, because the two standards are different kinds
        of thing:

        * **Maryland COMAR** states levels in dB(A), so "what share of the
          period was at or above the level" is a question the limit supports.
          Each period is its own denominator — the night share is a share of
          measured night-time, not of the 24-hour day. Pooling over 24 hours
          would dilute the night figure with the daytime hours the night limit
          does not govern.
        * **WHO Lnight** is also a plain LAeq of the measured levels, over
          23:00-07:00, so a share of time above 45 dB(A) is exactly as
          computable — on that window, not COMAR's 22:00-07:00.
        * **WHO Lden** is not: it adds +5 dB to evening and +10 dB to night
          before averaging, so its 53 dB(A) sits on a penalty-weighted scale no
          measured reading is on. For Lden the answerable question is how many
          individual days had an Lden above the guideline.

        Every share is a description of exposure, not a compliance verdict: all
        of these limits are assessed on a period average.

        Returns
        -------
        dict
            ``day_pct``/``night_pct`` for COMAR, ``who_night_pct`` for WHO
            Lnight, ``nights_over``/``nights_total`` and
            ``days_over``/``days_total`` for the per-period counts, and
            ``interval_s``. Values are None when the data cannot support them.
        """
        out: dict = {'day_pct': None, 'night_pct': None, 'who_night_pct': None,
                     'nights_over': None, 'nights_total': 0,
                     'days_over': None, 'days_total': 0,
                     'interval_s': self._logging_interval_s(ts)}
        if leq.dropna().empty:
            return out

        out.update(time_above_level_by_period(
            ts, leq,
            day_threshold_db=MD_RESIDENTIAL_DAY,
            night_threshold_db=MD_RESIDENTIAL_NIGHT,
        ))
        # WHO Lnight's own window, which is an hour shorter than COMAR's.
        out['who_night_pct'], _n = time_above_level_in_window(
            ts, leq, threshold_db=WHO_ROAD_LNIGHT,
            start_hour=LDEN_DEFAULT.night_start, end_hour=LDEN_DEFAULT.night_end)

        nights = nightly_lnight(ts, leq)
        if not nights.empty:
            out['nights_total'] = int(len(nights))
            out['nights_over'] = int((nights > WHO_ROAD_LNIGHT).sum())

        if self.daily_summary is not None and 'Daily_Lden' in self.daily_summary.columns:
            lden_days = pd.to_numeric(self.daily_summary['Daily_Lden'], errors='coerce').dropna()
            if not lden_days.empty:
                out['days_total'] = int(len(lden_days))
                out['days_over'] = int((lden_days > WHO_ROAD_LDEN).sum())
        return out

    @classmethod
    def _exceedance_note(cls, stats: dict, *, brief: bool = False) -> str:
        """Explain what the two exceedance figures are, and what they are not.

        Shared by both reports so the caveats travel with the numbers wherever
        they appear, and appear exactly once in each document. ``brief`` keeps
        the two points a reader needs to avoid misreading the figures and drops
        the methodological detail, which the technical report carries in full.
        """
        iv = stats.get('interval_s')
        if iv is None or iv <= 0:
            interval_phrase = "the logging interval of the instrument"
        elif iv < 60:
            interval_phrase = f"the {iv:.0f}-second readings the logger stored"
        else:
            interval_phrase = f"the {iv / 60:.0f}-minute readings the logger stored"
        if brief:
            # Merged with the "why two night figures" note: both were explaining
            # windows, and on a two-page report the overlap cost a third page.
            return (
                "The two agencies (MD and WHO) define night differently — Maryland COMAR averages 22:00–07:00 (9 h) "
                f"against {MD_RESIDENTIAL_NIGHT:.0f} dB(A), WHO Lnight averages 23:00–07:00 (8 h) "
                f"against {WHO_ROAD_LNIGHT:.0f} dB(A) for health — so the average and the share of time "
                "are each computed over that standard's own window, never the 24-hour day. Shares are of "
                f"<i>measured</i> time, counted on the readings stored by the monitor. Lden has no share of time: it adds "
                f"+5 dB to evening and +10 dB to night readings before averaging, so its "
                f"{WHO_ROAD_LDEN:.0f} dB(A) is not on the scale of any single reading. A level can meet "
                "Maryland law and still exceed the WHO guideline."
            )
        return (
            "<b>On the shares of time.</b> Each share is counted within its own window and against its own "
            "level. The COMAR night figure covers 22:00–07:00 against "
            f"{MD_RESIDENTIAL_NIGHT:.0f} dB(A); the WHO night figure covers 23:00–07:00 against "
            f"{WHO_ROAD_LNIGHT:.0f} dB(A). Both are plain LAeq comparisons on the measured scale, so both "
            "are computable in the same way — they differ in window and in level, not in kind. A night "
            "share uses measured night-time as its denominator, never the 24-hour day, which would dilute "
            "it with hours the night level does not govern. All are shares of <i>measured</i> time, "
            f"counted on {interval_phrase}; a shorter logging interval resolves brief peaks that a longer "
            "one averages away. "
            "<b>Lden has no such figure</b> because it adds +5 dB to evening and +10 dB to night readings "
            "before averaging: its 53 dB(A) sits on a penalty-weighted scale that no measured reading is "
            "on, so a count of readings above 53 dB(A) would not be about Lden at all. "
            "<b>None of these shares is a compliance verdict.</b> Every limit here is assessed on a period "
            "average, and a period can pass on its average while spending real time above the level; the "
            "verdict rows above carry the assessment. WHO further intends Lden and Lnight as long-term "
            "annual averages, so figures from a short record are indicative."
        )

    @staticmethod
    def _fmt_share(pct: float | None) -> str:
        """Format a percentage of time, without rounding a real event to zero."""
        if pct is None:
            return "not available"
        if pct == 0:
            return "0%"
        if pct < 0.1:
            return "under 0.1%"
        return f"{pct:.1f}%"

    @staticmethod
    def _chart3_method_sentences() -> str:
        """The 'how Chart 3 was made' text, shared by both reports.

        Describes the viridis scale the heatmap actually uses. The technical
        report previously described a red/green scale, which this chart has
        never drawn — a reader following it would have read the loudest hours as
        the quietest, since viridis puts bright yellow at the top of the range.
        """
        return (
            "<b>Cells</b> — one clock hour on one date, computed by logarithmic energy averaging (never an "
            "arithmetic mean) over the samples in that hour; blank cells are hours with no data. "
            "<b>Colour</b> — viridis: dark purple quietest, green mid-range, bright yellow loudest. Colour "
            "reflects the measured level only; a single hour cannot be compared against the WHO "
            "guidelines, which are defined on Lden and Lnight. Every second hour is labelled, each tick "
            "on the centre of its cell."
        )

    @staticmethod
    def _chart2_method_sentences(*, metrics_at: str, reading: str = 'individual') -> str:
        """The 'how Chart 2 was made' text, shared by both reports.

        Carries the box-plot vocabulary, defined here rather than in the
        definitions table because this is the only figure that uses it. The
        wording follows the PI's supplied definitions, with the whisker rule
        corrected: hers fixed the upper whisker at Q3 + 1.5 x IQR and the lower
        at the smallest observation, which is two different rules. Both whiskers
        reach the furthest observation *within* 1.5 x IQR of the box, which is
        the Tukey convention this chart is drawn to.
        """
        return (
            "A visual summary of the LAeq at each hour of the day, pooled across every measured day: the "
            "00:00 box holds every reading taken between 00:00 and 00:59 on any day of the record. "
            "<b>Box</b> — the interquartile range (IQR), the middle 50% of readings, from the 25th "
            "percentile (Q1) to the 75th (Q3). <b>Median</b> — the line across the box: half the readings "
            "are louder, half quieter. <b>Whiskers</b> — extend to the furthest reading within 1.5 x IQR "
            f"of the box, one above Q3 and one below Q1. <b>Outliers</b> — the points beyond the whiskers: "
            f"{reading} readings that fall outside the majority of the data, the quietest at night and the "
            "loudest by day. What produced any individual outlier cannot be determined from sound level "
            "data. A tall box means the level at that hour varied widely between days; a short box means "
            "it was consistent. "
            "<b>Colours</b> — orange is daytime (07:00\u201322:00), dark blue on the shaded background is "
            "night (22:00\u201307:00), following the Maryland COMAR definition. [3] "
            f"<b>Dashed lines</b> — the COMAR 26.02.03.03 Table 2 residential limits, "
            f"{MD_RESIDENTIAL_DAY:.0f} dB(A) by day and {MD_RESIDENTIAL_NIGHT:.0f} dB(A) by night, each "
            f"drawn only across the hours its period covers. They apply to the LAeq of the whole day or "
            f"whole night period, reported {metrics_at}, not to a single hour or a single reading. [2]"
        )

    @staticmethod
    def _chart1_method_sentences(binning: 'TimeSeriesBinning', *, metrics_at: str) -> str:
        """The 'how Chart 1 was made' text, shared by both reports.

        One sentence per step of the calculation, in the order the calculation
        happens, so a reader can follow it without inferring anything.
        """
        if not binning.bin_label:
            return "Each plotted point is an energy-averaged LAeq over the resampling interval."
        # One labelled entry per element of the figure. A reader looking at a
        # single line on the chart can find just that line, instead of reading a
        # paragraph to locate it.
        return (
            f"<b>Black line</b> — LAeq in consecutive {binning.bin_label} bins: every sample in a bin is "
            f"combined by logarithmic energy averaging into one value, giving one point per bin, not one "
            f"per sample. Bin length is set by record length ({ts_resample_ladder_text()}). "
            f"<b>Shaded band</b> — L-Min to L-Max within the same bins. "
            f"<b>Purple line</b> — centered {binning.window_label} moving median, which removes the daily "
            f"cycle and leaves the underlying trend. "
            f"<b>Breaks</b> — bins with no measurement are left blank, so a gap is an outage, not a quiet "
            f"period. "
            f"<b>Dashed lines</b> — the WHO 2018 values of {WHO_ROAD_LDEN:.0f} and "
            f"{WHO_ROAD_LNIGHT:.0f} dB(A), drawn as a scale only. WHO sets its limits on Lden and Lnight, "
            f"whole-period figures reported {metrics_at}, not on a {binning.bin_label} average, so a bin "
            f"above a line is not by itself an exceedance."
        )

    @staticmethod
    def _period_title_label(ts_valid: pd.Series) -> str:
        """Name the measurement period for a chart title, e.g. ``'March 2026'``."""
        if ts_valid.empty:
            return ''
        start, end = ts_valid.min(), ts_valid.max()
        if (start.year, start.month) == (end.year, end.month):
            return start.strftime('%B %Y')
        if start.year == end.year:
            return f"{start.strftime('%B')}–{end.strftime('%B %Y')}"
        return f"{start.strftime('%B %Y')} – {end.strftime('%B %Y')}"

    def generate_resident_pdf_report(self, output_dir: str | None = None) -> str:
        """Two-page resident summary: headline numbers plus Charts 1, 2 and 3.

        Portrait letter, so it reads and prints as a handout rather than as an
        extract of the landscape technical report. Every figure and number is
        computed by the same code paths as the comprehensive report — this is a
        shorter selection of that report, never a separate calculation.

        Returns
        -------
        str
            Absolute path to the written PDF.
        """
        report_filename = f"resident_noise_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        report_dir = output_dir or os.path.dirname(self.filepath)
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, report_filename)

        doc = self._resident_doc_template(report_path)
        story = self._build_resident_story(doc)
        doc.build(story, onFirstPage=self._add_page_template, onLaterPages=self._add_page_template)
        return report_path

    def generate_resident_docx_report(self, output_dir: str | None = None) -> str:
        """The resident summary as an editable Word file, from the same story."""
        report_filename = f"resident_noise_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.docx"
        report_dir = output_dir or os.path.dirname(self.filepath)
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, report_filename)

        doc = self._resident_doc_template(io.BytesIO())
        story = self._build_resident_story(doc)
        page_of = self._paginate(doc, story)
        return render_story_to_docx(
            story, report_path,
            page_width_in=self.RESIDENT_PAGE['width_in'],
            page_height_in=self.RESIDENT_PAGE['height_in'],
            margins_in=self.RESIDENT_PAGE['margins_in'],
            footer_lines=self._footer_lines(),
            page_of=page_of,
        )

    def _resident_doc_template(self, path: str) -> SimpleDocTemplate:
        g = self.RESIDENT_PAGE
        top, bottom, left, right = g['margins_in']
        return PageTrackingDocTemplate(
            path, pagesize=(g['width_in'] * inch, g['height_in'] * inch),
            topMargin=top * inch, bottomMargin=bottom * inch,
            leftMargin=left * inch, rightMargin=right * inch,
            title="Resident Noise Summary", author="",
        )

    def _build_resident_story(self, doc) -> list:
        """Assemble the resident summary as a ReportLab story (PDF and Word)."""
        ts_col, leq_col, lmax_col, lmin_col = self._resolve_acoustic_columns()
        styles = self._get_pdf_styles()
        cap = ParagraphStyle('ResidentCaption', parent=styles['BodyText'],
                             fontSize=8, leading=10.5,
                             textColor=colors.HexColor('#374151'), spaceAfter=2)
        story: list = []

        ts = self._get_timestamp_series(ts_col)
        leq = self._get_numeric_series(leq_col)
        lmax = self._get_numeric_series(lmax_col) if (lmax_col and lmax_col in self.df.columns) else None
        lmin = self._get_numeric_series(lmin_col) if (lmin_col and lmin_col in self.df.columns) else None

        ts_valid = ts.dropna()
        location = self._display_location_label()
        suffix = self._chart_location_suffix()
        period = self._period_title_label(ts_valid)

        # ── Header ───────────────────────────────────────────────────────────
        story.append(Paragraph("Resident Noise Summary", styles['Title']))
        story.append(Spacer(1, 0.10 * inch))
        header_bits = [b for b in (location, period) if b]
        if header_bits:
            story.append(Paragraph(" &nbsp;·&nbsp; ".join(escape(b) for b in header_bits), styles['SubTitle']))
        story.append(Paragraph(
            f"Generated {datetime.now().strftime('%d %b %Y')}", styles['Footer']))
        story.append(Spacer(1, 0.10 * inch))

        if self.timestamps_synthetic:
            story.append(Paragraph(
                "<b>The date and time information in this file could not be read.</b> The charts below "
                "would show substituted dates rather than real ones, so this summary cannot be produced "
                "from this file. Re-export the data keeping the full 'YYYY-MM-DD HH:MM:SS' timestamp column.",
                styles['BodyText']))
            return story

        # ── Definitions, before anything that uses the terms ─────────────────
        story.append(self._resident_definitions_table(ts))
        story.append(Spacer(1, 0.10 * inch))

        # ── Key numbers ──────────────────────────────────────────────────────
        story.extend(self._resident_result_tables(ts=ts, leq=leq, lmax_col=lmax_col))
        story.append(Spacer(1, 0.05 * inch))
        story.append(Paragraph(self._exceedance_note(self._exceedance_summary(ts, leq), brief=True), cap))
        story.append(Spacer(1, 0.08 * inch))

        source_note = f" <b>Data source:</b> {escape(self._figure_source_label())}."
        avail_w = doc.width / inch

        # No forced break here. The definitions and results table fill page 1 and
        # a little runs onto page 2; a hard break at this point left that page
        # 21% full. Each figure is wrapped with its caption instead, so the
        # pair moves as a unit and the pages pack themselves.

        # ── Figure 1 — time series ───────────────────────────────────────────
        t1 = f"{period} noise time series{suffix}".strip()
        fig1, binning = self._fig_time_series_with_band(
            ts=ts, leq=leq, lmax=lmax, lmin=lmin, title=t1)
        img1 = self._chart_image(fig1, width_inch=avail_w, height_inch=3.0)
        story.append(KeepTogether([
            img1 if img1 else Paragraph("<i>Time series chart unavailable.</i>", styles['BodyText']),
            Paragraph(
                f"<b>Figure 1.</b> Sound level over the whole monitoring period. "
                f"{self._chart1_method_sentences(binning, metrics_at='in Table 4')}"
                + source_note,
                cap),
        ]))
        story.append(Spacer(1, 0.16 * inch))

        # ── Figure 2 — hourly distribution ───────────────────────────────────
        reading_word = self._reading_word(ts)
        # The box-plot vocabulary is defined here rather than in the definitions
        # table on page 1, because it is only needed at this figure and reads
        # better beside the thing it describes.
        t2 = f"{period} noise Leq variability by time of day{suffix}".strip()
        fig2 = self._fig_diurnal_box_whisker(ts=ts, leq=leq, title=t2)
        img2 = self._chart_image(fig2, width_inch=avail_w, height_inch=3.0)
        story.append(KeepTogether([
            img2 if img2 else Paragraph("<i>Hourly variability chart unavailable.</i>", styles['BodyText']),
            Paragraph(
                "<b>Figure 2.</b> " + self._chart2_method_sentences(
                    metrics_at="in Table 3", reading=reading_word)
                + source_note,
                cap),
        ]))

        story.append(PageBreak())

        # ── Figure 3 — heatmap ───────────────────────────────────────────────
        t3 = f"{period} noise levels by date and hour{suffix}".strip()
        fig3 = self._fig_temporal_heatmap(ts=ts, leq=leq, title=t3)
        img3 = self._chart_image(fig3, width_inch=avail_w, height_inch=3.4)
        story.append(KeepTogether([
            img3 if img3 else Paragraph("<i>Heatmap unavailable.</i>", styles['BodyText']),
            Paragraph(
                "<b>Figure 3.</b> LAeq for every hour of every measured day. "
                + self._chart3_method_sentences() +
                " Reading down a column shows how one day changed hour by hour; reading across a row "
                "shows whether a given hour behaved the same way from day to day." + source_note,
                cap),
        ]))
        story.append(Spacer(1, 0.12 * inch))

        # ── Closing note ─────────────────────────────────────────────────────
        # Kept short so the summary holds to two pages. The full instrument
        # provenance, uncertainty budget and compliance assessment are in the
        # comprehensive report, which this points to rather than reproducing.
        story.append(Paragraph(
            "<b>About the WHO guidelines.</b> The values quoted are the road-traffic guideline values. WHO "
            "sets its guidelines separately for each transport source, and road traffic is the general, "
            "widely cited benchmark; quoting it here does not assert that road traffic is the source of "
            "the sound measured at this home. A sound level meter records total sound energy and cannot "
            "identify what produced it. The WHO values are annual averages, so a record of days or weeks "
            "is indicative rather than a determination. [1]",
            cap))
        story.append(Spacer(1, 0.06 * inch))
        story.append(Paragraph(
            "<b>About these measurements.</b> Levels were recorded with a Convergence Instruments "
            "NSRT_W_mk4 sound level logger, A-weighted. Each unit is factory-calibrated and supplied with "
            "its own manufacturer's certificate, retained by the study team and available on request. "
            "A sound level meter records total sound energy; it does not identify what produced a sound, "
            "so attributing any level here to a particular source requires evidence beyond these "
            "measurements. ISO 1996-2 notes that the combined standard uncertainty of an environmental "
            "noise measurement is typically 1 to 3 dB, so smaller differences should not be treated as "
            "meaningful. The full technical report, with the compliance assessment, the calibration "
            "statement and the data-quality record, is available from the study team.",
            cap))
        story.append(Spacer(1, 0.06 * inch))
        story.append(Paragraph("<b>Sources</b>", cap))
        ref_style = ParagraphStyle(
            'Reference', parent=cap,
            # Hanging indent: the marker sits in the margin and continuation
            # lines align under the text, so the numbers stay scannable.
            leftIndent=14, firstLineIndent=-14, spaceAfter=3,
        )
        for entry in self._limit_reference_entries():
            story.append(Paragraph(entry, ref_style))

        return story

    @staticmethod
    def _limit_reference_entries() -> list[str]:
        """Numbered sources, one entry per item.

        Returned as a list rather than one joined string so each reference is
        rendered as its own paragraph. Run together they were unreadable: a
        reader looking for [3] had to scan a block of prose for the marker.

        Each entry names the specific provision rather than the document, so a
        claim can be checked without reading the whole regulation. All were
        verified against the primary text on 5 August 2026.
        """
        return [
            "[1] WHO, <i>Environmental Noise Guidelines for the European Region</i> (2018), "
            "ISBN 978-92-890-5356-3: road traffic Lden 53 dB and Lnight 45 dB, both graded strong "
            "recommendations. Lden and Lnight are defined in EU Directive 2002/49/EC, Annex I, as "
            "long-term averages over a year.",

            "[2] COMAR 26.02.03.03A(1), Table 2, Maximum Allowable Noise Levels: residential "
            "65 dB(A) by day and 55 dB(A) at night; measured at or within the property line of the "
            "receiving property (.03D(2)); prominent discrete tones and periodic noises must be "
            "5 dB(A) lower (.03A(3)).",

            "[3] COMAR 26.02.03.01B(5) and B(15): daytime is 7 a.m. to 10 p.m., nighttime is "
            "10 p.m. to 7 a.m. B(13) defines equivalent sound level; B(4) defines Ldn.",

            "[4] ISO 1996-1 and ISO 1996-2, <i>Acoustics \u2014 Description, measurement and assessment "
            "of environmental noise</i>: definition of the equivalent continuous sound level, and a "
            "combined measurement uncertainty of the order of 1 to 3 dB.",

            "[5] WHO, <i>Guidelines for Community Noise</i> (Berglund, Lindvall &amp; Schwela, 1999), "
            "ISBN 92-4-154553-4: the decibel scale and its relation to perceived loudness; indoor "
            "bedroom guidelines.",
        ]

    def _reading_word(self, ts: pd.Series) -> str:
        """Name the logging interval, e.g. '1-second'. Measured, never assumed."""
        iv = self._logging_interval_s(ts)
        if not iv:
            return 'logged'
        return f"{iv:.0f}-second" if iv < 60 else f"{iv / 60:.0f}-minute"

    def _resident_definitions_table(self, ts: pd.Series) -> Table:
        """Define every term before the report uses it.

        A reader who meets "L90" or "share of time" for the first time inside a
        results row has to infer the meaning from context, and usually infers
        wrongly — L90 in particular reads as an average unless told otherwise.
        Terms specific to the box plot are defined beside Figure 2 instead of
        here, where they are actually needed.
        """
        styles = self._get_pdf_styles()
        term = ParagraphStyle('DefTerm', parent=styles['BodyText'], fontSize=8.5,
                              leading=10.5, fontName='Helvetica-Bold', spaceAfter=0)
        body = ParagraphStyle('DefBody', parent=styles['BodyText'], fontSize=8.5,
                              leading=10.5, spaceAfter=0)
        head = ParagraphStyle('DefHead', parent=body, fontName='Helvetica-Bold',
                              textColor=colors.white)

        reading = self._reading_word(ts)

        # Wording follows the PI's supplied definitions verbatim wherever they are
        # correct, since that is the register she wants. Three were changed:
        #   * "Leq — the loudness averaged over time": loudness is a perceptual
        #     quantity; Leq is an energy average of sound level.
        #   * L90 described as an average: it is a percentile, which is the
        #     specific misreading this definition exists to prevent.
        #   * Nothing here attributes sound to a source, which the meter cannot do.
        rows = [
            ("Table 1. Definitions", "", True),
            ("A-weighted decibel, dB(A)",
             "Measurement of sound intensity that adjusts raw decibel levels to match the frequency "
             "sensitivity of the human ear (filters out very high and very low frequencies). Every 3 dB is a doubling of sound energy; every 10 dB "
             "increase is about twice as loud in perceived loudness. [5]", False),
            ("LAeq (average level)",
             "The A-weighted equivalent continuous sound level over a period of time: the constant level that would contain the "
             "same sound energy as the actual, varying sound over the same period. An energy average, not "
             "an arithmetic mean. LAeq is used for the black line in Figure 1 and for the legal limits. "
             "[2,4]", False),
            ("L90, Background floor",
             f"The noise level that is exceeded 90% of the time: the steady base beneath passing loud events. "
             f"A percentile of every {reading} reading, not an average. This is a key number, because a "
             f"source that runs all the time raises the floor.", False),
            ("L10",
             "The noise level exceeded 10% of the time, giving the louder end of the range. Quoted with L90 it "
             "shows how wide the spread of noise levels is.", False),
            ("L-Max",
             "The single loudest noise level recorded during the measured period. Not an average.", False),
            ("Lnight",
             "The LAeq across the night only; LAeq averaged from 23:00–07:00. [1]", False),
            ("Lden",
             "The day–evening–night level: the LAeq of the day (07:00–19:00), evening (19:00–23:00) and "
             "night (23:00–07:00) combined over 24 hours, with +5 dB added to the evening and +10 dB to "
             "the night before averaging, because the same sound disturbs more at those hours. [1]", False),
            ("Share of time",
             f"The percentage of {reading} readings inside an averaged period that were at or above a stated "
             f"level. It describes how exposure was distributed throughout the period.", False),
        ]

        data, style = [], [
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#D6DEE7')),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ('TOPPADDING', (0, 0), (-1, -1), 2.5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
            ('LEFTPADDING', (0, 0), (-1, -1), 6),
            ('RIGHTPADDING', (0, 0), (-1, -1), 6),
        ]
        for i, (label, text, is_head) in enumerate(rows):
            if is_head:
                data.append([Paragraph(label, head), ''])
                style += [('SPAN', (0, i), (-1, i)),
                          ('BACKGROUND', (0, i), (-1, i), colors.HexColor('#3D5A80'))]
            else:
                data.append([Paragraph(label, term), Paragraph(text, body)])
                style.append(('BACKGROUND', (0, i), (-1, i), colors.HexColor('#F7F9FB')))

        table = Table(data, colWidths=[1.35 * inch, 5.95 * inch])
        table.setStyle(TableStyle(style))
        return table

    def _resident_result_tables(self, *, ts: pd.Series, leq: pd.Series,
                                lmax_col: str | None) -> list:
        """The results, as three tables: summary, COMAR limits, WHO guidelines.

        Every value comes from the same helpers the technical report uses, so the
        two documents cannot disagree.
        """
        ts_valid = ts.dropna()
        leq_clean = leq.dropna()

        start_str = ts_valid.min().strftime('%d %b %Y') if not ts_valid.empty else 'N/A'
        end_str = ts_valid.max().strftime('%d %b %Y') if not ts_valid.empty else 'N/A'
        completeness = data_completeness_pct(ts_valid, actual_count=len(self.df))

        laeq_v = energetic_mean_db(leq) if not leq_clean.empty else None
        env = (compute_ldn_lden(ts, leq) or {}) if not leq_clean.empty else {}
        exc = (exceedance_levels_db(leq_clean.to_numpy()) or {}) if not leq_clean.empty else {}

        # Day and night averages come from compute_ldn_lden, never a local hour
        # mask: it is the single vetted implementation of the window definitions,
        # and it documents which window each key carries.
        #   LAeq_day = 07:00-22:00   LAeq_night = 22:00-07:00   (Ldn / COMAR)
        #   Lnight   = 23:00-07:00                              (WHO / Lden)
        laeq_day = env.get('LAeq_day')
        laeq_night = env.get('LAeq_night')

        exc_stats = self._exceedance_summary(ts, leq)
        reading = self._reading_word(ts)

        # Per-period series behind the four range statements. Each range is the
        # spread of whole-period averages — one LAeq per day or per night —
        # never the min/max of individual readings, which would just restate
        # L-Min and L-Max. Nights are keyed to the date they began, so a night
        # is never split at midnight into two half-nights.
        day_series = (pd.to_numeric(self.daily_summary.get('Daytime_LAeq'), errors='coerce').dropna()
                      if self.daily_summary is not None else pd.Series(dtype=float))
        night_comar_series = nightly_lnight(ts, leq, night_start=22, night_end=7)
        lden_series = (pd.to_numeric(self.daily_summary.get('Daily_Lden'), errors='coerce').dropna()
                       if self.daily_summary is not None else pd.Series(dtype=float))
        lnight_series = nightly_lnight(ts, leq)
        # Days that actually contain readings, not merely the calendar span. With
        # an exclusion window applied the two differ, and the span alone would
        # overstate coverage.
        days_with_data = int(ts_valid.dt.date.nunique()) if not ts_valid.empty else 0

        def _d(v) -> str:
            try:
                fv = float(v)
                return f"{fv:.1f} dB(A)" if np.isfinite(fv) else "Not available"
            except (TypeError, ValueError):
                return "Not available"

        def _lim(limit_db) -> str:
            """The limit column: the number on its own."""
            return f"<b>{float(limit_db):.0f} dB(A)</b>"

        def _vs(value, limit_db) -> str:
            """The measured column: the value and its distance from the limit.

            No verdict wording — the margin states the position and the reader
            draws the conclusion.
            """
            try:
                fv = float(value)
                if not np.isfinite(fv):
                    return "Not available"
            except (TypeError, ValueError):
                return "Not available"
            diff = fv - float(limit_db)
            if abs(diff) < 0.05:
                return f"{fv:.1f} dB(A), equal to the limit"
            return (f"{fv:.1f} dB(A), which is {abs(diff):.1f} dB "
                    f"{'above' if diff > 0 else 'below'} the limit")

        def _vs_range(value, limit_db, per_period: pd.Series, period_word: str) -> str:
            """Measured column with the average AND its per-period range.

            The average carries the comparison against the limit; the range
            shows the variability behind it, as the spread of the individual
            per-day or per-night averages. The range of raw readings would be
            wrong here — it restates L-Min/L-Max, not the variability of the
            metric in this row.
            """
            base = _vs(value, limit_db)
            per = pd.to_numeric(per_period, errors='coerce').dropna()
            if base == "Not available" or len(per) < 2:
                return base
            return (f"{base}. Individual {period_word} values ranged "
                    f"{per.min():.1f} to {per.max():.1f} dB(A) across "
                    f"{len(per)} {period_word}s")

        def _laeq_with_spread() -> str:
            """LAeq with its day-to-day range.

            LAeq must not carry a standard deviation: it is a logarithmic energy
            average, so an SD of the dB values is an SD of logarithms rather than
            the dispersion of the quantity being averaged, and "x plus or minus y
            dB" reads as a mean and spread that LAeq is not. The honest companion
            is the range of the individual daily LAeq values.
            """
            base = _d(laeq_v)
            if base == "Not available" or self.daily_summary is None:
                return base
            daily = pd.to_numeric(
                self.daily_summary.get('Average_L_EQ_dB'), errors='coerce').dropna()
            if len(daily) < 2:
                return base
            return (f"{base}. Daily values ranged {daily.min():.1f} to "
                    f"{daily.max():.1f} dB(A) across {len(daily)} days")

        def _climate_spread() -> str:
            """L90 to L10, the conventional spread descriptor for noise.

            Both are percentiles of the logged readings, not averages, so neither
            takes a plus-or-minus of its own. Quoted as a pair they are the
            standard statement of how wide the acoustic climate is.
            """
            lo, hi = exc.get('L90'), exc.get('L10')
            try:
                lo_f, hi_f = float(lo), float(hi)
                if not (np.isfinite(lo_f) and np.isfinite(hi_f)):
                    raise ValueError
            except (TypeError, ValueError):
                return "Not available"
            return (f"{lo_f:.1f} to {hi_f:.1f} dB(A), a spread of {hi_f - lo_f:.1f} dB "
                    f"covering the middle 80% of readings")

        def _periods_over() -> str:
            """Counts of individual days and nights past each WHO guideline."""
            bits = []
            if exc_stats['days_over'] is not None:
                bits.append(f"Lden above {WHO_ROAD_LDEN:.0f} dB(A) on "
                            f"{exc_stats['days_over']} of {exc_stats['days_total']} days")
            if exc_stats['nights_over'] is not None:
                bits.append(f"Lnight above {WHO_ROAD_LNIGHT:.0f} dB(A) on "
                            f"{exc_stats['nights_over']} of {exc_stats['nights_total']} nights")
            return "; ".join(bits) if bits else "Not available"

        # Completeness is floored, never rounded up: a 99.96% record contains a
        # real outage, and printing "100%" would erase it.
        if completeness is None:
            completeness_str = "Not available"
        else:
            cp = math.floor(float(completeness) * 10.0) / 10.0
            completeness_str = f"{min(cp, 100.0):.1f}% of expected readings present"

        base_style = self._get_pdf_styles()['BodyText']
        body = ParagraphStyle('ResidentCell', parent=base_style, fontSize=8.5, leading=10.5,
                              spaceBefore=0, spaceAfter=0)
        centred = ParagraphStyle('ResidentLimit', parent=body, alignment=TA_CENTER)
        col_head = ParagraphStyle('ResidentColHead', parent=body,
                                  fontName='Helvetica-Bold', textColor=colors.white)
        col_head_c = ParagraphStyle('ResidentColHeadC', parent=col_head, alignment=TA_CENTER)

        GRID = colors.HexColor('#D6DEE7')
        BANNER = colors.HexColor('#293241')
        ROW_BG = colors.HexColor('#F0F4F8')
        W_LABEL, W_LIMIT, W_VALUE = 2.45 * inch, 1.15 * inch, 3.7 * inch

        def _build(headers: list[str], rows: list[tuple[str, ...]], widths: list[float]) -> Table:
            """One table: a header row, then one row per measurement."""
            cells = [[Paragraph(h, col_head_c if i == 1 and len(headers) == 3 else col_head)
                      for i, h in enumerate(headers)]]
            style = [
                ('GRID', (0, 0), (-1, -1), 0.5, GRID),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ('LEFTPADDING', (0, 0), (-1, -1), 6),
                ('RIGHTPADDING', (0, 0), (-1, -1), 6),
                ('BACKGROUND', (0, 0), (-1, 0), BANNER),
            ]
            for i, row in enumerate(rows, start=1):
                rendered = [Paragraph(f"<b>{row[0]}</b>", body)]
                if len(row) == 3:
                    rendered += [Paragraph(row[1], centred), Paragraph(row[2], body)]
                else:
                    rendered += [Paragraph(row[1], body)]
                cells.append(rendered)
                style.append(('BACKGROUND', (0, i), (-1, i), ROW_BG))
            t = Table(cells, colWidths=widths, repeatRows=1)
            t.setStyle(TableStyle(style))
            return t

        # Three separate tables rather than one with group banners.
        #
        # The summary block has no limit to compare against, so in a shared
        # three-column layout its middle column read "Not applicable" on every
        # row — a column of noise. Splitting it off lets each table carry only
        # the columns it actually uses, and gives the limit column a heading of
        # its own in the two tables where a limit exists.
        summary = _build(
            ['Table 2. Summary of measurements', 'Measured at this home'],
            [
                ("Monitoring period", f"{start_str} to {end_str}, {days_with_data} days with data"),
                ("Data completeness", completeness_str),
                ("LAeq, whole period", _laeq_with_spread()),
                ("Spread of levels, L90 to L10", _climate_spread()),
                ("L-Max, loudest reading", _d(self._lamax_value(lmax_col))),
            ],
            [W_LABEL, W_LIMIT + W_VALUE],
        )

        comar = _build(
            ['Table 3. Maryland COMAR — enforceable legal limits (residential)',
             'Legal limit', 'Measured at this home'],
            [
                ("Daytime LAeq, 07:00–22:00", _lim(MD_RESIDENTIAL_DAY),
                 _vs_range(laeq_day, MD_RESIDENTIAL_DAY, day_series, "day")),
                ("Night-time LAeq, 22:00–07:00", _lim(MD_RESIDENTIAL_NIGHT),
                 _vs_range(laeq_night, MD_RESIDENTIAL_NIGHT, night_comar_series, "night")),
                ("Share of time at or above the limit", "Not applicable",
                 f"Daytime {self._fmt_share(exc_stats['day_pct'])} of the {reading} readings in "
                 f"07:00–22:00; night-time {self._fmt_share(exc_stats['night_pct'])} of the "
                 f"{reading} readings in 22:00–07:00"),
            ],
            [W_LABEL, W_LIMIT, W_VALUE],
        )

        who = _build(
            ['Table 4. WHO 2018 — health-based guidelines (not law)',
             'Guideline', 'Measured at this home'],
            [
                ("Lden, 24 h with evening and night penalties", _lim(WHO_ROAD_LDEN),
                 _vs_range(env.get('Lden'), WHO_ROAD_LDEN, lden_series, "day")),
                ("Lnight, 23:00–07:00", _lim(WHO_ROAD_LNIGHT),
                 _vs_range(env.get('Lnight'), WHO_ROAD_LNIGHT, lnight_series, "night")),
                ("Share of time at or above the Lnight guideline", "Not applicable",
                 f"{self._fmt_share(exc_stats['who_night_pct'])} of the {reading} readings in "
                 f"23:00–07:00"),
                ("Individual periods above the guideline", "Not applicable", _periods_over()),
            ],
            [W_LABEL, W_LIMIT, W_VALUE],
        )

        return [summary, Spacer(1, 0.09 * inch), comar, Spacer(1, 0.09 * inch), who]

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
        return self._display_location_label() or self.device_id or "withheld"

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

        # The heading goes INSIDE the box below, as its first row.
        #
        # As a separate h2 above it, the style's keepWithNext bound the heading
        # to the box, and a KeepTogether cannot split — so the pair jumped to
        # the next page whenever the whole box did not fit, leaving page 1 at
        # 56% on every report. Inside the table the block splits normally, and
        # the heading cannot be orphaned because it is row 0 of the thing it
        # heads.

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

        _heading_style = ParagraphStyle(
            'SummaryHeading', parent=_plain_style,
            fontName='Helvetica-Bold', fontSize=12, leading=15, spaceAfter=4,
            textColor=colors.HexColor('#293241'),
        )

        inner_content = [
            Paragraph("Non-Technical Summary: Noise Exposure &amp; Health Assessment", _heading_style)
        ]

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

        # One background and one border around the whole summary, rather than a
        # box per paragraph — hence a single-cell table.
        #
        # splitInRow lets that cell break across a page boundary. Without it the
        # box is indivisible: at roughly four inches tall it rarely fits in what
        # remains of page 1, so it moved wholesale to page 2 and left the first
        # page 44% empty on every report this platform has produced.
        # One paragraph per ROW, not all of them in one cell.
        #
        # A cell holding a list of flowables is indivisible, so the whole box —
        # about four inches tall — moved to the next page whenever it did not
        # fit in what remained of the current one, leaving page 1 of every
        # report this platform has produced 44% empty. Split between rows it
        # flows like ordinary text, and the background and border still enclose
        # the block because the style spans all rows.
        summary_table = Table(
            [[flowable] for flowable in inner_content],
            colWidths=[9.0 * inch],
            splitByRow=1,
        )
        summary_table.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), colors.HexColor(_box_bg)),
            ('BOX',           (0, 0), (-1, -1), 1.2, colors.HexColor(_box_border)),
            # Tight between rows; the 12pt breathing room belongs at the two
            # ends of the box, not between every paragraph inside it.
            ('TOPPADDING',    (0, 0), (-1, -1), 1),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 1),
            ('TOPPADDING',    (0, 0), (0, 0), 12),
            ('BOTTOMPADDING', (0, -1), (-1, -1), 12),
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
                logger.warning(f"Gap detection failed: {_e}")

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
            "WHO Guidelines for Community Noise (1999), and Maryland COMAR 26.02.03. "
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
                ldn=env.get('Ldn') if isinstance(env, dict) else None,
                laeq=laeq_overall,
                laeq_day=laeq_day,
                laeq_night=laeq_night,
                lamax=lamax,
                environment=self.environment,
            )
        except Exception as _e:
            logger.warning(f"Compliance evaluation failed: {_e}")
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

        # How often the level sat above each limit. The pass/fail rows above
        # report whole-period averages, which say nothing about how the exposure
        # was distributed: a period can pass on its average while spending a
        # meaningful share of its hours above the level.
        exc_stats = self._exceedance_summary(ts, self._get_numeric_series(leq_col))
        story.append(Paragraph("Time and periods above the limits", styles['h2']))
        story.append(Paragraph(
            f"<b>Maryland COMAR.</b> The measured level was at or above the "
            f"{MD_RESIDENTIAL_DAY:.0f} dB(A) day limit for "
            f"<b>{self._fmt_share(exc_stats['day_pct'])}</b> of measured daytime (07:00–22:00), and at or "
            f"above the {MD_RESIDENTIAL_NIGHT:.0f} dB(A) night limit for "
            f"<b>{self._fmt_share(exc_stats['night_pct'])}</b> of measured night-time (22:00–07:00). "
            f"<b>WHO 2018.</b> The measured level was at or above the {WHO_ROAD_LNIGHT:.0f} dB(A) Lnight "
            f"guideline for <b>{self._fmt_share(exc_stats['who_night_pct'])}</b> of the measured WHO night "
            f"window (23:00–07:00). "
            + (f"Lnight exceeded {WHO_ROAD_LNIGHT:.0f} dB(A) on <b>{exc_stats['nights_over']} of "
               f"{exc_stats['nights_total']}</b> nights, each computed over its own 23:00–07:00 window. "
               if exc_stats['nights_over'] is not None else "")
            + (f"Lden exceeded {WHO_ROAD_LDEN:.0f} dB(A) on <b>{exc_stats['days_over']} of "
               f"{exc_stats['days_total']}</b> days. " if exc_stats['days_over'] is not None else ""),
            styles['BodyText']
        ))
        story.append(Paragraph(self._exceedance_note(exc_stats), styles['BodyText']))
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

        # Two half-day blocks side by side.
        #
        # Twenty-four rows in one column filled a whole landscape page top to
        # bottom while leaving more than half its width empty, and pushed
        # Section 7 onto a page of its own that then sat two-thirds blank. Split
        # 00:00-11:00 against 12:00-23:00 the same table is half as tall, uses
        # the width the page actually has, and puts morning beside afternoon
        # where the two can be compared directly.
        header = ["Hour", "Average LAeq (dB(A))", "Min (dB(A))", "Max (dB(A))", "Std Dev (dB)"]
        rows = []
        for idx, row in self.hourly_summary.iterrows():
            hour = int(row.get('Hour', idx))
            rows.append([
                f"{hour:02d}:00",
                self._fmt_db_plain(row.get('Average_L_EQ_dB')),
                self._fmt_db_plain(row.get('Min_L_EQ_dB')),
                self._fmt_db_plain(row.get('Max_L_EQ_dB')),
                self._fmt_float(row.get('Std_Dev'), 2),
            ])

        half = (len(rows) + 1) // 2
        col_widths = [0.7 * inch, 1.35 * inch, 0.95 * inch, 0.95 * inch, 0.95 * inch]
        block_style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3D5A80')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F0F4F8')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#DDDDDD')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('TOPPADDING', (0, 0), (-1, -1), 2.5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2.5),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ])

        blocks = []
        for chunk in (rows[:half], rows[half:]):
            if not chunk:
                continue
            t = Table([header] + chunk, colWidths=col_widths)
            t.setStyle(block_style)
            blocks.append(t)

        if len(blocks) == 2:
            side_by_side = Table([blocks], colWidths=[4.95 * inch, 4.95 * inch])
            side_by_side.setStyle(TableStyle([
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (0, 0), 0),
                ('RIGHTPADDING', (1, 0), (1, 0), 0),
            ]))
            story.append(side_by_side)
        elif blocks:
            story.append(blocks[0])
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
        # Kept: removing it changes no page count (Chart 1 cannot fit under
        # Section 7's table either way) and a clean start for the figures reads
        # better than a heading stranded under an unrelated table.
        story.append(PageBreak())
        story.append(Paragraph("Section 8: Advanced Visualizations", styles['h1']))

        leq = self._get_numeric_series(leq_col)
        if leq.dropna().empty:
            story.append(Paragraph("LEQ stream not available; visualizations cannot be generated.", styles['BodyText']))
            return

        # Provenance now travels in the caption rather than inside the image.
        src = f" <b>Data source:</b> {escape(self._figure_source_label())}."

        lmax = self._get_numeric_series(lmax_col) if (lmax_col and lmax_col in self.df.columns) else None
        lmin = self._get_numeric_series(lmin_col) if (lmin_col and lmin_col in self.df.columns) else None

        # Chart 1: Time Series with WHO band
        story.append(Paragraph("Chart 1: Time Series (LAeq with L-Max/L-Min envelope &amp; WHO limits)", styles['h2']))
        fig1, binning = self._fig_time_series_with_band(ts=ts, leq=leq, lmax=lmax, lmin=lmin)
        if fig1:
            img1 = self._chart_image(fig1, width_inch=9.4, height_inch=4.5)
            if img1:
                story.append(img1)
            else:
                story.append(Paragraph("<i>Unable to render chart image.</i>", styles['BodyText']))
        story.append(Paragraph(
            "<b>What it is:</b> LAeq across the full measurement period, with the L-Min to L-Max envelope. "
            "<b>How it is calculated:</b> "
            + self._chart1_method_sentences(binning, metrics_at="in Section 3") + " "
            "<b>How to read it:</b> The dark line is the sustained acoustic load; the width of the shaded "
            "band is the spread between the quietest and loudest moments within each bin." + src,
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.2 * inch))

        # Chart 2: Box-and-whisker (diurnal volatility)
        story.append(Paragraph("Chart 2: Diurnal Box-and-Whisker (Hourly LEQ Volatility)", styles['h2']))
        fig2 = self._fig_diurnal_box_whisker(ts=ts, leq=leq)
        if fig2:
            img2 = self._chart_image(fig2, width_inch=9.4, height_inch=5.0)
            if img2:
                story.append(img2)
            else:
                story.append(Paragraph("<i>Unable to render chart image.</i>", styles['BodyText']))
        story.append(Paragraph(
            "<b>What it is:</b> The full statistical spread of LAeq at each hour of the day, not a single "
            "average. "
            "<b>How it is calculated:</b> " + self._chart2_method_sentences(
                metrics_at="in Section 3", reading=self._reading_word(ts)) + " "
            "<b>How to read it:</b> A tall box indicates acoustically variable conditions at that hour; "
            "hours with short, low boxes are steady and quiet. What produces either pattern cannot be "
            "determined from sound level data alone." + src,
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.2 * inch))

        # Chart 3: Temporal Heatmap
        story.append(Paragraph("Chart 3: Temporal Heatmap (LAeq Intensity by Date &amp; Hour)", styles['h2']))
        fig3 = self._fig_temporal_heatmap(ts=ts, leq=leq)
        if fig3:
            img3 = self._chart_image(fig3, width_inch=9.0, height_inch=4.8)
            if img3:
                story.append(img3)
            else:
                story.append(Paragraph("<i>Unable to render chart image.</i>", styles['BodyText']))
        story.append(Paragraph(
            "<b>What it is:</b> LAeq for every hour of every day in the record. "
            "<b>How it is calculated:</b> " + self._chart3_method_sentences() + " "
            "<b>How to read it:</b> Read down a column to see how one day changed hour by hour; read across "
            "a row to see whether a given hour behaved the same way from day to day. A consistently bright "
            "row indicates a recurring daily pattern; identifying what produces it requires observations "
            "beyond sound level data." + src,
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.2 * inch))

        # Charts 4 and 5 side by side.
        #
        # Both are square. Stacked one per page on an 11-inch-wide landscape
        # sheet each wasted about four inches of width, and the pair spanned
        # three pages: a full one each, plus an entirely blank sheet between
        # them where a KeepTogether that slightly overran the frame forced a
        # break and then rendered on the page after. Laid out in two columns the
        # pair occupies one page, and neither can trigger that break.
        story.append(PageBreak())
        radar_w = (doc_width := 9.5) / 2 - 0.15   # two columns inside the text block
        radar_h = 4.2

        cap_col = ParagraphStyle('RadarCaption', parent=styles['BodyText'], fontSize=8, leading=10)

        try:
            fig4 = self._fig_diurnal_radar(ts=ts, leq=leq)
            img4 = self._chart_image(fig4, width_inch=radar_w, height_inch=radar_h) if fig4 else None
            cell4 = img4 or Paragraph(
                "<i>Insufficient hourly data to generate the diurnal radar chart.</i>", styles['BodyText'])
        except Exception as e:
            logger.warning(f"Diurnal radar chart failed: {str(e)[:120]}")
            cell4 = Paragraph("<i>Diurnal radar chart could not be generated.</i>", styles['BodyText'])

        cap4 = Paragraph(
            "<b>What it is:</b> A polar radar chart showing the mean LAeq noise level for each of the 24 hours of the day, "
            "plotted clockwise from midnight (00:00) around the circle. "
            "<b>How it is calculated:</b> All measurements falling within each clock hour are energy-averaged (LAeq) across "
            "every day in the dataset. Each spoke is labelled by the hour it starts: the 06:00 spoke is the "
            "06:00–06:59 bin. "
            "<b>How to read it:</b> The shaded sector spans the night period, 22:00 to 07:00 (Maryland COMAR), "
            "and so covers the hourly bins from 22:00 through 06:00. "
            "The polygon shape summarises the site's daily level pattern. "
            "A lopsided polygon with morning and late-afternoon peaks indicates activity concentrated at "
            "those hours; a uniformly expanded polygon indicates a level that is broadly steady around the "
            "clock. What produces either pattern cannot be determined from sound level data alone and "
            "requires corroborating observation. "
            "The dashed rings mark 45 dB and 53 dB for visual orientation only. They are the WHO guideline "
            "VALUES, but those guidelines are defined on Lnight and Lden — a night-long and a 24-hour "
            "penalty-weighted average respectively — so an individual hour rising above a ring is not an "
            "exceedance. The compliance assessment in Section 3 evaluates the correct metrics." + src,
            cap_col
        )

        try:
            fig5 = self._fig_weekly_radar(ts=ts, leq=leq)
        except Exception as _e5:
            logger.warning(f"Weekly radar chart failed: {str(_e5)[:120]}")
            fig5 = None

        if fig5 is not None:
            img5 = self._chart_image(fig5, width_inch=radar_w, height_inch=radar_h)
            cell5 = img5 or Paragraph("<i>Unable to render the weekly radar chart image.</i>",
                                      styles['BodyText'])
        else:
            cell5 = Paragraph(
                "<i>The weekly radar chart requires data spanning at least 3 distinct calendar days "
                "covering multiple days of the week. This dataset does not meet that threshold — "
                "extend the measurement period to see day-of-week noise patterns.</i>",
                styles['BodyText'])

        cap5 = Paragraph(
            "<b>What it is:</b> A 7-spoke radar chart showing the mean LAeq noise level for each day of the week "
            "(Monday–Sunday), with separate traces for daytime (07:00–22:00) and nighttime (22:00–07:00). "
            "<b>How it is calculated:</b> All measurements are grouped by day-of-week and time-of-day period, "
            "then energy-averaged (LAeq) across all occurrences of that combination in the dataset. "
            "<b>How to read it:</b> A wider daytime polygon shows that daytime levels exceed nighttime levels. "
            "Shorter weekend than weekday spokes indicate a level that falls at weekends, and roughly equal "
            "spokes indicate a level that does not vary by day of week. These are descriptions of the measured "
            "pattern; attributing any of them to a particular source requires evidence beyond sound level data." + src,
            cap_col
        )

        col_w = radar_w * inch + 0.15 * inch
        radar_block = Table(
            [[Paragraph("Chart 4: Diurnal Noise Fingerprint (24-Hour Polar Radar)", styles['h2']),
              Paragraph("Chart 5: Weekly Noise Profile (Day-of-Week Radar)", styles['h2'])],
             [cell4, cell5],
             [cap4, cap5]],
            colWidths=[col_w, col_w],
        )
        radar_block.setStyle(TableStyle([
            ('VALIGN', (0, 0), (-1, 0), 'BOTTOM'),
            ('VALIGN', (0, 1), (-1, 1), 'MIDDLE'),
            ('VALIGN', (0, 2), (-1, 2), 'TOP'),
            ('ALIGN', (0, 1), (-1, 1), 'CENTER'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (0, -1), 12),
            ('LEFTPADDING', (1, 0), (1, -1), 12),
            ('TOPPADDING', (0, 0), (-1, -1), 2),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 2),
        ]))
        story.append(radar_block)

    # ============================================================
    # CHART GENERATION
    # ============================================================

    def _fig_time_series_with_band(self, *, ts: pd.Series, leq: pd.Series, lmax: pd.Series | None, lmin: pd.Series | None,
                                   title: str | None = None):
        """Build Chart 1.

        Returns
        -------
        tuple of (plotly.graph_objects.Figure or None, TimeSeriesBinning)
            The figure and a record of how it was binned, so the caption states
            what was actually done rather than reciting the adaptive ladder.
            The binning is empty when no figure could be built.
        """
        df = pd.DataFrame({'ts': ts, 'leq': leq})
        if lmax is not None:
            df['lmax'] = lmax
        if lmin is not None:
            df['lmin'] = lmin

        df = df.dropna(subset=['ts', 'leq']).sort_values('ts')
        if df.empty:
            return None, TimeSeriesBinning()

        # Multi-week reports become unreadable if every raw point is plotted.
        # Resample adaptively to preserve the envelope while keeping the LEQ trace legible.
        span = df['ts'].iloc[-1] - df['ts'].iloc[0]
        freq, bin_label = ts_resample_rule(span)

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
        # Window chosen from the diurnal cycle, not from a fixed bin count.
        bin_interval = pd.Timedelta(freq if freq[0].isdigit() else f'1{freq}')
        roll_window, roll_span_label = ts_rolling_window(span, bin_interval)
        roll_w = max(3, int(round(roll_window / bin_interval)))
        roll_w = min(roll_w, max(1, len(df_plot)))

        # CENTRED. A trailing window reports the median of the preceding N bins
        # at the position of the last one, which shifts the whole trend line
        # forward by half the window — on a 24-hour window that placed every
        # feature 12 hours later than it occurred. Centring puts the median at
        # the middle of the data it summarises.
        #
        # min_periods of half the window keeps the two ends honest: with
        # min_periods=1 the first and last points were medians of a single bin,
        # so the trend line ran out to the edges carrying no smoothing at all.
        rolling_1h = df_plot['leq'].rolling(
            window=roll_w, center=True, min_periods=max(1, roll_w // 2)).median()
        rolling_1h = rolling_1h.where(df_plot['leq'].notna())
        roll_label = f'Rolling median ({roll_span_label}, centered)'

        fig = go.Figure()

        # Draw the envelope first so the LAeq trace stays visually dominant.
        if 'lmax' in df_plot.columns and 'lmin' in df_plot.columns:
            # Shade the L-Max/L-Min band, one filled polygon per contiguous run
            # of measured bins.
            #
            # A single 'tonexty' fill across the whole series cannot be used:
            # plotly pairs the two traces positionally, and the NaN gaps that
            # must be present (so outages are not drawn as data) break that
            # pairing — the earlier attempt rendered a triangular wedge spanning
            # several days of no measurement. Splitting on the gaps and filling
            # each run as its own closed polygon (up the L-Max side, back down
            # the L-Min side) gives the shaded envelope with the outages left
            # genuinely blank.
            measured = df_plot['lmax'].notna() & df_plot['lmin'].notna()
            run_id = (measured != measured.shift()).cumsum()
            first_band = True
            for _, seg in df_plot[measured].groupby(run_id[measured], sort=False):
                if len(seg) < 2:
                    continue
                seg_x = list(seg.index)
                fig.add_trace(go.Scatter(
                    x=seg_x + seg_x[::-1],
                    y=list(seg['lmax']) + list(seg['lmin'])[::-1],
                    mode='lines',
                    line=dict(color='rgba(0,0,0,0)', width=0),
                    fill='toself',
                    fillcolor='rgba(61,90,128,0.13)',
                    name='Envelope (L-Min–L-Max)',
                    legendgroup='envelope',
                    showlegend=first_band,
                    hoverinfo='skip',
                ))
                first_band = False

            fig.add_trace(go.Scatter(
                x=df_plot.index,
                y=df_plot['lmax'],
                mode='lines',
                name='L-Max',
                line=dict(color='rgba(244,162,97,0.55)', width=1.5, dash='dot'),
                connectgaps=False,
                hoverinfo='skip',
                showlegend=False,
            ))
            fig.add_trace(go.Scatter(
                x=df_plot.index,
                y=df_plot['lmin'],
                mode='lines',
                name='L-Min',
                line=dict(color='rgba(42,157,143,0.55)', width=1.5, dash='dot'),
                connectgaps=False,
                hoverinfo='skip',
                showlegend=False,
            ))
            # Full-strength legend proxies: the faded dotted plot style is
            # near-invisible at swatch size (PI comment), but the plotted lines
            # must stay subtle so they don't compete with the LAeq trace.
            for proxy_name, proxy_color in (('L-Max', 'rgb(230,126,34)'),
                                            ('L-Min', 'rgb(26,148,133)')):
                fig.add_trace(go.Scatter(
                    x=[None], y=[None], mode='lines', name=proxy_name,
                    line=dict(color=proxy_color, width=2.5, dash='dot'),
                    hoverinfo='skip', showlegend=True,
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
                name=roll_label,
                line=dict(color='#8E44AD', width=2.5),
                connectgaps=False,
                hovertemplate='%{x|%d %b %Y %H:%M}<br>Rolling median: %{y:.1f} dB(A)<extra></extra>',
            ))

        # Fixed orientation levels. Both lines previously read "WHO 24-Hr
        # Threshold", which was wrong twice: 45 dB is the Lnight guideline, not a
        # 24-hour one, and neither guideline applies to the LAeq trace plotted
        # here — Lden and Lnight are penalty-weighted long-term averages.
        #
        # Labelled in the right margin rather than inside the frame: an in-plot
        # label sat on top of the traces, and on a dense multi-week record it was
        # unreadable against them.
        for ref_y, ref_alpha in ((53.0, 0.95), (45.0, 0.7)):
            fig.add_hline(y=ref_y, line_dash='dash',
                          line_color=f'rgba(231,111,81,{ref_alpha})')
            fig.add_annotation(
                x=1.012, xref='paper', xanchor='left',
                y=ref_y, yref='y', yanchor='middle',
                text=f'{ref_y:.0f} dB',
                showarrow=False, align='left',
                # Named so _size_fig_for_print can widen the right margin to fit
                # whatever width this text renders at, rather than trusting a
                # hand-set margin that clipped the "dB" when the type grew.
                name=self.REF_LABEL_ANNOTATION_NAME,
                font=dict(size=9, color='rgba(196,78,52,1)'),
            )

        # Stable y-axis window.
        #
        # Autoscaling put the reference lines somewhere different in every
        # report, and on a loud record the 45 dB line was pushed onto the very
        # bottom edge of the frame where it could barely be seen. Anchoring the
        # window means both lines land in the same place every time, so two
        # reports can be held side by side and compared by eye. The window only
        # ever grows: data outside it extends the axis rather than being clipped.
        plotted = [df_plot['leq']]
        for extra in ('lmax', 'lmin'):
            if extra in df_plot.columns:
                plotted.append(df_plot[extra])
        observed = pd.concat(plotted).dropna()
        y_lo, y_hi = Y_AXIS_BASE_WINDOW_DB
        if not observed.empty:
            y_lo = min(y_lo, math.floor(float(observed.min())) - 2.0)
            y_hi = max(y_hi, math.ceil(float(observed.max())) + 2.0)

        tickformat = '%d %b\n%H:%M' if span <= pd.Timedelta(days=3) else '%d %b'
        chart_title = title or (
            f'Chart 1: Time Series — LAeq with L-Max/L-Min envelope ({bin_label} bins)'
        )
        fig.update_layout(
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
                range=[y_lo, y_hi],
                dtick=10,
            ),
            legend=dict(
                orientation='h',
                yanchor='bottom',
                y=1.015,
                xanchor='center',
                x=0.5,
                bgcolor='rgba(255,255,255,0.85)',
                bordercolor='rgba(0,0,0,0.08)',
                borderwidth=1,
            ),
            title=dict(y=0.975, yanchor='top', x=0.5, xanchor='center'),
            # Right margin holds the two reference-line labels; the top carries
            # the title above the legend; the bottom clears the x-axis title AND
            # the source caption beneath it.
            margin=dict(l=62, r=84, t=150, b=90),
            autosize=True,
            height=470,
            hovermode='x unified',
            plot_bgcolor='white',
            paper_bgcolor='white',
        )
        self._apply_chart_typography(fig, title=chart_title)
        self._add_source_annotation(fig, y=-0.20)
        return fig, TimeSeriesBinning(freq=freq, bin_label=bin_label, window_label=roll_span_label)

    def _fig_diurnal_box_whisker(self, *, ts: pd.Series, leq: pd.Series, title: str | None = None):
        """Generate a diurnal box-and-whisker chart for hourly LEQ volatility.

        Day (07:00–22:00) and night (22:00–07:00) hours are coloured apart, the
        night hours shaded, and the COMAR residential limit for each period drawn
        across only the hours that period covers — the limits are defined on the
        LAeq of the whole period, so a line spanning all 24 hours would assert a
        limit over hours it does not govern.
        """
        try:
            df = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
            if df.empty:
                return None
            df['hour'] = df['ts'].dt.hour
            fig = go.Figure()

            # Maryland COMAR day period is 07:00–22:00, i.e. the hourly bins
            # labelled 07:00 through 21:00. Everything else is night.
            day_hours = set(range(7, 22))
            # Colourblind-safe pair (Okabe-Ito orange / report slate blue).
            DAY_LINE, DAY_FILL = '#B37700', 'rgba(230,159,0,0.35)'
            NIGHT_LINE, NIGHT_FILL = '#293241', 'rgba(61,90,128,0.45)'

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
                is_day = h in day_hours
                box_line = DAY_LINE if is_day else NIGHT_LINE
                box_fill = DAY_FILL if is_day else NIGHT_FILL

                # Box with analytically computed stats (no raw data bulk)
                fig.add_trace(go.Box(
                    q1=[q1], median=[med], q3=[q3],
                    lowerfence=[lf], upperfence=[uf],
                    x=[label],
                    name=label,
                    marker=dict(color=box_line),
                    line=dict(color=box_line, width=1.5),
                    fillcolor=box_fill,
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
                        marker=dict(color=box_line, size=3, opacity=0.35),
                        showlegend=False,
                        hovertemplate='Hour: %{x}<br>LEQ: %{y:.1f} dB(A) (beyond 1.5×IQR)<extra></extra>',
                    ))

            # Night shading and period limits.
            #
            # On a categorical axis plotly places category i at x = i, so the
            # band edges sit on the half-integers between categories. Night is
            # two spans because it wraps midnight: 00:00–06:00 and 22:00–23:00.
            NIGHT_SPANS = ((-0.5, 6.5), (21.5, 23.5))
            DAY_SPAN = (6.5, 21.5)
            for x0, x1 in NIGHT_SPANS:
                fig.add_vrect(x0=x0, x1=x1, fillcolor='rgba(44,62,80,0.07)',
                              line_width=0, layer='below')

            # COMAR residential limits, each drawn only across the hours its
            # period covers. Imported from the compliance matrix so the resident
            # report cannot drift from the technical report's limit values.
            limit_spans = [
                (MD_RESIDENTIAL_DAY, [DAY_SPAN], 'rgba(179,119,0,0.95)',
                 f'COMAR day limit {MD_RESIDENTIAL_DAY:.0f} dB(A), 07:00–22:00'),
                (MD_RESIDENTIAL_NIGHT, list(NIGHT_SPANS), 'rgba(41,50,65,0.95)',
                 f'COMAR night limit {MD_RESIDENTIAL_NIGHT:.0f} dB(A), 22:00–07:00'),
            ]
            # Drawn as shapes, not traces. On a category axis a Scatter with
            # numeric x is not positioned at those category indices — plotly
            # appends the numbers as new categories, which put the limit lines
            # in empty space to the right of 23:00. Shapes take the numeric
            # coordinate directly.
            for limit_db, spans, colour, _legend_name in limit_spans:
                for x0, x1 in spans:
                    fig.add_shape(type='line', xref='x', yref='y',
                                  x0=x0, x1=x1, y0=limit_db, y1=limit_db,
                                  line=dict(color=colour, width=2, dash='dash'),
                                  layer='above')

            # Legend keys. Each hour is its own Box trace, and the limits are
            # shapes, so neither can carry a legend entry — these empty traces
            # do it instead.
            legend_keys = [
                ('Daytime hours (07:00–22:00)', DAY_LINE, 'square', None),
                ('Night hours (22:00–07:00)', NIGHT_LINE, 'square', None),
            ] + [(name, colour, None, 'dash') for _l, _s, colour, name in limit_spans]
            for legend_name, colour, symbol, dash in legend_keys:
                fig.add_trace(go.Scatter(
                    x=[None], y=[None],
                    mode='markers' if symbol else 'lines',
                    marker=dict(color=colour, size=11, symbol=symbol) if symbol else None,
                    line=dict(color=colour, width=2, dash=dash) if dash else None,
                    name=legend_name, showlegend=True, hoverinfo='skip',
                ))

            # Same anchored window as Chart 1, extended if the data or the
            # limit lines fall outside it, so both COMAR lines are always in frame.
            leq_all = pd.to_numeric(df['leq'], errors='coerce').dropna()
            y_lo, y_hi = Y_AXIS_BASE_WINDOW_DB
            if not leq_all.empty:
                y_lo = min(y_lo, math.floor(float(leq_all.min())) - 2.0)
                y_hi = max(y_hi, math.ceil(float(leq_all.max())) + 2.0)
            y_lo = min(y_lo, MD_RESIDENTIAL_NIGHT - 5.0)
            y_hi = max(y_hi, MD_RESIDENTIAL_DAY + 5.0)

            chart2_title = title or 'Chart 2: Diurnal Box-and-Whisker (Hourly LEQ Volatility)'
            fig.update_layout(
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
                    title='LAeq (dB(A))',
                    showgrid=True,
                    gridcolor='rgba(0,0,0,0.08)',
                    zeroline=False,
                    range=[y_lo, y_hi],
                    dtick=10,
                ),
                title=dict(y=0.975, yanchor='top', x=0.5, xanchor='center'),
                legend=dict(
                    orientation='h', yanchor='bottom', y=1.015,
                    xanchor='center', x=0.5,
                    bgcolor='rgba(255,255,255,0.85)',
                    bordercolor='rgba(0,0,0,0.08)', borderwidth=1,
                ),
                # Top margin carries the title plus a two-row legend.
                margin=dict(l=70, r=25, t=170, b=105),
                autosize=True,
                height=620,
                paper_bgcolor='white',
                plot_bgcolor='white',
            )
            self._apply_chart_typography(fig, title=chart2_title)
            self._add_source_annotation(fig, y=-0.26)
            return fig
        except Exception as e:
            logger.warning(f"Box plot failed: {str(e)[:100]}")
            return None

    def _fig_temporal_heatmap(self, *, ts: pd.Series, leq: pd.Series, title: str | None = None):
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
            xaxis_title='Date', yaxis_title='Hour of Day',
            margin=dict(l=80, r=20, t=85, b=85), height=520,
            plot_bgcolor='white',
            paper_bgcolor='white',
        )
        # Colourbar title only; its font size is set with everything else by
        # _scale_fig_fonts once the print box is known.
        fig.update_traces(colorbar=dict(title=dict(text='LAeq dB(A)')),
                          selector=dict(type='heatmap'))
        self._apply_chart_typography(
            fig, title=title or 'Chart 3: Temporal Heatmap (LAeq intensity by date & hour)')
        self._add_source_annotation(fig, y=-0.22)

        # Tick labels are drawn from the same string list used as y, so each tick
        # lands exactly on its cell centre. Every second hour is labelled: at the
        # type size this report prints at, 24 labels overlap into an unreadable
        # block in the shorter (resident) layout.
        y_ticks = y[::2]
        fig.update_yaxes(
            tickmode='array',
            tickvals=y_ticks,
            ticktext=y_ticks,
            automargin=True,
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

        # Night sector shading, 22:00–07:00 (Maryland COMAR night period).
        #
        # Built as a true centre-anchored wedge: down the 22:00 radius, round the
        # rim to the 07:00 spoke, back down the 07:00 radius. The previous
        # version listed only the 22:00–06:00 spokes at constant r_max and let
        # fill='toself' close the shape, which produced a circular segment cut
        # off by a chord — the shading visibly ended at 06:00 while the legend
        # said 07:00. The rim must reach the 07:00 spoke because the 06:00 hour
        # bin covers 06:00–06:59 and is a night hour.
        night_labels = [f"{h % 24:02d}:00" for h in range(22, 32)]   # 22:00 … 07:00
        fig.add_trace(go.Scatterpolar(
            r=[r_min] + [r_max] * len(night_labels) + [r_min],
            theta=[night_labels[0]] + night_labels + [night_labels[-1]],
            mode='lines',
            fill='toself',
            fillcolor='rgba(44,62,80,0.07)',
            line=dict(color='rgba(0,0,0,0)', width=0),
            name='Night (22:00–07:00)',
            showlegend=True,
            hoverinfo='skip',
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
                    # Fixed 5 dB step, drawn horizontally. Letting plotly choose
                    # produced a tick every 2 dB, which at print type size it
                    # then rotated upright and packed into an unreadable column
                    # through the middle of the chart.
                    dtick=5,
                    tickangle=0,
                    gridcolor='rgba(180,180,180,0.5)',
                    linecolor='rgba(150,150,150,0.6)',
                ),
                angularaxis=dict(
                    direction='clockwise',
                    gridcolor='rgba(180,180,180,0.4)',
                ),
                bgcolor='rgba(248,249,250,1)',
            ),
            title=dict(x=0.5),
            legend=dict(orientation='h', yanchor='bottom', y=-0.15, xanchor='center', x=0.5),
            paper_bgcolor='white',
            width=700,
            height=700,
            margin=dict(l=60, r=60, t=90, b=95),
        )
        self._apply_chart_typography(fig, title='Mean LAeq by Hour of Day')
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
                    dtick=5,
                    tickangle=0,
                    gridcolor='rgba(180,180,180,0.5)',
                    linecolor='rgba(150,150,150,0.6)',
                ),
                angularaxis=dict(
                    direction='clockwise',
                    gridcolor='rgba(180,180,180,0.4)',
                ),
                bgcolor='rgba(248,249,250,1)',
            ),
            title=dict(x=0.5),
            legend=dict(orientation='h', yanchor='bottom', y=-0.18, xanchor='center', x=0.5),
            paper_bgcolor='white',
            width=700,
            height=700,
            margin=dict(l=60, r=60, t=90, b=115),
        )
        self._apply_chart_typography(fig, title='Weekly Profile — Daytime vs Nighttime LAeq')
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
            # Keep the PNG on the flowable. ReportLab replaces a BytesIO passed
            # as `filename` with str(buffer) — the repr, not the data — so the
            # bytes are otherwise unrecoverable, and the Word renderer needs
            # them to embed the same image the PDF shows.
            img._png_bytes = img_bytes
            aspect = img.imageHeight / img.imageWidth if img.imageWidth > 0 else 1
            img.drawWidth  = width_inch * inch
            img.drawHeight = (width_inch * inch) * aspect
            if img.drawHeight > height_inch * inch:
                img.drawHeight = height_inch * inch
                img.drawWidth  = (height_inch * inch) / aspect
            return img
        except Exception as e:
            logger.warning(f"Chart rendering failed: {str(e)[:120]}")
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
