# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
import pandas as pd
import numpy as np
from datetime import datetime
import math
import os
import re
import logging
import plotly.graph_objects as go
from analysis.noise_analyzer import NoiseAnalyzer
from analysis.iso_epa_standards import StandardsAnalyzer
from analysis.acoustics import (compute_ldn_lden, energetic_mean_db,
                                exceedance_levels_db, energy_concentration,
                                time_above_level_by_period, time_above_level_in_window,
                                LDEN_DEFAULT)
from analysis.gap_detector import (
    detect_gaps,
    gap_report_to_dict,
    _modal_interval_seconds,
    floor_pct,
    format_duration,
)
from analysis.docx_from_story import render_story_to_docx, PageTrackingDocTemplate
from analysis.periods import (daily_summary, hourly_summary, nightly_values, WHO_NIGHT, COMAR_NIGHT,
                              COMPLETE_COVERAGE_PCT)
from analysis.clock import (DEFAULT_CLOCK, FOLD_COLUMN, describe_time_basis, ordering_key,
                            zone_abbreviations)
from analysis.weather_screen import REASON_LABELS as WEATHER_REASON_LABELS
from analysis.compliance_matrix import (evaluate_compliance,
                                        MD_RESIDENTIAL_DAY, MD_RESIDENTIAL_NIGHT,
                                        WHO_ROAD_LDEN, WHO_ROAD_LNIGHT)
import io
from typing import NamedTuple
from reportlab.platypus import Paragraph, Spacer, Image, PageBreak, Table, TableStyle, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from xml.sax.saxutils import escape

logger = logging.getLogger(__name__)


