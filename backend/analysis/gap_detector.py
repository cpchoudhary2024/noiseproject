# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""
Forensic Gap Detector for environmental noise datasets.

Calculates the modal logging interval, then flags every consecutive pair of
rows whose time delta exceeds 2× that interval as a data gap.

Gap categories: Minor (< 15 minutes without readings) and Major (≥ 15 minutes).
The cause of a gap is not stated: the record shows only that readings are absent.

Daylight-saving clock changes are not gaps. Loggers that follow local wall-clock
time jump from 01:59:59 to 03:00:00 when daylight saving begins; no reading is
lost, so the jump is reported separately and excluded from missing time and
completeness.
"""
from __future__ import annotations

import logging
import math
import os
import pandas as pd
from dataclasses import dataclass, field

from analysis.timestamp_utils import parse_timestamps_robust
from analysis.clock import FOLD_COLUMN, ordering_key

logger = logging.getLogger(__name__)

MINOR_GAP_THRESHOLD_MIN: float = 15.0   # minutes below which a gap is "Minor"

# Time zone of the logger clocks, used only to recognise daylight-saving jumps.
# The study loggers are in Maryland (US Eastern) and follow wall-clock time.
LOGGER_TIMEZONE: str = os.environ.get('LOGGER_TIMEZONE', 'America/New_York')


def _warn_if_dropped(before: int, after: int, where: str) -> None:
    """Loudly log when timestamp de-duplication removes a non-trivial fraction.

    Guarantees mass row loss can never happen *silently*: a logger that stored
    sub-interval samples at a coarser time resolution must have its timestamps
    expanded (see ``_expand_tied_timestamps``) before merging, otherwise up to
    59/60 of the data could be discarded here.
    """
    dropped = before - after
    if dropped > 0 and before > 0 and (dropped / before) > 0.02:
        logger.warning(
            "[MERGE] %s: dropped %d/%d rows (%.1f%%) as duplicate timestamps — "
            "verify sub-minute timestamp expansion ran; no measurement rows should be lost.",
            where, dropped, before, 100.0 * dropped / before,
        )


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class DataGap:
    gap_start: pd.Timestamp
    gap_end: pd.Timestamp
    duration_seconds: float
    category: str   # "Minor" | "Major"
    reason: str     # human-readable cause


@dataclass
class ClockChange:
    before: pd.Timestamp   # last reading before the jump (wall clock)
    after: pd.Timestamp    # first reading after the jump (wall clock)
    shift_seconds: float   # wall-clock advance beyond real elapsed time (3600 for DST)
    position: int = -1     # position of ``after`` in the series searched
    elapsed_seconds: float = 0.0  # real time between the two readings


@dataclass
class GapReport:
    gaps: list[DataGap] = field(default_factory=list)
    clock_changes: list[ClockChange] = field(default_factory=list)
    excluded: list = field(default_factory=list)   # (before, after, step_seconds)
    expected_rows: int = 0                          # readings expected over the measured span
    completeness_exact: float = 100.0               # unrounded; display via floor_pct
    total_rows: int = 0
    actual_span_seconds: float = 0.0
    missing_seconds: float = 0.0
    uptime_pct: float = 100.0
    logging_interval_seconds: float = 1.0
    continuous: bool = True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _modal_interval_seconds(ts: pd.Series) -> float:
    """Return the modal positive time delta (the standard logging interval)."""
    deltas = ts.sort_values().diff().dropna().dt.total_seconds()
    positives = deltas[deltas > 0]
    if positives.empty:
        return 1.0
    mode = positives.mode()
    return float(mode.iloc[0]) if not mode.empty else float(positives.median())


def _is_nonexistent_local(t: pd.Timestamp, tz: str) -> bool:
    """True when ``t`` falls in the hour skipped when daylight saving begins."""
    try:
        return pd.isna(pd.Timestamp(t).tz_localize(tz, nonexistent='NaT', ambiguous=False))
    except Exception:
        return False


def find_clock_changes(ts: pd.Series, interval_sec: float | None = None,
                       tz: str = LOGGER_TIMEZONE) -> list[ClockChange]:
    """Forward clock jumps caused by daylight saving, in a wall-clock series.

    For every step longer than two logging intervals, the real elapsed time is
    taken through UTC in zone ``tz``. When the wall clock advanced more than the
    real time did, the difference (one hour for daylight saving) is a clock
    change, not missing data. Any real outage in the same step is kept in
    ``elapsed_seconds`` so it is still reported as a gap.

    Parameters
    ----------
    ts : pd.Series
        Naive wall-clock timestamps in elapsed-time order (wall time itself,
        except that a repeated November hour steps backwards).
    interval_sec : float, optional
        Logging interval (s); the modal interval when omitted.
    tz : str
        IANA zone of the timestamps.
    """
    ts = pd.to_datetime(ts, errors='coerce').dropna().reset_index(drop=True)
    if len(ts) < 2:
        return []
    interval = interval_sec or _modal_interval_seconds(ts)
    deltas = ts.diff().dt.total_seconds()
    cand = deltas[deltas > max(2.0 * interval, 2.0)]
    if cand.empty:
        return []
    before = ts.iloc[cand.index - 1].reset_index(drop=True)
    after = ts.iloc[cand.index].reset_index(drop=True)
    try:
        b_utc = before.dt.tz_localize(tz, ambiguous=False, nonexistent='shift_forward')
        a_utc = after.dt.tz_localize(tz, ambiguous=False, nonexistent='shift_forward')
    except Exception:
        return []
    elapsed = (a_utc - b_utc).dt.total_seconds().to_numpy()
    out = []
    for k, (idx, d) in enumerate(cand.items()):
        shift = float(d - elapsed[k])
        if shift >= 3600.0 - interval:
            out.append(ClockChange(before=before.iloc[k], after=after.iloc[k], shift_seconds=shift,
                                   position=int(idx), elapsed_seconds=float(elapsed[k])))
    return out


def _dst_transitions(start: pd.Timestamp, end: pd.Timestamp, tz: str) -> list[pd.Timestamp]:
    """Local wall times at which ``tz`` changes UTC offset within [start, end]."""
    probes = pd.date_range(start.floor('h'), end.ceil('h'), freq='h')
    if len(probes) < 2:
        return []
    offsets = probes.tz_localize(tz, nonexistent='shift_forward', ambiguous=False).map(lambda t: t.utcoffset())
    return [probes[i] for i in range(1, len(probes)) if offsets[i] != offsets[i - 1]]


def describe_logger_clock(ts: pd.Series, fold: pd.Series | None = None,
                          tz: str = LOGGER_TIMEZONE) -> dict:
    """What the record itself shows about the logger clock.

    ``follows_dst`` is True when the record jumps or repeats an hour exactly at a
    daylight-saving change of ``tz``, False when it spans such a change without
    doing so, and None when it does not span one (the data cannot tell).
    """
    ts = pd.to_datetime(ts, errors='coerce').dropna()
    if len(ts) < 2:
        return {'follows_dst': None, 'evidence': ''}
    key = ordering_key(ts, fold.loc[ts.index] if fold is not None else None).sort_values(kind='mergesort')
    wall = ts.loc[key.index].reset_index(drop=True)
    jumps = find_clock_changes(wall, None, tz)
    repeated = bool(fold is not None and fold.any())
    if jumps or repeated:
        parts = [f"the clock jumps from {c.before:%H:%M:%S} to {c.after:%H:%M:%S} on {c.before:%d %b %Y}"
                 for c in jumps]
        if repeated:
            first = ts[fold.loc[ts.index].astype(bool)].min()
            parts.append(f"01:00–01:59 is recorded twice on {first:%d %b %Y}")
        return {'follows_dst': True,
                'evidence': 'In this record ' + '; '.join(parts) + ', so the logger clock follows daylight saving time.'}
    changes = _dst_transitions(ts.min(), ts.max(), tz)
    if changes:
        return {'follows_dst': False,
                'evidence': (f"This record spans the daylight-saving change on {changes[0]:%d %b %Y} "
                             f"without a clock jump, so the logger clock does not follow daylight saving time.")}
    return {'follows_dst': None,
            'evidence': 'This record does not span a daylight-saving change, so the data cannot show whether the logger clock follows it.'}


def data_completeness_pct(ts: pd.Series, actual_count: int | None = None,
                          fold: pd.Series | None = None, tz: str | None = None) -> float | None:
    """Interval-aware data completeness (%) — robust to non-1 Hz logging.

    ``expected = span / modal_interval + 1``, so a logger sampling every 2 s
    (or every minute) is **not** falsely reported as having lost data, which a
    fixed "1 Hz" assumption would do.

    Parameters
    ----------
    ts : pd.Series
        Timestamps in any parseable form.
    actual_count : int, optional
        Number of samples actually present. Defaults to the count of valid
        (non-NaT) timestamps.

    Returns
    -------
    float | None
        Completeness percentage capped at 100, or None when undeterminable.
    """
    ts = pd.to_datetime(ts, errors='coerce')
    key_all = ordering_key(ts, fold)
    order = key_all.dropna().sort_values(kind='mergesort').index
    key = key_all.loc[order]
    if len(key) < 2:
        return None
    interval = _modal_interval_seconds(key)
    span = (key.iloc[-1] - key.iloc[0]).total_seconds()
    if span <= 0 or interval <= 0:
        return None
    wall = ts.loc[order].reset_index(drop=True)
    span -= sum(c.shift_seconds for c in find_clock_changes(wall, interval, tz or LOGGER_TIMEZONE))
    expected = span / interval + 1.0
    actual = float(actual_count) if actual_count is not None else float(len(ts))
    # Unrounded: rounding here turned 99.99% into "100.0" and the summary then
    # declared a record with real gaps "complete". Callers floor for display.
    return min(100.0, 100.0 * actual / max(1.0, expected))


def format_duration(seconds: float) -> str:
    """Exact duration, e.g. '1 min 31 s', '2 h 5 min', '45 s'."""
    s = int(round(seconds))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h} h {m} min" if m else f"{h} h"
    if m:
        return f"{m} min {sec} s" if sec else f"{m} min"
    return f"{sec} s"


def floor_pct(value: float | None, decimals: int = 1) -> float | None:
    """Floor a percentage for display so missing data never rounds up to 100%."""
    if value is None:
        return None
    f = 10 ** decimals
    return math.floor(float(value) * f) / f


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def detect_gaps(df: pd.DataFrame, time_col: str, tz: str | None = None) -> GapReport:
    """
    Analyse a dataframe for temporal gaps.
    Returns a GapReport with all gaps, clock changes and uptime statistics.

    ``tz`` is the zone the timestamps are expressed in; by default the report
    clock recorded in ``df.attrs['clock']``.
    """
    report = GapReport()
    tz = tz or (df.attrs.get('clock') or {}).get('target') or LOGGER_TIMEZONE

    # Sort on the repeated-hour-aware key so deltas are real elapsed time even
    # across the hour the clock repeats when daylight saving ends.
    fold = df[FOLD_COLUMN] if FOLD_COLUMN in df.columns else None
    wall_all = parse_timestamps_robust(df[time_col])[0]
    key_all = ordering_key(wall_all, fold)
    order = key_all.dropna().sort_values(kind='mergesort').index
    ts = key_all.loc[order].reset_index(drop=True)      # elapsed-time order
    wall = wall_all.loc[order].reset_index(drop=True)   # wall-clock labels

    report.total_rows = len(ts)
    if len(ts) < 2:
        return report

    interval_sec = _modal_interval_seconds(ts)
    report.logging_interval_seconds = interval_sec

    span_sec = (ts.iloc[-1] - ts.iloc[0]).total_seconds()
    report.actual_span_seconds = span_sec

    # A gap is any consecutive delta greater than 2× the modal interval
    # (the 2× tolerance handles minor jitter without false positives).
    gap_threshold_sec = max(interval_sec * 2.0, 2.0)

    # Vectorised gap detection — avoids a Python loop over potentially millions of rows.
    deltas = ts.diff().dt.total_seconds()          # NaN at position 0
    gap_mask = deltas > gap_threshold_sec

    # Daylight-saving jumps are clock changes, not missing readings.
    report.clock_changes = find_clock_changes(wall, interval_sec, tz)
    if report.clock_changes:
        # Measure these steps in real elapsed time: the clock change is removed,
        # any genuine outage in the same step remains a gap.
        for c in report.clock_changes:
            deltas.iloc[c.position] = c.elapsed_seconds
        gap_mask = deltas > gap_threshold_sec
        span_sec -= sum(c.shift_seconds for c in report.clock_changes)
    # Steps that fall inside a window the analyst excluded are exclusions, not
    # data loss: they are listed separately and left out of missing time and
    # completeness (the excluded time is removed from the expected span).
    report.excluded = []
    for es, ee in (df.attrs.get('exclusions') or []):
        tol = pd.Timedelta(seconds=interval_sec)
        inside = gap_mask & (wall.shift(1) >= es - tol) & (wall <= ee + tol) & (wall.shift(1) < ee) & (wall > es)
        for idx in inside[inside].index:
            gap_mask.iloc[idx] = False
            report.excluded.append((pd.Timestamp(es), pd.Timestamp(ee), float(deltas.iloc[idx])))
            span_sec -= max(0.0, float(deltas.iloc[idx]) - interval_sec)
    gap_indices = gap_mask[gap_mask].index.tolist()

    missing_total: float = float(
        (deltas[gap_mask] - interval_sec).clip(lower=0).sum()
    )

    gaps: list[DataGap] = []
    for idx in gap_indices:
        delta_sec = float(deltas.iloc[idx])
        dur_min = (delta_sec - interval_sec) / 60.0
        category = "Minor" if dur_min < MINOR_GAP_THRESHOLD_MIN else "Major"
        reason = ""

        gaps.append(DataGap(
            gap_start=wall.iloc[idx - 1],
            gap_end=wall.iloc[idx],
            duration_seconds=delta_sec,
            category=category,
            reason=reason,
        ))

    report.gaps = gaps
    report.missing_seconds = missing_total
    report.continuous = len(gaps) == 0

    # Uptime = fraction of the expected row-count that is actually present
    expected_rows = max(int(round(span_sec / interval_sec)) + 1, report.total_rows)
    report.expected_rows = expected_rows
    report.completeness_exact = min(100.0, (report.total_rows / expected_rows) * 100.0)
    report.uptime_pct = floor_pct(report.completeness_exact, 2)

    return report


def gap_report_to_dict(report: GapReport) -> dict:
    """Serialise GapReport to a JSON-safe dict for the API response."""
    gaps_list = []
    for g in report.gaps:
        # Time without readings = interval between the readings either side,
        # less one logging interval (the next reading was due one interval on).
        missing = max(0.0, g.duration_seconds - report.logging_interval_seconds)
        label = (
            f"No readings between {g.gap_start.strftime('%Y-%m-%d %H:%M:%S')} and "
            f"{g.gap_end.strftime('%Y-%m-%d %H:%M:%S')} ({format_duration(missing)})."
        )
        gaps_list.append({
            "start":            g.gap_start.strftime('%Y-%m-%d %H:%M:%S'),
            "end":              g.gap_end.strftime('%Y-%m-%d %H:%M:%S'),
            "duration_seconds": round(g.duration_seconds, 1),
            "missing_seconds":  round(missing, 1),
            "duration_human":   format_duration(missing),
            "category":         g.category,
            "reason":           g.reason,
            "label":            label,
        })

    clock_list = [{
        "before": c.before.strftime('%Y-%m-%d %H:%M:%S'),
        "after":  c.after.strftime('%Y-%m-%d %H:%M:%S'),
        "label":  (f"Daylight saving time began between {c.before.strftime('%Y-%m-%d %H:%M:%S')} and "
                   f"{c.after.strftime('%H:%M:%S')}: clocks moved forward one hour. The skipped hour "
                   f"is not counted as missing data."),
    } for c in report.clock_changes]

    excluded_list = [{
        "start": a.strftime('%Y-%m-%d %H:%M:%S'),
        "end": b.strftime('%Y-%m-%d %H:%M:%S'),
        "label": (f"Excluded by the analyst: {a.strftime('%Y-%m-%d %H:%M:%S')} to "
                  f"{b.strftime('%Y-%m-%d %H:%M:%S')}, as entered "
                  f"({format_duration(max(0.0, d - report.logging_interval_seconds))} of readings removed)."),
    } for a, b, d in report.excluded]

    return {
        "continuous":               report.continuous,
        "gaps":                     gaps_list,
        "clock_changes":            clock_list,
        "excluded":                 excluded_list,
        "gap_count":                len(report.gaps),
        "minor_gap_count":          sum(1 for g in report.gaps if g.category == "Minor"),
        "major_gap_count":          sum(1 for g in report.gaps if g.category == "Major"),
        "total_rows":               report.total_rows,
        "actual_span_seconds":      round(report.actual_span_seconds, 1),
        "missing_seconds":          round(report.missing_seconds, 1),
        "uptime_pct":               report.uptime_pct,
        "completeness_exact":       report.completeness_exact,
        "expected_rows":            report.expected_rows,
        "logging_interval_seconds": round(report.logging_interval_seconds, 2),
    }


# ---------------------------------------------------------------------------
# Multi-file merging
# ---------------------------------------------------------------------------

def _identify_acoustic_columns(df: pd.DataFrame) -> tuple[str | None, str | None, str | None, str | None]:
    """
    Return (time_col, leq_col, lmax_col, lmin_col) from a dataframe.
    Tries common name patterns; falls back to heuristics.
    """
    cols_lower = {c: c.lower() for c in df.columns}

    def _find(patterns: list[str]) -> str | None:
        for pat in patterns:
            for col, cl in cols_lower.items():
                if pat in cl:
                    return col
        return None

    time_col = _find(['timestamp', 'datetime', 'time', 'date'])
    leq_col  = _find(['leq', 'laeq', 'l_eq', 'l-eq'])
    lmax_col = _find(['lmax', 'l-max', 'l_max', 'max'])
    lmin_col = _find(['lmin', 'l-min', 'l_min', 'min'])

    # Fallback: if no leq found, pick first numeric column that isn't time/max/min
    if not leq_col:
        num_cols = df.select_dtypes(include='number').columns.tolist()
        for c in num_cols:
            cl = c.lower()
            if not any(k in cl for k in ['time', 'date', 'hour', 'min', 'max', 'id']):
                leq_col = c
                break

    return time_col, leq_col, lmax_col, lmin_col


_CANONICAL_TIME = '__merge_ts__'
_CANONICAL_LEQ  = '__merge_leq__'
_CANONICAL_LMAX = '__merge_lmax__'
_CANONICAL_LMIN = '__merge_lmin__'


def _canonicalize(df: pd.DataFrame) -> pd.DataFrame:
    """
    Rename each file's acoustic columns to canonical internal names before concat.

    This prevents pd.concat from NaN-filling rows when two files use different
    column names for the same measurement (e.g. 'Timestamp' vs 'Time', 'L_EQ_dB'
    vs 'LEQ_dB').  Non-acoustic columns are left as-is.
    """
    time_col, leq_col, lmax_col, lmin_col = _identify_acoustic_columns(df)
    df = df.copy()
    rename: dict[str, str] = {}
    if time_col:
        rename[time_col] = _CANONICAL_TIME
    if leq_col and leq_col != time_col:
        rename[leq_col] = _CANONICAL_LEQ
    if lmax_col and lmax_col not in (time_col, leq_col):
        rename[lmax_col] = _CANONICAL_LMAX
    if lmin_col and lmin_col not in (time_col, leq_col, lmax_col):
        rename[lmin_col] = _CANONICAL_LMIN
    if rename:
        df = df.rename(columns=rename)
    return df


def _sort_dedupe(df: pd.DataFrame, time_col: str) -> pd.DataFrame:
    """Chronological sort and removal of rows recorded twice (overlapping files).

    A row is a duplicate only when both its timestamp and its daylight-saving
    pass match another row, so the second pass of a repeated hour is kept.
    """
    fold = df[FOLD_COLUMN] if FOLD_COLUMN in df.columns else None
    key = ordering_key(df[time_col], fold)
    df = df.loc[key.sort_values(kind='mergesort').index]
    subset = [time_col] + ([FOLD_COLUMN] if fold is not None else [])
    return df.drop_duplicates(subset=subset).reset_index(drop=True)


def merge_dataframes(dfs: list[pd.DataFrame]) -> tuple[pd.DataFrame, str | None]:
    """
    Merge multiple dataframes into one master dataframe:
      1. Canonicalize acoustic column names so concat never NaN-fills key columns
      2. Concatenate
      3. Sort chronologically
      4. Drop duplicate timestamps

    Returns (merged_df, time_col_name).  The returned time column name is the
    canonical '__merge_ts__' sentinel when files had different time column names;
    callers that need a human-readable name should fall back to any 'time'-like
    column in the result.
    """
    if not dfs:
        return pd.DataFrame(), None
    if len(dfs) == 1:
        time_col, _, _, _ = _identify_acoustic_columns(dfs[0])
        if time_col and time_col in dfs[0].columns:
            df = dfs[0].copy()
            df[time_col] = parse_timestamps_robust(df[time_col])[0]
            df = df.dropna(subset=[time_col])
            _before = len(df)
            df = _sort_dedupe(df, time_col)
            _warn_if_dropped(_before, len(df), "single-file")
            return df, time_col
        return dfs[0].copy(), None

    # Identify original column names from the first file (used to rename back after merge)
    orig_time, orig_leq, orig_lmax, orig_lmin = _identify_acoustic_columns(dfs[0])

    # Canonicalize each df so acoustic columns share names across all files
    canonical_dfs = [_canonicalize(df) for df in dfs]

    merged = pd.concat(canonical_dfs, ignore_index=True)

    time_col = _CANONICAL_TIME if _CANONICAL_TIME in merged.columns else None

    if time_col:
        merged[time_col] = parse_timestamps_robust(merged[time_col])[0]
        merged = merged.dropna(subset=[time_col])
        if FOLD_COLUMN in merged.columns:
            merged[FOLD_COLUMN] = merged[FOLD_COLUMN].fillna(0).astype('int8')
        _before = len(merged)
        merged = _sort_dedupe(merged, time_col)
        _warn_if_dropped(_before, len(merged), "merge")

    # Rename canonical columns back to original names so downstream code can find them
    back_rename: dict[str, str] = {}
    if _CANONICAL_TIME in merged.columns and orig_time:
        back_rename[_CANONICAL_TIME] = orig_time
    if _CANONICAL_LEQ in merged.columns and orig_leq:
        back_rename[_CANONICAL_LEQ] = orig_leq
    if _CANONICAL_LMAX in merged.columns and orig_lmax:
        back_rename[_CANONICAL_LMAX] = orig_lmax
    if _CANONICAL_LMIN in merged.columns and orig_lmin:
        back_rename[_CANONICAL_LMIN] = orig_lmin
    if back_rename:
        merged = merged.rename(columns=back_rename)

    # Return the original time column name (not the internal canonical name)
    final_time_col = orig_time if (orig_time and orig_time in merged.columns) else time_col
    return merged, final_time_col