# Instrument provenance statement.
DEFAULT_INSTRUMENT_NOTE = (
    "Not provided. The data file carries no instrument metadata and none was entered, "
    "so the instrument model, serial number and calibration are not stated in this report."
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


_HOME_LABEL_RE = re.compile(r'^(home|house)\s*([A-Za-z])$', re.IGNORECASE)


def is_safe_label(value):
    """True when ``value`` is a study code rather than a personal identifier."""
    v = str(value or '').strip()
    return bool(v) and bool(_SAFE_LABEL_RE.match(v))


# Averaging intervals offered for the Chart 1 time history.
_TS_BIN_CHOICES: tuple[tuple[pd.Timedelta, str, str], ...] = (
    (pd.Timedelta(minutes=15), '15min', '15-minute'),
    (pd.Timedelta(hours=1),    '1h',    '1-hour'),
    (pd.Timedelta(days=1),     '1D',    '24-hour'),
)

_TS_MAX_POINTS = 1500


def ts_resample_rule(span):
    """Choose the Chart 1 averaging interval for a record of length ``span``."""
    for interval, freq, label in _TS_BIN_CHOICES:
        if span / interval <= _TS_MAX_POINTS:
            return freq, label
    return _TS_BIN_CHOICES[-1][1], _TS_BIN_CHOICES[-1][2]


def ts_rolling_window(span, bin_interval):
    """Choose the smoothing window for the Chart 1 trend line."""
    if bin_interval >= pd.Timedelta(days=1):
        return pd.Timedelta(days=7), '7-day'
    if span < pd.Timedelta(days=3):
        return pd.Timedelta(hours=1), '1-hour'
    return pd.Timedelta(days=1), '24-hour'


# Baseline y-axis window for the Chart 1 time series, in dB(A).
Y_AXIS_BASE_WINDOW_DB: tuple[float, float] = (30.0, 90.0)

# font sizes are points at final print size, not canvas pixels
CHART_TITLE_PT = 13.0
CHART_LEGEND_PT = 9.5
CHART_AXIS_TITLE_PT = 10.5
CHART_TICK_PT = 9.0
CHART_ANNOTATION_PT = 9.0
CHART_FONT_COLOR = '#1F2933'


def ts_resample_ladder_text():
    """State the averaging interval as the record lengths it applies to."""
    parts = []
    for i, (interval, freq, label) in enumerate(_TS_BIN_CHOICES):
        if i == len(_TS_BIN_CHOICES) - 1:
            parts.append(f"{label} beyond that")
            break
        max_days = int((interval * _TS_MAX_POINTS).total_seconds() // 86400)
        parts.append(f"{label} for records up to {max_days} days")
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


class TimeSeriesBinning(NamedTuple):
    """How Chart 1 was binned, so the caption can state it rather than guess."""
    freq: str = ''
    bin_label: str = ''
    window_label: str = ''


def _days_text(days):
    d = float(days)
    if abs(d - round(d)) < 0.05:
        n = int(round(d))
        return f"{n} day{'s' if n != 1 else ''}"
    return f"{d:.1f} days"


def _excluded_text(x):
    a, b = pd.Timestamp(x['start']), pd.Timestamp(x['end'])
    return f"{a:%d %b %Y %H:%M:%S} to {b:%d %b %Y %H:%M:%S}" if a.date() != b.date() else \
        f"{a:%d %b %Y %H:%M:%S} to {b:%H:%M:%S}"


def deidentify_label(value, fallback):
    """Return ``(label, was_redacted)`` for a user-supplied identifier."""
    return (str(value).strip(), False) if is_safe_label(value) else (fallback, True)


class ChartExportUnavailable(RuntimeError):
    """The server cannot rasterise Plotly figures, so a report would have no figures."""


_CHART_EXPORT_OK = False


def ensure_chart_export():
    """Render a one-point figure; raise ``ChartExportUnavailable`` if that fails."""
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
    """Publication-grade environmental noise analysis report."""

    def __init__(self, df, filepath, analysis=None, standards=None, daily_summary=None, hourly_summary=None,
                 device_id='', source_files=None, merge_gap_report=None,
                 custom_section_heading='', custom_section_body='', environment='outdoor',
                 deidentify=True, instrument_note=None,
                 weather_screen=None):
        """Initialize report generator with ONLY the uploaded data."""
        self.df = df.copy()
        self.filepath = filepath
        self.deidentify = bool(deidentify)
        self.instrument_note = (instrument_note or DEFAULT_INSTRUMENT_NOTE).strip()

        raw_device_id = str(device_id or '').strip()
        raw_sources = [str(f) for f in (source_files or [])]
        self.redactions = []

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
        self.merge_gap_report = merge_gap_report
        self.custom_section_heading = str(custom_section_heading or '').strip()
        self.custom_section_body = str(custom_section_body or '').strip()
        self.environment = str(environment or 'outdoor').strip().lower()
        self.weather_screen = weather_screen or None
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

    @staticmethod
    def generate_plain_english_summary(
        *,
        laeq,
        lden=None,
        lnight=None,
        laeq_day=None,
        laeq_night=None,
        laeq_min=None,
        laeq_max=None,
        l10=None,
        l90=None,
        start_date='',
        end_date='',
        duration_label='',
        data_completeness_pct=None,
        n_days=0,
        n_gaps=0,
        total_gap_hours=None,
        environment='outdoor',
        truncation_warning=False,
        timestamps_unusable=False,
        lamax=None,
        logging_interval_s=None,
        energy_dominance=None,
        time_basis='',
        clock_change_dates=None,
        excluded=None,
    ):
        """Returns a structured multi-paragraph plain-English summary."""
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

        int_s = _v(logging_interval_s)
        if int_s is None or int_s <= 0:
            interval_word = "logged-interval"
        elif int_s < 1:
            interval_word = f"{int_s:.2f}-second"
        elif int_s < 60:
            interval_word = f"{int_s:.0f}-second"
        else:
            interval_word = f"{int_s / 60.0:.0f}-minute"

        laeq_v = _v(laeq)
        if laeq_v is None:
            return (
                "A plain-English summary could not be generated because the average noise level "
                "(LAeq) could not be computed. Please check that the dataset contains valid "
                "numeric measurements."
            )

        # unusable timestamps
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

        day_word = (_days_text(n_days) if n_days and n_days > 0
                    else duration_label or "the measurement period")

        # state continuity once: no gaps, or completeness and interruptions
        completeness_note = ""
        is_continuous = True
        missing_s = float(total_gap_hours or 0.0) * 3600.0
        cp = None
        if data_completeness_pct is not None:
            try:
                cp = math.floor(float(data_completeness_pct) * 100.0) / 100.0
            except (TypeError, ValueError):
                cp = None
        if n_gaps:
            is_continuous = False
            ng = int(n_gaps)
            completeness_note = (
                f" {ng} interruption{'s' if ng != 1 else ''} with no readings "
                f"{'was' if ng == 1 else 'were'} detected, totalling {format_duration(missing_s)}"
                + (f"; data completeness was {cp:.2f}%." if cp is not None else ".")
            )
            if cp is not None and cp < 90.0:
                completeness_note += " Averages may under- or over-estimate exposure over the whole period."
        elif cp is not None and cp < 100.0:
            is_continuous = False
            completeness_note = f" Data completeness was {cp:.2f}%."
        elif cp is not None:
            completeness_note = " The record has no gaps."

        if excluded:
            is_continuous = False
            completeness_note += (
                f" {len(excluded)} window{'s' if len(excluded) != 1 else ''} excluded by the analyst "
                f"({'; '.join(excluded)}) {'is' if len(excluded) == 1 else 'are'} not part of the analysis."
            )
        for d in (clock_change_dates or []):
            completeness_note += (
                f" Daylight saving time began on {d}: clocks moved forward one hour, and the "
                f"skipped hour is not counted as missing data."
            )
        if time_basis:
            completeness_note += f" All times are {time_basis}."

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

        # noise level
        if n_days and n_days > 0:
            dt = _days_text(n_days)
            period_label = "24-hour" if dt == "1 day" else dt.replace(" days", "-day")
        else:
            period_label = "whole-record"
        level_sentence = (
            f"The {period_label} energy-average level (LAeq) was {_f(laeq_v)} dB(A)."
        )

        day_night_sentence = ""
        dv = _v(laeq_day)
        nv = _v(laeq_night)
        if dv is not None and nv is not None:
            diff = dv - nv
            # describe the pattern only, a meter can't say what caused it
            base = (f" Daytime levels (07:00–22:00) averaged {_f(dv)} dB(A) and nighttime "
                    f"levels (22:00–07:00) averaged {_f(nv)} dB(A).")
            if diff >= 0:
                day_night_sentence = base + f" Daytime exceeded nighttime by {_f(diff)} dB."
            else:
                day_night_sentence = base + f" Nighttime exceeded daytime by {_f(-diff)} dB."

        # Name the stream the peak came from.
        peak_sentence = ""
        lamax_v = _v(lamax)
        if lamax_v is not None:
            peak_sentence = (f" The highest single sound level recorded (L-Max) was "
                             f"{_f(lamax_v)} dB(A).")
        elif _v(laeq_max) is not None:
            peak_sentence = (f" The highest {interval_word} average level recorded (LEQ) was "
                             f"{_f(_v(laeq_max))} dB(A). Instantaneous peaks within those intervals "
                             f"were higher; an L-Max stream is required to report them.")

        # Disclose when the average rests on a handful of samples.
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

        # 3. Variability paragraph
        para3 = ""
        l10_v = _v(l10)
        l90_v = _v(l90)
        if l10_v is not None and l90_v is not None:
            para3 = (
                f"The level exceeded 10% of the time (L10) was {_f(l10_v)} dB(A) and the level "
                f"exceeded 90% of the time (L90) was {_f(l90_v)} dB(A), a spread of "
                f"{_f(l10_v - l90_v)} dB."
            )

        # 4. WHO compliance bullet points
        WHO_LDEN_LIMIT   = 53.0   # WHO 2018, Table 1 (road traffic, Lden)
        WHO_LNIGHT_LIMIT = 45.0   # WHO 2018, Table 1 (road traffic, Lnight)
        WHO_LOAEL_NIGHT  = 40.0

        bullets = []

        lden_v = _v(lden)
        if lden_v is not None:
            if lden_v > WHO_LDEN_LIMIT:
                excess = lden_v - WHO_LDEN_LIMIT
                bullets.append(
                    f"24-hour weighted average (Lden): {_f(lden_v)} dB(A). "
                    f"Above the WHO 2018 road-traffic guideline value of {WHO_LDEN_LIMIT} dB(A) "
                    f"by {_f(excess)} dB."
                )
            else:
                bullets.append(
                    f"24-hour weighted average (Lden): {_f(lden_v)} dB(A). "
                    f"At or below the WHO 2018 road-traffic guideline value of {WHO_LDEN_LIMIT} dB(A)."
                )

        lnight_v = _v(lnight)
        if lnight_v is not None:
            if lnight_v > WHO_LNIGHT_LIMIT:
                excess = lnight_v - WHO_LNIGHT_LIMIT
                bullets.append(
                    f"Nighttime level (Lnight, 23:00–07:00): {_f(lnight_v)} dB(A). "
                    f"Above the WHO 2018 road-traffic guideline value of {WHO_LNIGHT_LIMIT} dB(A) "
                    f"by {_f(excess)} dB."
                )
            elif lnight_v > WHO_LOAEL_NIGHT:
                bullets.append(
                    f"Nighttime level (Lnight, 23:00–07:00): {_f(lnight_v)} dB(A). "
                    f"At or below the WHO 2018 road-traffic guideline value of {WHO_LNIGHT_LIMIT} dB(A) but above the "
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
            # No Lden/Lnight available.
            note = ("Lden and Lnight could not be computed for this dataset because usable "
                    "timestamps were unavailable. WHO 2018 guidelines are defined on Lden and "
                    "Lnight, which apply +5 dB (evening) and +10 dB (night) penalties, so they "
                    "cannot be inferred from LAeq alone and no comparison is made here.")
            bullets.append(f"Whole-record average level (LAeq): {_f(laeq_v)} dB(A). {note}")

        bullet_lines = "\n".join(f"  • {b}" for b in bullets)
        para4 = (f"Comparison with the WHO 2018 road-traffic guideline values "
                 f"(total measured level, all sources):\n{bullet_lines}")

        # Assemble with paragraph separators
        parts = [para1, para2]
        if para3:
            parts.append(para3)
        parts.append(para4)
        return "\n\n".join(parts)

    def _compute_summaries(self):
        """Compute daily and hourly summaries DIRECTLY from the uploaded dataset."""
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

        tz = self.clock_zone
        interval = _modal_interval_seconds(df_data['ts'])
        daily = daily_summary(df_data['ts'], df_data['leq'], tz, interval)
        if not daily.empty:
            self.daily_summary = daily
            logger.info(f"Computed daily summary: {len(daily)} days")
        hourly = hourly_summary(df_data['ts'], df_data['leq'])
        if not hourly.empty:
            self.hourly_summary = hourly
            logger.info(f"Computed hourly summary: {len(hourly)} hours")

    # partial first and last periods count only if half was measured
    MIN_PERIOD_COVERAGE_PCT = 50.0

    def _per_night(self, ts, leq, window):
        data = pd.DataFrame({'ts': pd.to_datetime(ts, errors='coerce'),
                             'leq': pd.to_numeric(leq, errors='coerce')}).dropna()
        if data.empty:
            return pd.Series(dtype=float)
        nv = nightly_values(data['ts'], data['leq'], window, self.clock_zone,
                            _modal_interval_seconds(data['ts']))
        nv = nv[nv['coverage_pct'] >= self.MIN_PERIOD_COVERAGE_PCT]
        return nv['laeq'].dropna()

    def _per_day_lden(self):
        ds = self.daily_summary
        if ds is None or 'Daily_Lden' not in ds.columns:
            return pd.Series(dtype=float)
        cov = pd.to_numeric(ds.get('Lden_Coverage_pct'), errors='coerce')
        keep = cov >= self.MIN_PERIOD_COVERAGE_PCT if cov is not None else True
        return pd.to_numeric(ds.loc[keep, 'Daily_Lden'], errors='coerce').dropna()

    def _gap_info(self):
        if getattr(self, '_gap_info_cache', None) is None:
            ts_col = self._resolve_acoustic_columns()[0]
            try:
                self._gap_info_cache = (gap_report_to_dict(detect_gaps(self.df, ts_col, self.clock_zone))
                                        if ts_col else {})
            except Exception as exc:
                logger.warning(f"Gap detection failed: {exc}")
                self._gap_info_cache = {}
        return self._gap_info_cache

    def _add_time_basis_note(self, story, styles):
        story.append(Paragraph("<b>Time Basis and Periods</b>", styles['h2']))
        story.append(Paragraph(
            f"All times in this report are {escape(self.time_basis(self._get_timestamp_series(self._resolve_acoustic_columns()[0])))}. "
            "Each period takes readings from its own hours only: a calendar day runs 00:00 to 24:00; "
            "the COMAR day 07:00 to 22:00; a night (22:00 to 07:00 for COMAR, 23:00 to 07:00 for "
            "Lnight) is labelled with the evening on which it begins; and a daily Lden covers the "
            "24 hours from 07:00, its night being the one that follows (EU Directive 2002/49/EC, "
            "Annex I). When daylight saving time begins, the clock moves from 01:59 to 03:00 and that "
            "day has 23 hours; when it ends, 01:00 to 01:59 occurs twice and both passes are kept, in "
            "order. Neither change is treated as missing or duplicate data, and period coverage is "
            "measured against each period's real length.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.12 * inch))

    def _completeness_pct(self, ts_valid):
        return self._gap_info().get('completeness_exact') if not ts_valid.empty else None

    def time_basis(self, ts=None):
        return describe_time_basis(self.clock_zone, ts)

    def zone_abbreviation(self, ts):
        """Zone abbreviation(s) in force over the record, for axis titles (e.g."""
        t = pd.to_datetime(ts, errors='coerce').dropna()
        return zone_abbreviations(self.clock_zone, t.min(), t.max()) if not t.empty else self.clock_zone

    @property
    def clock_zone(self):
        """IANA zone the record's timestamps are expressed in (the report clock)."""
        return (self.df.attrs.get('clock') or {}).get('target') or DEFAULT_CLOCK

    TECHNICAL_PAGE = dict(width_in=11.0, height_in=8.5,
                          margins_in=(0.5, 0.5, 0.75, 0.75))
    RESIDENT_PAGE = dict(width_in=8.5, height_in=11.0,
                         margins_in=(0.6, 0.6, 0.6, 0.6))

    def generate_pdf_report(self, report_type='comprehensive', output_dir=None):
        """Generate the 8-section publication-grade PDF report."""
        report_filename = f"noise_analysis_{report_type}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        report_dir = output_dir or os.path.dirname(self.filepath)
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, report_filename)

        doc = self._technical_doc_template(report_path)
        story = self._build_technical_story(doc, report_type)
        doc.build(story, onFirstPage=self._add_page_template, onLaterPages=self._add_page_template)
        return report_path

    def generate_docx_report(self, report_type='comprehensive', output_dir=None):
        """The same report as :meth:`generate_pdf_report`, as an editable Word file."""
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

    def _technical_doc_template(self, path):
        g = self.TECHNICAL_PAGE
        top, bottom, left, right = g['margins_in']
        return PageTrackingDocTemplate(
            path, pagesize=(g['width_in'] * inch, g['height_in'] * inch),
            topMargin=top * inch, bottomMargin=bottom * inch,
            leftMargin=left * inch, rightMargin=right * inch,
        )

    @staticmethod
    def _paginate(doc, story):
        doc.build(list(story))
        return dict(doc.flowable_pages)

    @staticmethod
    def _footer_lines():
        return (
            f"Environmental Noise Analysis | {datetime.now().strftime('%Y-%m-%d')}",
            "Developed by Chandra Prakash Choudhary | PI: Dr. Ana María Rule, "
            "Associate Professor — Johns Hopkins University",
        )

    def _build_technical_story(self, doc, report_type='comprehensive'):
        """Assemble the technical report as a ReportLab story."""
        styles = self._get_pdf_styles()
        story = []

        # Header
        self._add_pdf_header(story, styles, report_type)

        ts_col, leq_col, lmax_col, lmin_col = self._resolve_acoustic_columns()
        ts = self._get_timestamp_series(ts_col)

        # Timestamp-integrity warning, fabricated dates were substituted.
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

        # 8 sections

        # Section 1: Executive Acoustic Summary
        self._add_section_1_executive_summary(story, styles, ts=ts, leq_col=leq_col)
        story.append(Spacer(1, 0.2 * inch))

        # Section 2: Data Quality & Completeness
        self._add_section_2_data_quality(story, styles, ts=ts)
        story.append(Spacer(1, 0.2 * inch))

        if self.weather_screen:
            self._add_weather_screen_section(story, styles)
            story.append(Spacer(1, 0.2 * inch))

        # Section 3: Comparison with health guidelines and regulatory limits
        self._add_section_3_compliance(story, styles, ts=ts, leq_col=leq_col)
        story.append(Spacer(1, 0.2 * inch))

        # Sections 4-6 and 8 are entirely date/time-based.
        time_based_ok = not getattr(self, 'timestamps_synthetic', False)

        if time_based_ok:
            # Section 4: Single-Event Sleep Disturbance
            self._add_section_4_sleep_disturbance(story, styles, ts=ts, lmax_col=lmax_col)
            story.append(Spacer(1, 0.2 * inch))

        # Optional custom section (user-provided notes), inserted after Section 4
        if self.custom_section_heading or self.custom_section_body:
            self._add_custom_section(story, styles)
            story.append(Spacer(1, 0.2 * inch))

        if time_based_ok:
            # Section 5: Daily Summary Matrix
            self._add_section_5_daily_matrix(story, styles)
            # No forced break here.
            story.append(Spacer(1, 0.2 * inch))

            # Section 6: Diurnal Hourly Profile
            self._add_section_6_hourly_profile(story, styles, ts=ts)
            story.append(Spacer(1, 0.2 * inch))
        else:
            self._add_withheld_time_sections_notice(story, styles)
            story.append(Spacer(1, 0.2 * inch))

        self._add_section_7_percentiles(story, styles, leq_col=leq_col)
        story.append(Spacer(1, 0.2 * inch))

        if time_based_ok:
            # Section 8: Visualizations
            self._add_section_8_visualizations(story, styles, ts=ts, leq_col=leq_col, lmax_col=lmax_col, lmin_col=lmin_col)
            story.append(PageBreak())

        # Section 9: Methodological Limitations & Disclaimer
        self._add_section_9_disclaimer(story, styles)

        return story

    # RESIDENT REPORT (two pages, three charts)

    def _display_location_label(self):
        label = str(self.device_id or '').strip()
        if not label or not is_safe_label(label):
            return ''
        m = _HOME_LABEL_RE.match(label)
        return f"{m.group(1).capitalize()} {m.group(2).upper()}" if m else label

    def _chart_location_suffix(self):
        label = self._display_location_label()
        return f" at {label}" if label else ""

    SOURCE_ANNOTATION_NAME = 'figure-source'
    # Reference-line labels drawn outside the plot frame, in the right margin.
    REF_LABEL_ANNOTATION_NAME = 'ref-line-label'

    @staticmethod
    def _apply_chart_typography(fig, *, title):
        fig.update_layout(title=dict(text=f"<b>{title}</b>", font=dict(color=CHART_FONT_COLOR)))
        return fig

    @staticmethod
    def _scale_fig_fonts(fig, *, px_per_pt):
        """Set every font on ``fig`` from the point sizes declared at module level."""
        def px(pt):
            return max(1, int(round(pt * px_per_pt)))

        tick_font = dict(size=px(CHART_TICK_PT), color=CHART_FONT_COLOR)
        fig.update_layout(
            title=dict(font=dict(size=px(CHART_TITLE_PT), color=CHART_FONT_COLOR)),
            legend=dict(font=dict(size=px(CHART_LEGEND_PT), color=CHART_FONT_COLOR)),
            font=tick_font,
        )
        axis_title = dict(font=dict(size=px(CHART_AXIS_TITLE_PT), color=CHART_FONT_COLOR))
        if any(getattr(tr, 'type', '') in ('scatterpolar', 'barpolar') for tr in fig.data):
            fig.update_polars(radialaxis=dict(tickfont=tick_font),
                              angularaxis=dict(tickfont=tick_font))
        else:
            # automargin lets plotly measure the rendered tick labels and grow the margin to fit them.
            fig.update_xaxes(title=axis_title, tickfont=tick_font, automargin=True)
            fig.update_yaxes(title=axis_title, tickfont=tick_font, automargin=True)
        fig.update_traces(colorbar=dict(
            title=dict(font=dict(size=px(CHART_AXIS_TITLE_PT), color=CHART_FONT_COLOR)),
            tickfont=tick_font,
        ), selector=dict(type='heatmap'))
        for ann in fig.layout.annotations:
            ann.font.size = px(CHART_ANNOTATION_PT)
        return fig

    def _add_source_annotation(self, fig, *, y=-0.20):
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

    def _chart_image(self, fig, *, width_inch, height_inch, drop_source=True):
        sized = self._size_fig_for_print(fig, width_inch=width_inch, height_inch=height_inch,
                                         drop_source=drop_source)
        return self._plotly_fig_to_image(sized, width_inch=width_inch, height_inch=height_inch)

    def _size_fig_for_print(self, fig, *, width_inch, height_inch,
                            dpi=300, drop_source=False):
        """Fix a figure's pixel canvas to the print box at ``dpi`` and size its fonts."""
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
            # drop only the source caption, keep the 53/45 dB reference labels
            fig.layout.annotations = tuple(
                a for a in fig.layout.annotations if a.name != self.SOURCE_ANNOTATION_NAME
            )
        # Fonts last, so they are sized against the canvas just set.
        px_per_pt = (canvas_w / width_inch) / 72.0
        self._scale_fig_fonts(fig, px_per_pt=px_per_pt)

        # Reserve room under a polar chart for its horizontal legend.
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

        # Reserve room for any label parked in the right margin.
        margin_labels = [a for a in fig.layout.annotations
                         if a.name == self.REF_LABEL_ANNOTATION_NAME]
        if margin_labels:
            widest = max(len(str(a.text or '')) for a in margin_labels)
            needed = int(round(widest * 0.62 * CHART_ANNOTATION_PT * px_per_pt)) + int(12 * px_per_pt)
            current = fig.layout.margin.r or 0
            if needed > current:
                fig.update_layout(margin=dict(r=needed))
        return fig

    def _exceedance_summary(self, ts, leq):
        """How often the measured level sat above each applicable limit."""
        out = {'day_pct': None, 'night_pct': None, 'who_night_pct': None,
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

        nights = self._per_night(ts, leq, WHO_NIGHT)
        if not nights.empty:
            out['nights_total'] = int(len(nights))
            out['nights_over'] = int((nights > WHO_ROAD_LNIGHT).sum())

        lden_days = self._per_day_lden()
        if not lden_days.empty:
            out['days_total'] = int(len(lden_days))
            out['days_over'] = int((lden_days > WHO_ROAD_LDEN).sum())
        return out

    @classmethod
    def _exceedance_note(cls, stats, *, brief=False):
        """Explain what the two exceedance figures are, and what they are not."""
        iv = stats.get('interval_s')
        if iv is None or iv <= 0:
            interval_phrase = "the logging interval of the instrument"
        elif iv < 60:
            interval_phrase = f"the {iv:.0f}-second readings the logger stored"
        else:
            interval_phrase = f"the {iv / 60:.0f}-minute readings the logger stored"
        if brief:
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
            "<b>None of these shares is a compliance determination.</b> Every value here is compared on a period "
            "average, and a period can sit below on its average while spending real time above the level; the "
            "comparison rows above carry that comparison. WHO further intends Lden and Lnight as long-term "
            "annual averages, so figures from a short record are indicative."
        )

    @staticmethod
    def _fmt_share(pct):
        if pct is None:
            return "not available"
        if pct == 0:
            return "0%"
        if pct < 0.1:
            return "under 0.1%"
        return f"{pct:.1f}%"

    @staticmethod
    def _chart3_method_sentences():
        return (
            "<b>Cells</b> — one clock hour on one date, computed by logarithmic energy averaging (never an "
            "arithmetic mean) over the samples in that hour; blank cells are hours with no data. "
            "<b>Colour</b> — viridis: dark purple quietest, green mid-range, bright yellow loudest. Colour "
            "reflects the measured level only; a single hour cannot be compared against the WHO "
            "guidelines, which are defined on Lden and Lnight. Every second hour is labelled, each tick "
            "on the centre of its cell."
        )

    @staticmethod
    def _chart2_method_sentences(*, metrics_at, reading='individual'):
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
            f"<b>Dashed lines</b> — the COMAR 26.02.03.02B(1) Table 1 residential limits, "
            f"{MD_RESIDENTIAL_DAY:.0f} dB(A) by day and {MD_RESIDENTIAL_NIGHT:.0f} dB(A) by night, each "
            f"drawn only across the hours its period covers. They apply to the LAeq of the whole day or "
            f"whole night period, reported {metrics_at}, not to a single hour or a single reading. [2]"
        )

    @staticmethod
    def _chart1_method_sentences(binning, *, metrics_at):
        if not binning.bin_label:
            return "Each plotted point is an energy-averaged LAeq over the resampling interval."
        # One labelled entry per element of the figure.
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
    def _period_title_label(ts_valid):
        if ts_valid.empty:
            return ''
        start, end = ts_valid.min(), ts_valid.max()
        if (start.year, start.month) == (end.year, end.month):
            return start.strftime('%B %Y')
        if start.year == end.year:
            return f"{start.strftime('%B')}–{end.strftime('%B %Y')}"
        return f"{start.strftime('%B %Y')} – {end.strftime('%B %Y')}"

    def generate_resident_pdf_report(self, output_dir=None):
        """Two-page resident summary: headline numbers plus Charts 1, 2 and 3."""
        report_filename = f"resident_noise_summary_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        report_dir = output_dir or os.path.dirname(self.filepath)
        os.makedirs(report_dir, exist_ok=True)
        report_path = os.path.join(report_dir, report_filename)

        doc = self._resident_doc_template(report_path)
        story = self._build_resident_story(doc)
        doc.build(story, onFirstPage=self._add_page_template, onLaterPages=self._add_page_template)
        return report_path

    def generate_resident_docx_report(self, output_dir=None):
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

    def _resident_doc_template(self, path):
        g = self.RESIDENT_PAGE
        top, bottom, left, right = g['margins_in']
        return PageTrackingDocTemplate(
            path, pagesize=(g['width_in'] * inch, g['height_in'] * inch),
            topMargin=top * inch, bottomMargin=bottom * inch,
            leftMargin=left * inch, rightMargin=right * inch,
            title="Resident Noise Summary", author="",
        )

    def _build_resident_story(self, doc):
        """Assemble the resident summary as a ReportLab story (PDF and Word)."""
        ts_col, leq_col, lmax_col, lmin_col = self._resolve_acoustic_columns()
        styles = self._get_pdf_styles()
        cap = ParagraphStyle('ResidentCaption', parent=styles['BodyText'],
                             fontSize=8, leading=10.5,
                             textColor=colors.HexColor('#374151'), spaceAfter=2)
        story = []

        ts = self._get_timestamp_series(ts_col)
        leq = self._get_numeric_series(leq_col)
        lmax = self._get_numeric_series(lmax_col) if (lmax_col and lmax_col in self.df.columns) else None
        lmin = self._get_numeric_series(lmin_col) if (lmin_col and lmin_col in self.df.columns) else None

        ts_valid = ts.dropna()
        location = self._display_location_label()
        suffix = self._chart_location_suffix()
        period = self._period_title_label(ts_valid)

        # Header
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

        # Definitions, before anything that uses the terms
        story.append(self._resident_definitions_table(ts))
        story.append(Spacer(1, 0.10 * inch))

        # Key numbers
        story.extend(self._resident_result_tables(ts=ts, leq=leq, lmax_col=lmax_col))
        story.append(Spacer(1, 0.05 * inch))
        story.append(Paragraph(self._exceedance_note(self._exceedance_summary(ts, leq), brief=True), cap))
        story.append(Spacer(1, 0.08 * inch))

        source_note = (f" <b>Data source:</b> {escape(self._figure_source_label())}."
                       + self._figure_screen_note())
        avail_w = doc.width / inch

        # No forced break here.

        # Figure 1, time series
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

        # Figure 2, hourly distribution
        reading_word = self._reading_word(ts)
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

        # Figure 3, heatmap
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

        # closing note
        story.append(Paragraph(
            "<b>About the WHO guidelines.</b> The values quoted are the road-traffic guideline values. WHO "
            "sets its guidelines separately for each transport source, and road traffic is the general, "
            "widely cited benchmark; quoting it here does not assert that road traffic is the source of "
            "the sound measured at this home. A sound level meter records total sound energy and cannot "
            "identify what produced it. The WHO values are annual averages, so a record of days or weeks "
            "is indicative rather than a determination. [1]",
            cap))
        story.append(Spacer(1, 0.06 * inch))
        if self.weather_screen:
            story.append(Paragraph(self._weather_screen_resident_note(), cap))
            story.append(Spacer(1, 0.06 * inch))
        story.append(Paragraph(
            f"<b>About these measurements.</b> Levels are A-weighted. Instrument and calibration: "
            f"{escape(self.instrument_note)} "
            "A sound level meter records total sound energy; it does not identify what produced a sound, "
            "so attributing any level here to a particular source requires evidence beyond these "
            "measurements. ISO 1996-2 notes that the combined standard uncertainty of an environmental "
            "noise measurement is typically 1 to 3 dB, so smaller differences should not be treated as "
            "meaningful. The technical report gives the guideline comparisons, the method and the "
            "data-quality record in full.",
            cap))
        story.append(Spacer(1, 0.06 * inch))
        story.append(Paragraph("<b>Sources</b>", cap))
        ref_style = ParagraphStyle(
            'Reference', parent=cap,
            leftIndent=14, firstLineIndent=-14, spaceAfter=3,
        )
        for entry in self._limit_reference_entries():
            story.append(Paragraph(entry, ref_style))

        return story

    @staticmethod
    def _limit_reference_entries():
        """Numbered sources, one entry per item."""
        return [
            "[1] WHO, <i>Environmental Noise Guidelines for the European Region</i> (2018), "
            "ISBN 978-92-890-5356-3: road traffic Lden 53 dB and Lnight 45 dB, both graded strong "
            "recommendations. Lden and Lnight are defined in EU Directive 2002/49/EC, Annex I, as "
            "long-term averages over a year.",

            "[2] COMAR 26.02.03.02B(1), Table 1, Maximum Allowable Noise Levels: residential "
            "65 dBA by day and 55 dBA at night, expressed as equivalent A-weighted sound levels "
            "(.02A(2)); prominent discrete tones and periodic noises must be 5 dBA lower (.02B(3)); "
            "sources including motor vehicles on public roads, licensed airports, railroads and "
            "residential air-conditioning are exempt (.02C); measured at or within the property "
            "line of the receiving property with a Type II or better meter (.02D). Regulation .03 "
            "has been repealed.",

            "[3] COMAR 26.02.03.01B(4) and B(14): daytime is 7 a.m. to 10 p.m., nighttime is "
            "10 p.m. to 7 a.m. B(12) defines equivalent A-weighted sound level.",

            "[4] ISO 1996-1 and ISO 1996-2, <i>Acoustics \u2014 Description, measurement and assessment "
            "of environmental noise</i>: definition of the equivalent continuous sound level, and a "
            "combined measurement uncertainty of the order of 1 to 3 dB.",

            "[5] WHO, <i>Guidelines for Community Noise</i> (Berglund, Lindvall &amp; Schwela, 1999), "
            "ISBN 92-4-154553-4: the decibel scale and its relation to perceived loudness; indoor "
            "bedroom guidelines.",
        ]

    def _reading_word(self, ts):
        iv = self._logging_interval_s(ts)
        if not iv:
            return 'logged'
        return f"{iv:.0f}-second" if iv < 60 else f"{iv / 60:.0f}-minute"

    def _resident_definitions_table(self, ts):
        """Define every term before the report uses it."""
        styles = self._get_pdf_styles()
        term = ParagraphStyle('DefTerm', parent=styles['BodyText'], fontSize=8.5,
                              leading=10.5, fontName='Helvetica-Bold', spaceAfter=0)
        body = ParagraphStyle('DefBody', parent=styles['BodyText'], fontSize=8.5,
                              leading=10.5, spaceAfter=0)
        head = ParagraphStyle('DefHead', parent=body, fontName='Helvetica-Bold',
                              textColor=colors.white)

        reading = self._reading_word(ts)

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

    def _resident_result_tables(self, *, ts, leq,
                                lmax_col):
        """The results, as three tables: summary, COMAR limits, WHO guidelines."""
        ts_valid = ts.dropna()
        leq_clean = leq.dropna()

        start_str = ts_valid.min().strftime('%d %b %Y') if not ts_valid.empty else 'N/A'
        end_str = ts_valid.max().strftime('%d %b %Y') if not ts_valid.empty else 'N/A'
        completeness = self._completeness_pct(ts_valid)

        laeq_v = energetic_mean_db(leq) if not leq_clean.empty else None
        env = (compute_ldn_lden(ts, leq) or {}) if not leq_clean.empty else {}
        exc = (exceedance_levels_db(leq_clean.to_numpy()) or {}) if not leq_clean.empty else {}

        # LAeq_day is 07-22 and LAeq_night 22-07 (Ldn windows, as COMAR quotes them)
        laeq_day = env.get('LAeq_day')
        laeq_night = env.get('LAeq_night')

        exc_stats = self._exceedance_summary(ts, leq)
        reading = self._reading_word(ts)

        # Per-period series behind the four range statements.
        day_series = (pd.to_numeric(self.daily_summary.get('Daytime_LAeq'), errors='coerce').dropna()
                      if self.daily_summary is not None else pd.Series(dtype=float))
        night_comar_series = self._per_night(ts, leq, COMAR_NIGHT)
        lden_series = self._per_day_lden()
        lnight_series = self._per_night(ts, leq, WHO_NIGHT)
        # Days that actually contain readings, not merely the calendar span.
        days_with_data = int(ts_valid.dt.date.nunique()) if not ts_valid.empty else 0

        def _d(v):
            try:
                fv = float(v)
                return f"{fv:.1f} dB(A)" if np.isfinite(fv) else "Not available"
            except (TypeError, ValueError):
                return "Not available"

        def lim(limit_db):
            return f"<b>{float(limit_db):.0f} dB(A)</b>"

        def vs(value, limit_db):
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

        def vs_range(value, limit_db, per_period, period_word):
            base = vs(value, limit_db)
            per = pd.to_numeric(per_period, errors='coerce').dropna()
            if base == "Not available" or len(per) < 2:
                return base
            return (f"{base}. Individual {period_word} values ranged "
                    f"{per.min():.1f} to {per.max():.1f} dB(A) across "
                    f"{len(per)} {period_word}s")

        def laeq_with_spread():
            base = _d(laeq_v)
            if base == "Not available" or self.daily_summary is None:
                return base
            daily = pd.to_numeric(
                self.daily_summary.get('Average_L_EQ_dB'), errors='coerce').dropna()
            if len(daily) < 2:
                return base
            return (f"{base}. Daily values ranged {daily.min():.1f} to "
                    f"{daily.max():.1f} dB(A) across {len(daily)} days")

        def climate_spread():
            lo, hi = exc.get('L90'), exc.get('L10')
            try:
                lo_f, hi_f = float(lo), float(hi)
                if not (np.isfinite(lo_f) and np.isfinite(hi_f)):
                    raise ValueError
            except (TypeError, ValueError):
                return "Not available"
            return (f"{lo_f:.1f} to {hi_f:.1f} dB(A), a spread of {hi_f - lo_f:.1f} dB "
                    f"covering the middle 80% of readings")

        def periods_over():
            bits = []
            if exc_stats['days_over'] is not None:
                bits.append(f"Lden above {WHO_ROAD_LDEN:.0f} dB(A) on "
                            f"{exc_stats['days_over']} of {exc_stats['days_total']} days")
            if exc_stats['nights_over'] is not None:
                bits.append(f"Lnight above {WHO_ROAD_LNIGHT:.0f} dB(A) on "
                            f"{exc_stats['nights_over']} of {exc_stats['nights_total']} nights")
            return "; ".join(bits) if bits else "Not available"

        if completeness is None:
            completeness_str = "Not available"
        else:
            cp = math.floor(float(completeness) * 10.0) / 10.0
            completeness_str = f"{floor_pct(min(cp, 100.0), 2):.2f}% of expected readings present"

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

        def build(headers, rows, widths):
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
        summary = build(
            ['Table 2. Summary of measurements', 'Measured at this home'],
            [
                ("Monitoring period", f"{start_str} to {end_str}, {days_with_data} days with data "
                                      f"(times in {self.zone_abbreviation(ts_valid)})"),
                ("Data completeness", completeness_str),
                ("LAeq, whole period", laeq_with_spread()),
                ("Spread of levels, L90 to L10", climate_spread()),
                ("L-Max, loudest reading", _d(self._lamax_value(lmax_col))),
            ],
            [W_LABEL, W_LIMIT + W_VALUE],
        )

        comar = build(
            ['Table 3. Maryland COMAR — enforceable legal limits (residential)',
             'Legal limit', 'Measured at this home'],
            [
                ("Daytime LAeq, 07:00–22:00", lim(MD_RESIDENTIAL_DAY),
                 vs_range(laeq_day, MD_RESIDENTIAL_DAY, day_series, "day")),
                ("Night-time LAeq, 22:00–07:00", lim(MD_RESIDENTIAL_NIGHT),
                 vs_range(laeq_night, MD_RESIDENTIAL_NIGHT, night_comar_series, "night")),
                ("Share of time at or above the limit", "Not applicable",
                 f"Daytime {self._fmt_share(exc_stats['day_pct'])} of the {reading} readings in "
                 f"07:00–22:00; night-time {self._fmt_share(exc_stats['night_pct'])} of the "
                 f"{reading} readings in 22:00–07:00"),
            ],
            [W_LABEL, W_LIMIT, W_VALUE],
        )

        who = build(
            ['Table 4. WHO 2018 — health-based guidelines (not law)',
             'Guideline', 'Measured at this home'],
            [
                ("Lden, 24 h with evening and night penalties", lim(WHO_ROAD_LDEN),
                 vs_range(env.get('Lden'), WHO_ROAD_LDEN, lden_series, "day")),
                ("Lnight, 23:00–07:00", lim(WHO_ROAD_LNIGHT),
                 vs_range(env.get('Lnight'), WHO_ROAD_LNIGHT, lnight_series, "night")),
                ("Share of time at or above the Lnight guideline", "Not applicable",
                 f"{self._fmt_share(exc_stats['who_night_pct'])} of the {reading} readings in "
                 f"23:00–07:00"),
                ("Individual periods above the guideline", "Not applicable", periods_over()),
            ],
            [W_LABEL, W_LIMIT, W_VALUE],
        )

        return [summary, Spacer(1, 0.09 * inch), comar, Spacer(1, 0.09 * inch), who]

    # OPTIONAL CUSTOM SECTION (user notes, inserted after Section 4)

    # Weather screening disclosure

    _CLOCK_TEXT = {
        'local_dst': "local time in {tz}, observing daylight saving time",
        'local_standard': "local standard time in {tz}, without daylight saving",
        'utc': "Coordinated Universal Time (UTC)",
    }

    def _weather_screen_station_text(self):
        st = self.weather_screen['station']
        return (f"{st['name']} ({st['station_id']}) Automated Surface Observing System station, "
                f"{st['distance_km']:.1f} km from the monitoring location")

    def _weather_screen_resident_note(self):
        ws = self.weather_screen
        pct = 100.0 * ws['rows_removed'] / max(1, ws['rows_considered'])
        return (
            "<b>About weather.</b> Readings taken during rain, snow, thunder or strong wind, "
            "shortly before and after rain, while snow was on the ground, or when the weather "
            "could not be confirmed were removed before these results were calculated, using "
            f"official records from the {escape(self._weather_screen_station_text())}. "
            f"{ws['rows_removed']:,} readings ({pct:.1f}%) were removed for this reason. Weather "
            "at the station can differ from weather at the home, especially during showers.")

    def _weather_snow_rule_text(self):
        ws, cfg = self.weather_screen, self.weather_screen['config']
        src = ws.get('snow_source') or {'mode': 'depth'}
        limit = (f"{cfg['snow_depth_limit_mm'] / 25.4:g} inch ({cfg['snow_depth_limit_mm']:g} mm)")
        where = (f"station {escape(str(src.get('ghcnd_id', '')))}"
                 + (f", {src['distance_km']:.1f} km from the monitoring location"
                    if src.get('distance_km') else " (the weather station itself)"))
        return (f"<b>Snow cover:</b> every day with {limit} or more of snow on the ground at NOAA "
                f"{where}, and the day either side (IOA Good Practice Guide 2013, §2.7.3). Snow "
                "depth is not measured by the airport's automated sensors.")

    def _add_weather_screen_section(self, story, styles):
        """Method, rules, sources and effect of the weather screen."""
        ws = self.weather_screen
        cfg, summ, st = ws['config'], ws['summary'], ws['station']
        body = styles['BodyText']
        story.append(Paragraph("Weather Screening (Rain, Snow, Thunder, Wind)", styles['h1']))
        story.append(Paragraph(
            "Readings recorded while the weather could have affected the microphone were removed "
            "before any result in this report was calculated. Weather was taken from the "
            f"{escape(self._weather_screen_station_text())} (station located from a "
            f"{escape(ws['location_basis'])}; the location itself is not recorded). "
            f"Sources: {escape(ws['sources'])}", body))
        story.append(Spacer(1, 0.08 * inch))

        blk = cfg['block_minutes']
        clock = self._CLOCK_TEXT.get(ws['clock'], ws['clock']).format(tz=ws.get('tz') or st['tz'])
        rules = [
            f"The record was divided into {blk}-minute blocks. Logger timestamps were read as {escape(clock)}.",
            "<b>Precipitation:</b> every block in which the station reported precipitation of any kind "
            "(rain, drizzle, snow, ice pellets, hail or unidentified), or its rain gauge registered any "
            "amount (ISO 1996-2:2017; NSW Noise Policy for Industry 2017, Fact Sheet A4).",
            f"<b>Buffer:</b> {cfg['buffer_before_blocks']} block(s) before each precipitation or thunder "
            "block, for rain-gauge latency (IOA Good Practice Guide 2013, §3.1.9), and "
            f"{cfg['buffer_after_blocks']} block(s) after, because a wet windscreen alters readings "
            "after rain stops (ISO 1996-2).",
            "<b>Thunder:</b> every block in which thunder was reported at or near the station.",
            (f"<b>Wind:</b> every block whose mean wind at microphone height exceeded "
             f"{cfg['wind_limit_ms']:g} m/s (NSW Noise Policy for Industry 2017, Fact Sheet A4, which "
             "sets the limit on an average at microphone height)"
             + (f", and every block whose station wind or gust maximum at "
                f"{cfg['anemometer_height_m']:g} m exceeded the same limit with no height reduction — a "
                "stricter platform rule, informed by FHWA guidance, because a modelled height reduction "
                "cannot prove the wind at the microphone"
                if cfg.get('screen_station_wind') else
                ". The station's own wind and gust maxima are reported in the period-by-period audit "
                "trail but were not themselves grounds for exclusion")
             + f". Station mean wind, measured at {cfg['anemometer_height_m']:g} m, was converted to the "
             f"{cfg['mic_height_m']:g} m microphone height with the logarithmic wind profile, "
             "v(h) = v(h_ref)·ln(h/z0)/ln(h_ref/z0), "
             f"z0 = {cfg['roughness_length_m']:g} m (IEC 61400-11 reference roughness); factor "
             f"{ws['wind_height_factor']:.3f}."),
            self._weather_snow_rule_text(),
            f"<b>Unverified weather:</b> blocks without known wind and precipitation in every {cfg['slot_minutes']}-minute "
            "interval, neighbours within the configured buffer, and days whose snow cover could not be "
            "established. Periods whose weather could not be verified are treated as affected, "
            "never as clean.",
        ]
        for r in rules:
            story.append(Paragraph(r, body))
            story.append(Spacer(1, 0.04 * inch))
        story.append(Spacer(1, 0.06 * inch))

        story.append(Paragraph("<b>Readings removed from this report's data</b>", body))
        considered = max(1, ws['rows_considered'])
        for code, n in sorted(ws['rows_removed_by_reason'].items(), key=lambda kv: -kv[1]):
            label = WEATHER_REASON_LABELS.get(code, code)
            story.append(Paragraph(f"{escape(label)}: {n:,} ({100.0 * n / considered:.1f}%)", body))
        kept = ws['rows_considered'] - ws['rows_removed']
        # Native table graphic is preserved by both PDF and Word renderers.
        shares = [kept / considered, ws['rows_removed'] / considered]
        if all(v > 0 for v in shares):
            bar = Table([["Retained", "Excluded"]], colWidths=[6.0 * inch * v for v in shares])
            bar.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (0, 0), colors.HexColor('#0369a1')),
                ('BACKGROUND', (1, 0), (1, 0), colors.HexColor('#b45309')),
                ('TEXTCOLOR', (0, 0), (-1, -1), colors.white),
                ('FONTSIZE', (0, 0), (-1, -1), 7),
            ]))
            # Avoid illegible labels on very narrow segments.
            if min(shares) >= 0.12:
                story.append(bar)
        story.append(Paragraph("Screened results describe retained readings only. Excluded periods "
                               "are not reconstructed; percentages below are sample counts, not time coverage.", body))
        story.append(Paragraph(
            f"<b>Retained:</b> {kept:,} of {ws['rows_considered']:,} readings "
            f"({100.0 * kept / considered:.1f}%).", body))
        story.append(Spacer(1, 0.08 * inch))

        agree = summ.get('source_agreement')
        quality = (f"The station's 1-minute archive covered {summ['blocks_with_1min_pct']:.1f}% of "
                   f"blocks; {summ['blocks_verified_pct']:.1f}% met the wind/precipitation coverage rule. "
                   "Missing minutes may be supplemented by METAR reports; gaps remain excluded.")
        iq = ws.get('identifier_quality') or summ.get('identifier_quality') or {}
        if iq.get('reliable') is False:
            quality += (" The station's 1-minute precipitation identifier disagreed with its own "
                        f"METAR reports on {iq.get('only_1min_pct')}% of "
                        f"{iq.get('minutes_compared', 0):,} jointly observed minutes, so it was "
                        "flagged as suspect. Positive precipitation evidence was retained conservatively; "
                        "disagreement alone cannot establish dry conditions.")
        if iq.get('impossible_snow_minutes'):
            quality += (f" {iq['impossible_snow_minutes']:,} minutes carried a snow code at air "
                        "unusually warm temperatures. These suspect reports were retained as precipitation "
                        "evidence; temperature alone does not establish dry conditions.")
        if agree:
            quality += (f" Where both sources observed the same minute, they agreed on whether "
                        f"precipitation was occurring in {agree['agree_pct']:.1f}% of "
                        f"{agree['minutes_compared']:,} minutes; precipitation reported by either "
                        "source was treated as precipitation.")
        story.append(Paragraph(f"<b>Weather data quality.</b> {quality}", body))
        story.append(Spacer(1, 0.08 * inch))
        far = (" At this distance the screen can miss rain that fell only at the monitoring "
               "location, and remove periods that were dry there; an on-site rain gauge would "
               "settle both." if float(st.get('distance_km') or 0) > 25 else "")
        story.append(Paragraph(
            "<b>Limitations.</b> Weather at a station "
            f"{st['distance_km']:.1f} km away can differ from weather at the microphone, "
            f"particularly for showers.{far} Estimated microphone wind can overstate or understate actual "
            "site wind. Retained observations are not certified free of weather effects. "
            "On-site weather and surface-condition logs are needed for site verification. "
            "Traffic on wet roads after rain is louder; that "
            "effect is not screened. Removing weather-affected periods changes the time periods "
            "the averages cover. The block-by-block record of this screen (reference "
            f"{escape(ws['screen_id'])}) can be downloaded from the analysis platform.", body))
        story.append(Spacer(1, 0.12 * inch))

    def _add_custom_section(self, story, styles):
        heading = self.custom_section_heading or "Additional Notes"
        story.append(Paragraph(f"Additional Notes: {escape(heading)}", styles['h1']))
        if self.custom_section_body:
            for line in self.custom_section_body.splitlines():
                line = line.strip()
                if line:
                    story.append(Paragraph(escape(line), styles['BodyText']))
                    story.append(Spacer(1, 0.06 * inch))

    # Section 9: methodological limitations & disclaimer

    def _add_withheld_time_sections_notice(self, story, styles):
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
    def _file_sha256(path):
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
        """Data provenance and chain of custody."""
        story.append(Paragraph("Data Provenance &amp; Chain of Custody", styles['h2']))

        src = os.path.basename(self.filepath or '')
        digest = self._file_sha256(self.filepath) if self.filepath else None

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
            "The deployment geometry (microphone height, orientation and distance from any "
            "reflecting facade) is not recorded in the data file and is not reproduced here. "
            "Levels reported here are reproducible from the source file independently of that "
            "documentation.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.12 * inch))

    def _add_section_9_disclaimer(self, story, styles):
        """Section 9: limitations and disclaimer."""
        story.append(Paragraph("Section 9: Methodological Limitations &amp; Disclaimer", styles['h1']))
        story.append(Spacer(1, 0.15 * inch))

        self._add_provenance_section(story, styles)

        # Time basis: every period boundary below depends on it.
        if not getattr(self, 'timestamps_synthetic', False):
            self._add_time_basis_note(story, styles)

        # Averaging period, the most commonly overlooked limitation.
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
            "(e.g., distinguishing a commercial aircraft from a heavy goods vehicle). A source-specific comparison requires "
            "cross-referencing with external databases (e.g., ADS-B flight tracking).",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.12 * inch))

        # Health vs. Legal Limits
        story.append(Paragraph("<b>Health vs. Legal Limits</b>", styles['h2']))
        story.append(Paragraph(
            "This report evaluates data against both biological health guidelines (WHO 2018) and local regulatory limits "
            "(e.g., Maryland COMAR). A level at or below a regulatory limit does not guarantee the absence of adverse "
            "health effects.",
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

    # Formatting helpers (strict - no text/float fusion)

    @staticmethod
    def _fmt_float(value, decimals=2):
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
    def _fmt_db(cls, value):
        s = cls._fmt_float(value, 2)
        return "N/A" if s == "N/A" else f"{s} dB(A)"

    @staticmethod
    def _table_header_style():
        return ParagraphStyle('TableHeader', fontName='Helvetica', fontSize=8.5, leading=10.5,
                              textColor=colors.white, alignment=TA_CENTER)

    @classmethod
    def _fmt_db_plain(cls, value):
        return cls._fmt_float(value, 2)

    # Acoustic column detection

    def _resolve_acoustic_columns(self):
        """Map dataset columns into LEQ / L-Max / L-Min streams."""
        def norm(name):
            return "".join(ch for ch in (name or "").lower() if ch.isalnum())

        from analysis.timestamp_utils import resolve_time_column
        ts_col = resolve_time_column(self.df)

        noise_cols = list(self.analyzer.noise_columns)

        lmax_col = None
        lmin_col = None
        for c in noise_cols:
            n = norm(c)
            if lmax_col is None and (n.startswith("lmax") or "lmax" in n or n.endswith("maxdba")):
                lmax_col = c
            if lmin_col is None and (n.startswith("lmin") or "lmin" in n or n.endswith("mindba")):
                lmin_col = c

        leq_candidates = []
        for c in noise_cols:
            n = norm(c)
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

    def _get_timestamp_series(self, ts_col):
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

    def _figure_screen_note(self):
        if not self.weather_screen:
            return ""
        ws = self.weather_screen
        pct = 100.0 * ws['rows_removed'] / max(1, ws['rows_considered'])
        return (f" Weather-screened: {pct:.1f}% of readings were removed as weather-affected or "
                "unverifiable and are absent from this figure (see Weather Screening).")

    def _figure_source_label(self):
        stem = os.path.splitext(os.path.basename(self.filepath or ""))[0]
        stem = re.sub(r"^\d{8}_\d{6}_", "", stem)
        if not self.deidentify or is_safe_label(stem):
            return stem or "not recorded"
        return self._display_location_label() or self.device_id or "withheld"

    def _lamax_value(self, lmax_col):
        if not lmax_col or lmax_col not in self.df.columns:
            return None
        s = pd.to_numeric(self.df[lmax_col], errors='coerce').dropna()
        return float(s.max()) if not s.empty else None

    @staticmethod
    def _logging_interval_s(ts):
        try:
            v = _modal_interval_seconds(pd.to_datetime(ts, errors='coerce').dropna())
            return float(v) if v and v > 0 else None
        except Exception:
            return None

    def _get_numeric_series(self, col):
        if not col or col not in self.df.columns:
            return pd.Series(dtype=float)
        return pd.to_numeric(self.df[col], errors="coerce")

    # Section 1: executive acoustic summary

    def _add_section_1_executive_summary(self, story, styles, *, ts, leq_col):
        story.append(Paragraph("Section 1: Executive Acoustic Summary", styles['h1']))

        # Device / Location identity
        if self.device_id:
            story.append(Paragraph(f"<b>Device / Location ID:</b> {escape(self.device_id)}", styles['BodyText']))

        # Dataset provenance, single or merged
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

        if getattr(self, 'timestamps_synthetic', False):
            story.append(Paragraph(
                "<b>Measurement Date Range:</b> Not available — the date/time column in this "
                "file could not be read. The number of samples and their levels are known; "
                "when they were recorded is not.", styles['BodyText']))
            story.append(Paragraph(
                f"<b>Samples Analysed:</b> {len(self.df):,}", styles['BodyText']))
        else:
            story.append(Paragraph(f"<b>Measurement Date Range:</b> {escape(date_range)}", styles['BodyText']))
            story.append(Paragraph(f"<b>Time basis:</b> {escape(self.time_basis(ts_valid))}", styles['BodyText']))
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
        n_days = None
        try:
            tsv = ts.dropna()
            if len(tsv) > 1:
                n_days = (tsv.max() - tsv.min()).total_seconds() / 86400.0
        except (TypeError, AttributeError) as exc:
            logger.warning("Record length not stated in section 1: %s", exc)
        dtxt = _days_text(n_days) if n_days else ''
        perlbl = ("24-Hour" if dtxt == "1 day" else dtxt.replace(" days", "-Day") if dtxt else "Whole-Record")
        story.append(Paragraph(f"<b>{perlbl} Energy Average (LAeq):</b> {escape(laeq_str)}", styles['BodyText']))
        story.append(Spacer(1, 0.1 * inch))

        try:
            laeq_v = float(laeq) if laeq is not None else float('nan')
        except Exception:
            laeq_v = float('nan')

        if np.isfinite(laeq_v):
            laeq_str = escape(self._fmt_float(laeq_v))
            interp = (
                f"The equivalent continuous sound level (LAeq) over the measurement period was "
                f"{laeq_str} dB(A). Section 3 compares the record with the WHO 2018 guideline "
                f"values and the Maryland COMAR limits."
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
        n_days_v  = (ts_valid.max() - ts_valid.min()).total_seconds() / 86400 if not ts_valid.empty else 0

        completeness = self._completeness_pct(ts_valid)
        gi = self._gap_info()

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
            # A synthetic index is NOT a timeline.
            timestamps_unusable=bool(getattr(self, 'timestamps_synthetic', False)),
            lamax=self._lamax_value(self._resolve_acoustic_columns()[2]),
            logging_interval_s=self._logging_interval_s(ts),
            energy_dominance=energy_concentration(leq.dropna()),
            n_gaps=int(gi.get('gap_count') or 0),
            total_gap_hours=float(gi.get('missing_seconds') or 0.0) / 3600.0,
            time_basis=self.time_basis(ts_valid),
            clock_change_dates=[pd.Timestamp(c['before']).strftime('%d %b %Y')
                                for c in gi.get('clock_changes', [])],
            excluded=[_excluded_text(x) for x in gi.get('excluded', [])],
        )

        box_bg, box_border = '#EFF6FF', '#3D5A80'

        # The heading goes INSIDE the box below, as its first row.

        # Parse summary text into sections
        body_parts, who_part = [], None
        for para_block in summary_text.split('\n\n'):
            lines = para_block.split('\n')
            header_lines = [l for l in lines if not l.strip().startswith('•')]
            bullet_lines = [l.strip().lstrip('•').strip() for l in lines if l.strip().startswith('•')]
            header_joined = ' '.join(header_lines).strip()
            first = header_joined.strip()
            if first.startswith('WHO '):
                who_part = (header_joined, bullet_lines)
            else:
                if header_joined or bullet_lines:
                    body_parts.append((header_joined, bullet_lines))

        # Build content as separate inner Paragraphs inside one Table cell.
        plain_style = ParagraphStyle(
            'SummaryPlain', parent=styles['BodyText'],
            fontSize=9, leading=13, spaceAfter=4,
        )
        bold_style = ParagraphStyle(
            'SummaryBold', parent=plain_style,
            fontName='Helvetica-Bold', spaceAfter=2,
        )

        heading_style = ParagraphStyle(
            'SummaryHeading', parent=plain_style,
            fontName='Helvetica-Bold', fontSize=12, leading=15, spaceAfter=4,
            textColor=colors.HexColor('#293241'),
        )

        inner_content = [
            Paragraph("Non-Technical Summary: Noise Exposure &amp; Health Assessment", heading_style)
        ]

        # 2. Body paragraphs (dataset overview, noise level, variability)
        for (hdr, bullets) in body_parts:
            if hdr:
                inner_content.append(Paragraph(escape(hdr), plain_style))
            for bl in bullets:
                inner_content.append(Paragraph(f'•  {escape(bl)}', plain_style))

        # 3. WHO compliance section
        if who_part:
            inner_content.append(Spacer(1, 4))
            hdr, bullets = who_part
            inner_content.append(Paragraph(escape(hdr), bold_style))
            for bl in bullets:
                inner_content.append(Paragraph(f'•  {escape(bl)}', plain_style))

        summary_table = Table(
            [[flowable] for flowable in inner_content],
            colWidths=[9.0 * inch],
            splitByRow=1,
        )
        summary_table.setStyle(TableStyle([
            ('BACKGROUND',    (0, 0), (-1, -1), colors.HexColor(box_bg)),
            ('BOX',           (0, 0), (-1, -1), 1.2, colors.HexColor(box_border)),
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

        # top 10 peak events
        if not getattr(self, 'timestamps_synthetic', False):
            self._add_top_noise_events(story, styles, ts=ts, leq_col=leq_col)
        else:
            story.append(Paragraph(
                "<b>Top Noise Events:</b> Withheld. The loudest levels in this dataset are known, "
                "but the time at which each occurred is not, and an event table without reliable "
                "timestamps cannot be checked against the raw record.",
                styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))

    # Section 2: data quality & completeness

    # Top 10 Peak Noise Events

    @staticmethod
    def _compute_top_noise_events(ts, leq, top_n=10,
                                  min_separation_minutes=5.0,
                                  fold=None):
        """Detect discrete loud noise events and return the top N by peak level."""
        ts  = ts.dropna()
        leq = pd.to_numeric(leq, errors='coerce').reindex(ts.index)
        # Elapsed-time order: a repeated November hour sorts as two hours.
        key = ordering_key(ts, fold.reindex(ts.index) if fold is not None else None)
        df_tmp = pd.DataFrame({'ts': ts.values, 'key': key.values, 'leq': leq.values}).dropna()
        df_tmp = df_tmp.sort_values('key', kind='mergesort').reset_index(drop=True)
        if len(df_tmp) < 2:
            return []

        threshold = float(np.percentile(df_tmp['leq'], 90))
        wall   = df_tmp['ts'].to_numpy()
        times  = df_tmp['key'].to_numpy()
        levels = df_tmp['leq'].to_numpy()
        above  = levels >= threshold
        if not above.any():
            return []

        diffs = np.diff(times).astype('timedelta64[s]').astype(float)
        interval_s = float(np.median(diffs)) if len(diffs) else 1.0
        bridge_s = max(interval_s * 3.0, 3.0)

        events = []
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
            seg_wall = wall[i:j + 1]
            k = int(np.argmax(seg_lv))
            events.append({
                'start':      seg_wall[0],
                'end':        seg_wall[-1],
                'peak_time':  seg_wall[k],
                'peak_key':   seg_ts[k],
                'peak':       float(seg_lv[k]),
                'duration_s': float((seg_ts[-1] - seg_ts[0]) / np.timedelta64(1, 's')) + interval_s,
            })
            i = j + 1

        events.sort(key=lambda e: e['peak'], reverse=True)

        # Keep only distinct events, peaks at least min_separation apart.
        sep = np.timedelta64(int(min_separation_minutes * 60), 's')
        selected = []
        for e in events:
            if all(abs(e['peak_key'] - s['peak_key']) > sep for s in selected):
                selected.append(e)
            if len(selected) >= top_n:
                break
        return selected

    def _add_top_noise_events(self, story, styles, *, ts, leq_col):
        from reportlab.platypus import Table, TableStyle
        from reportlab.lib import colors as rl_colors

        leq = self._get_numeric_series(leq_col)
        if ts.dropna().empty or leq.dropna().empty:
            return

        fold = self.df[FOLD_COLUMN] if FOLD_COLUMN in self.df.columns else None
        events = self._compute_top_noise_events(ts, leq, fold=fold)
        if not events:
            return

        interval = self._logging_interval_s(ts) or 1.0
        interval_label = f"{interval:.0f} s" if interval >= 1 else f"{interval:.2f} s"
        block = [Spacer(1, 0.1 * inch), Paragraph("<b>Top Peak Noise Events</b>", styles['h2']),
                 Paragraph(
            "The loudest discrete events in the record. Each event is a contiguous run of LEQ "
            f"readings at or above the 90th-percentile level (L10); <b>Peak LEQ</b> is the highest "
            f"{interval_label} LEQ reading in the event and <b>Peak Time</b> the moment it was recorded "
            f"({escape(self.time_basis(ts))}). <b>Duration</b> is the number of readings in the event "
            "times the logging interval. Listed events are at least 5 minutes apart.",
            styles['BodyText']
        ), Spacer(1, 0.06 * inch)]

        header = ['#', 'Date', 'Day', 'Peak Time', f'Peak LEQ ({interval_label})', 'Duration']
        rows   = [header]
        for i, ev in enumerate(events, 1):
            dt_peak   = pd.Timestamp(ev['peak_time'])
            dur_str   = format_duration(ev['duration_s'])
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
        story.append(KeepTogether(block + [tbl]))
        story.append(Spacer(1, 0.15 * inch))

    # Top noise events for HTML report

    def _add_section_2_data_quality(self, story, styles, *, ts):
        story.append(Paragraph("Section 2: Data Quality & Completeness (QA/QC)", styles['h1']))

        ts_valid = ts.dropna()
        if ts_valid.empty:
            story.append(Paragraph("No valid timestamps found.", styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))
            return

        start = ts_valid.min()
        end = ts_valid.max()
        interval_s = _modal_interval_seconds(ts_valid)
        interval_s = interval_s if interval_s and interval_s > 0 else 1.0
        gi = self._gap_info()
        completeness = self._completeness_pct(ts_valid)
        actual_samples = len(self.df)
        expected_samples = int(gi.get('expected_rows') or actual_samples)
        uptime_pct = floor_pct(completeness, 2) if completeness is not None else None
        interval_label = (f"{interval_s:.0f} s" if interval_s >= 1 else f"{interval_s:.3f} s")
        body = styles['BodyText']

        if getattr(self, 'timestamps_synthetic', False):
            story.append(Paragraph(
                "<b>Measurement Span:</b> Not available — timestamps unreadable. Sample counts "
                "below are exact; completeness cannot be assessed without knowing the intended "
                "recording period.", body))
        else:
            story.append(Paragraph(f"<b>Measurement Span:</b> {escape(start.strftime('%Y-%m-%d %H:%M:%S'))} to {escape(end.strftime('%Y-%m-%d %H:%M:%S'))}", body))
            story.append(Paragraph(f"<b>Time basis:</b> {escape(self.time_basis(ts_valid))}", body))
            clock = self.df.attrs.get('clock') or {}
            if clock.get('converted'):
                story.append(Paragraph(
                    f"<b>Clock conversion:</b> logger timestamps were recorded in "
                    f"{escape(clock.get('source_label', ''))} and converted to "
                    f"{escape(clock.get('target_label', ''))} through UTC before any result was "
                    f"calculated; every period boundary in this report is on the converted clock.", body))
            story.append(Paragraph(f"<b>Detected Logging Interval:</b> {escape(interval_label)}", body))
        story.append(Paragraph(f"<b>Expected Samples:</b> {expected_samples:,}", body))
        story.append(Paragraph(f"<b>Actual Samples in Dataset:</b> {actual_samples:,}", body))
        if self.weather_screen:
            ws = self.weather_screen
            story.append(Paragraph(
                f"<b>Removed by weather screening:</b> {ws['rows_removed']:,} readings (see Weather "
                "Screening). These are deliberate exclusions, not instrument data loss: the time they "
                "cover is taken out of the expected span rather than counted as missing, and every "
                "result in this report uses only the remaining readings.", body))
        if uptime_pct is not None:
            story.append(Paragraph(f"<b>Data Completeness:</b> {uptime_pct:.2f}%", body))
        story.append(Spacer(1, 0.1 * inch))
        if uptime_pct is not None and uptime_pct < 90.0:
            story.append(Paragraph(
                f"<b>Completeness below 90%</b> ({100 - uptime_pct:.2f}% of the expected readings are "
                f"missing). Whole-period averages may not represent the unmeasured time.", body))
        story.append(Spacer(1, 0.12 * inch))

        # data continuity log
        log = [Paragraph("Data Continuity Log", styles['h2'])]
        if self.source_files:
            log.append(Paragraph(
                f"This record merges {len(self.source_files)} file(s). Continuity was checked across "
                f"every file boundary as well as within each file.", body))
            log.append(Spacer(1, 0.08 * inch))

        gaps = gi.get('gaps') or []
        if not gaps:
            log.append(Paragraph("No interruptions: a reading is present at every logging interval.", body))
        else:
            n_minor = int(gi.get('minor_gap_count') or 0)
            n_major = int(gi.get('major_gap_count') or 0)
            log.append(Paragraph(
                f"{len(gaps)} interruption{'s' if len(gaps) != 1 else ''} with no readings "
                f"({n_minor} shorter than 15 minutes, {n_major} of 15 minutes or longer), totalling "
                f"{format_duration(float(gi.get('missing_seconds') or 0.0))}.", body))
            log.append(Spacer(1, 0.06 * inch))
            log.extend(Paragraph(f"• {escape(g.get('label', ''))}", body) for g in gaps)
        log.extend(Paragraph(f"• {escape(c.get('label', ''))}", body) for c in gi.get('clock_changes') or [])
        log.extend(Paragraph(f"• {escape(x.get('label', ''))}", body) for x in gi.get('excluded') or [])
        story.append(KeepTogether(log) if len(log) <= 25 else log[0])
        if len(log) > 25:
            story.extend(log[1:])
        story.append(Spacer(1, 0.12 * inch))

    # Section 3: global regulatory & health compliance

    def _add_section_3_compliance(self, story, styles, *, ts, leq_col):
        story.append(Paragraph("Section 3: Comparison with Health Guidelines and Regulatory Limits", styles['h1']))

        if getattr(self, 'timestamps_synthetic', False):
            story.append(Paragraph(
                "<b>Comparison withheld.</b> Every standard applied in this report "
                "(WHO 2018 Lden and Lnight, Maryland COMAR daytime and nighttime limits) is "
                "defined over a specific time-of-day window. The date and time information in "
                "this file could not be read, so those windows cannot be established and no "
                "comparison can be made. The overall average level and the statistical "
                "percentiles elsewhere in this report remain valid. Re-export the source file "
                "with a full 'YYYY-MM-DD HH:MM:SS' timestamp column to obtain the comparison.",
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
        # Maryland nighttime: 22:00-07:00; daytime: 07:00-22:00
        laeq_day   = energetic_mean_db(leq[is_day])   if is_day.any()   else None
        laeq_night = energetic_mean_db(leq[is_night]) if is_night.any() else None

        # LAmax (peak level, for WHO bedroom single-event check)
        _, _, lmax_col, _ = self._resolve_acoustic_columns()
        lmax_series = self._get_numeric_series(lmax_col)
        lamax = float(lmax_series.max()) if not lmax_series.dropna().empty else None
        # The WHO bedroom single-event value applies at night: night readings only.
        night = (h >= 23) | (h < 7)
        lmax_night = lmax_series[night.reindex(lmax_series.index, fill_value=False)].dropna()
        lamax_night = float(lmax_night.max()) if not lmax_night.empty else None

        try:
            results = evaluate_compliance(
                lden=measured_lden,
                lnight=measured_lnight,
                ldn=env.get('Ldn') if isinstance(env, dict) else None,
                laeq=laeq_overall,
                laeq_day=laeq_day,
                laeq_night=laeq_night,
                lamax=lamax,
                lamax_night=lamax_night,
                environment=self.environment,
            )
        except Exception as _e:
            logger.warning(f"Compliance evaluation failed: {_e}")
            results = []

        if not results:
            story.append(Paragraph(
                "The comparison could not be computed — timestamps or LEQ values may be missing.",
                styles['BodyText']
            ))
            story.append(Spacer(1, 0.12 * inch))
            return

        cell_8 = ParagraphStyle('Comp8', parent=styles['BodyText'], fontSize=8, leading=10, alignment=TA_LEFT)
        hdr_style = ParagraphStyle('CompHdr', parent=styles['BodyText'], fontSize=8, leading=10,
                                   textColor=colors.whitesmoke, fontName='Helvetica-Bold', alignment=TA_CENTER)
        hdr_style_left = ParagraphStyle('CompHdrL', parent=hdr_style, alignment=TA_LEFT)
        num_style = ParagraphStyle('CompNum', parent=cell_8, alignment=TA_RIGHT)

        assess_pass = ParagraphStyle('AssessPass', parent=cell_8,
                                     textColor=colors.HexColor('#15803d'), fontName='Helvetica-Bold')
        assess_fail = ParagraphStyle('AssessFail', parent=cell_8,
                                     textColor=colors.HexColor('#b91c1c'), fontName='Helvetica-Bold')
        assess_ref  = ParagraphStyle('AssessRef', parent=cell_8,
                                     textColor=colors.HexColor('#6b7280'), fontName='Helvetica-Oblique')

        header_row = [
            Paragraph("Guideline or limit",     hdr_style_left),
            Paragraph("Metric",                 hdr_style_left),
            Paragraph("Measured\n(dB(A))",      hdr_style),
            Paragraph("Value\n(dB(A))",         hdr_style),
            Paragraph("Measured vs value",      hdr_style_left),
        ]

        rows = []
        for r in results:
            delta = r['delta_db']
            if r.get('kind') == 'indicative':
                # Source-specific reference (aircraft/railway), no compliance verdict.
                margin = abs(delta)
                rel = "above" if r['status'] == 'ABOVE' else "below"
                assess_txt = f"{self._fmt_float(margin, 1)} dB {rel} source reference — indicative only"
                assess_style = assess_ref
            elif r['status'] == 'AT OR BELOW':
                margin = abs(delta)
                assess_txt = f"At or below the value by {self._fmt_float(margin, 1)} dB"
                assess_style = assess_pass
            else:
                excess = abs(delta)
                assess_txt = f"Above the value by {self._fmt_float(excess, 1)} dB"
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
            "<i>A level at or below the Maryland COMAR values does not imply absence of health "
            "risk: the WHO 2018 health-based guideline values are lower.</i>",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.10 * inch))

        # How often the level sat above each limit.
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
            "Each row compares the total measured level with a guideline or limit value and states whether "
            "it is above, or at or below, that value. It is not a determination of compliance. "
            "The WHO 2018 guideline values are specific to one noise source (road traffic Lden 53 dB(A) and "
            "Lnight 45 dB(A); aircraft Lden 45 dB(A); railway Lden 54 dB(A)), while the sound level meter "
            "measures the combined sound of all sources and cannot attribute it to one; the aircraft and "
            "railway rows are therefore shown as <b>indicative</b> only. "
            "The Maryland COMAR values apply to noise a person causes at a receiving property; motor vehicles "
            "on public roads, licensed airports, railroads and residential air-conditioning are exempt "
            "(COMAR 26.02.03.02C), and a compliance measurement is made at the receiving property line with a "
            "Type II or better meter (26.02.03.02D). "
            + ("With an indoor microphone, the WHO 1999 bedroom rows compare the night-time LAeq "
               "(23:00–07:00) with 30 dB(A), and show the night-time LAmax against 45 dB for reference "
               "only: that guideline concerns how often 45 dB is exceeded in a night, which a single "
               "maximum cannot decide. " if self.environment == 'indoor' else "") +
            "Additionally, WHO 2018 intends Lden/Lnight to represent long-term annual average exposure; a "
            "measurement period of days or weeks is indicative only.",
            who_note_style
        ))
        story.append(Spacer(1, 0.12 * inch))

    # Section 4: single-event sleep disturbance (l-max)

    def _add_section_4_sleep_disturbance(self, story, styles, *, ts, lmax_col):
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

    # Section 5: daily summary matrix

    def _add_section_5_daily_matrix(self, story, styles):
        story.append(Paragraph("Section 5: Daily Summary Matrix (Multi-Week Variance)", styles['h1']))

        if self.daily_summary is None or self.daily_summary.empty:
            story.append(Paragraph("No daily summary data available.", styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))
            return

        # Every column is bounded to its own period (analysis.periods).
        story.append(Paragraph(
            f"Times are {escape(self.time_basis(self._get_timestamp_series(self._resolve_acoustic_columns()[0])))}. "
            "LAeq is the energy average of the LEQ readings in each period. The night column is the "
            "night that begins on the evening of the date shown; the Lden column covers the 24 hours "
            "from 07:00 on that date. * less than 99.5% of the period was measured (coverage in brackets).",
            styles['BodyText']))
        story.append(Spacer(1, 0.06 * inch))
        header = ["Date", "LAeq 00:00–24:00", "LAeq 07:00–22:00",
                  "LAeq night 22:00–07:00", "Lden 07:00–07:00"]
        data = [[Paragraph(f"<b>{h}</b><br/>dB(A)", self._table_header_style()) for h in header]]

        def cell(value, coverage):
            text = self._fmt_db_plain(value)
            try:
                cov = float(coverage)
            except (TypeError, ValueError):
                return text
            if text not in ('N/A', '') and np.isfinite(cov) and cov < COMPLETE_COVERAGE_PCT:
                return f"{text}* ({math.floor(cov)}%)"
            return text

        for _, row in self.daily_summary.iterrows():
            date_str = pd.Timestamp(row.get('Date')).strftime('%Y-%m-%d (%a)')
            data.append([
                date_str,
                cell(row.get('Average_L_EQ_dB'), row.get('Day_Coverage_pct')),
                cell(row.get('Daytime_LAeq'), row.get('Daytime_Coverage_pct')),
                cell(row.get('Nighttime_LAeq'), row.get('Night_Coverage_pct')),
                cell(row.get('Daily_Lden'), row.get('Lden_Coverage_pct')),
            ])

        table = Table(data, colWidths=[1.6*inch, 1.7*inch, 1.7*inch, 2.0*inch, 1.7*inch], repeatRows=1)
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

    # Section 6: diurnal hourly profile (24-hour cycle)

    def _compute_duration_label(self, ts=None):
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

    def _add_section_6_hourly_profile(self, story, styles, *, ts=None):
        duration_label = self._compute_duration_label(ts)
        story.append(Paragraph(f"Section 6: Diurnal Hourly Profile for {duration_label}", styles['h1']))

        if self.hourly_summary is None or self.hourly_summary.empty:
            story.append(Paragraph("No hourly summary data available.", styles['BodyText']))
            story.append(Spacer(1, 0.12 * inch))
            return

        # Two half-day blocks side by side.
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

    # Section 7: statistical noise profile (percentiles)

    def _add_section_7_percentiles(self, story, styles, *, leq_col):
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

    # Section 8: visualizations

    def _add_section_8_visualizations(self, story, styles, *, ts, leq_col, lmax_col, lmin_col):
        story.append(PageBreak())
        story.append(Paragraph("Section 8: Advanced Visualizations", styles['h1']))

        leq = self._get_numeric_series(leq_col)
        if leq.dropna().empty:
            story.append(Paragraph("LEQ stream not available; visualizations cannot be generated.", styles['BodyText']))
            return

        # Provenance now travels in the caption rather than inside the image.
        src = (f" <b>Data source:</b> {escape(self._figure_source_label())}."
               + self._figure_screen_note())

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
        story.append(PageBreak())
        radar_w = 9.5 / 2 - 0.15   # two columns inside the 9.5 in text block
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
            "exceedance. The comparison in Section 3 uses the correct metrics." + src,
            cap_col
        )

        try:
            fig5 = self._fig_weekly_radar(ts=ts, leq=leq)
        except Exception as e5:
            logger.warning(f"Weekly radar chart failed: {str(e5)[:120]}")
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

    # Chart generation

    def _fig_time_series_with_band(self, *, ts, leq, lmax, lmin,
                                   title=None):
        """Build Chart 1."""
        df = pd.DataFrame({'ts': ts, 'leq': leq})
        if lmax is not None:
            df['lmax'] = lmax
        if lmin is not None:
            df['lmin'] = lmin

        df = df.dropna(subset=['ts', 'leq']).sort_values('ts')
        if df.empty:
            return None, TimeSeriesBinning()

        # Multi-week reports become unreadable if every raw point is plotted.
        span = df['ts'].iloc[-1] - df['ts'].iloc[0]
        freq, bin_label = ts_resample_rule(span)

        df = df.set_index('ts')

        def energy_mean(series):
            clean = series.dropna()
            return energetic_mean_db(clean) if len(clean) else np.nan

        agg = {'leq': energy_mean}
        if 'lmax' in df.columns:
            agg['lmax'] = 'max'
        if 'lmin' in df.columns:
            agg['lmin'] = 'min'

        # keep empty bins as NaN so outages show as gaps
        df_plot = df.resample(freq).agg(agg)
        if df_plot['leq'].notna().sum() == 0:
            df_plot = df.copy()

        # Rolling smooth computed on the already-resampled series (fast path).
        bin_interval = pd.Timedelta(freq if freq[0].isdigit() else f'1{freq}')
        roll_window, roll_span_label = ts_rolling_window(span, bin_interval)
        roll_w = max(3, int(round(roll_window / bin_interval)))
        roll_w = min(roll_w, max(1, len(df_plot)))

        # centred window, a trailing one shifts the trend later
        rolling_1h = df_plot['leq'].rolling(
            window=roll_w, center=True, min_periods=max(1, roll_w // 2)).median()
        rolling_1h = rolling_1h.where(df_plot['leq'].notna())
        roll_label = f'Rolling median ({roll_span_label}, centered)'

        fig = go.Figure()

        # Draw the envelope first so the LAeq trace stays visually dominant.
        if 'lmax' in df_plot.columns and 'lmin' in df_plot.columns:
            # one filled polygon per run of measured bins
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

        # Lden 53 and Lnight 45 dB, drawn for orientation only
        for ref_y, ref_alpha in ((53.0, 0.95), (45.0, 0.7)):
            fig.add_hline(y=ref_y, line_dash='dash',
                          line_color=f'rgba(231,111,81,{ref_alpha})')
            fig.add_annotation(
                x=1.012, xref='paper', xanchor='left',
                y=ref_y, yref='y', yanchor='middle',
                text=f'{ref_y:.0f} dB',
                showarrow=False, align='left',
                name=self.REF_LABEL_ANNOTATION_NAME,
                font=dict(size=9, color='rgba(196,78,52,1)'),
            )

        # Stable y-axis window.
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
                title=f'Time ({self.zone_abbreviation(ts)})',
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

    def _fig_diurnal_box_whisker(self, *, ts, leq, title=None):
        """Generate a diurnal box-and-whisker chart for hourly LEQ volatility."""
        try:
            df = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
            if df.empty:
                return None
            df['hour'] = df['ts'].dt.hour
            fig = go.Figure()

            # COMAR day is 07:00-22:00
            day_hours = set(range(7, 22))
            # Colourblind-safe pair (Okabe-Ito orange / report slate blue).
            DAY_LINE, DAY_FILL = '#B37700', 'rgba(230,159,0,0.35)'
            NIGHT_LINE, NIGHT_FILL = '#293241', 'rgba(61,90,128,0.45)'

            for h in range(24):
                hour_values = pd.to_numeric(df.loc[df['hour'] == h, 'leq'], errors='coerce').dropna()
                if hour_values.empty:
                    continue

                # Compute exact statistics from ALL data, no sampling, no accuracy loss.
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
            NIGHT_SPANS = ((-0.5, 6.5), (21.5, 23.5))
            DAY_SPAN = (6.5, 21.5)
            for x0, x1 in NIGHT_SPANS:
                fig.add_vrect(x0=x0, x1=x1, fillcolor='rgba(44,62,80,0.07)',
                              line_width=0, layer='below')

            # COMAR limits, imported so they match the technical report
            limit_spans = [
                (MD_RESIDENTIAL_DAY, [DAY_SPAN], 'rgba(179,119,0,0.95)',
                 f'COMAR day limit {MD_RESIDENTIAL_DAY:.0f} dB(A), 07:00–22:00'),
                (MD_RESIDENTIAL_NIGHT, list(NIGHT_SPANS), 'rgba(41,50,65,0.95)',
                 f'COMAR night limit {MD_RESIDENTIAL_NIGHT:.0f} dB(A), 22:00–07:00'),
            ]
            # Drawn as shapes, not traces.
            for limit_db, spans, colour, _legend_name in limit_spans:
                for x0, x1 in spans:
                    fig.add_shape(type='line', xref='x', yref='y',
                                  x0=x0, x1=x1, y0=limit_db, y1=limit_db,
                                  line=dict(color=colour, width=2, dash='dash'),
                                  layer='above')

            # Legend keys.
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
                    title=f'Hour of day ({self.zone_abbreviation(ts)})',
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

    def _fig_temporal_heatmap(self, *, ts, leq, title=None):
        """Generate temporal heatmap (date x hour)."""
        df = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
        if df.empty:
            return None

        # Use full datetime (floor to midnight) for dates to allow stable sorting
        df['date'] = pd.to_datetime(df['ts']).dt.floor('D')
        df['hour'] = pd.to_datetime(df['ts']).dt.hour

        def grp_energetic_mean(x):
            return energetic_mean_db(x)

        agg = (
            df.groupby(['date', 'hour'])['leq']
            .apply(grp_energetic_mean)
            .reset_index(name='laeq')
        )

        pivot = agg.pivot(index='hour', columns='date', values='laeq')
        # index covers 0..23 in order
        pivot = pivot.reindex(index=list(range(24)))

        # continuous date columns from min to max
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

        # y is already a list of HH:00 strings.
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
            xaxis_title='Date', yaxis_title=f'Hour of day ({self.zone_abbreviation(ts)})',
            margin=dict(l=80, r=20, t=85, b=85), height=520,
            plot_bgcolor='white',
            paper_bgcolor='white',
        )
        fig.update_traces(colorbar=dict(title=dict(text='LAeq dB(A)')),
                          selector=dict(type='heatmap'))
        self._apply_chart_typography(
            fig, title=title or 'Chart 3: Temporal Heatmap (LAeq intensity by date & hour)')
        self._add_source_annotation(fig, y=-0.22)

        # label every second hour so the ticks don't overlap
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

    def _fig_diurnal_radar(self, *, ts, leq):
        """24-spoke polar radar: energy-averaged LAeq by hour of day (0–23)."""
        # Build hourly LAeq from summary if already computed, else from raw series
        hourly_leq = {}

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

        # Night sector shading, 22:00-07:00 (Maryland COMAR night period).
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

    def _fig_weekly_radar(self, *, ts, leq):
        """7-spoke radar: energy-averaged LAeq by day of week, daytime vs nighttime."""
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
        df['is_day'] = df['hour'].between(7, 21)   # 07:00-21:59 inclusive = daytime

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

        # Orientation rings.
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

    # Pdf rendering & utilities

    def _plotly_fig_to_image(self, fig, width_inch=8, height_inch=4):
        if fig is None:
            return None
        try:
            scale = 1.25 if len(self.df) > 500_000 else 2
            img_bytes = fig.to_image(format="png", scale=scale)
            img = Image(io.BytesIO(img_bytes))
            # Keep the PNG on the flowable.
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
