"""
Forensic Gap Detector for environmental noise datasets.

Calculates the modal logging interval, then flags every consecutive pair of
rows whose time delta exceeds 2× that interval as a data gap.

Gap categories (as per MODULE 6 spec):
  Minor  — gap < 15 minutes  → "Probable Device Restart / Calibration"
  Major  — gap ≥ 15 minutes  → "Probable Power Supply Failure / System Crash"
"""
from __future__ import annotations

import logging
import math
import pandas as pd
from dataclasses import dataclass, field

from analysis.timestamp_utils import parse_timestamps_robust

logger = logging.getLogger(__name__)

MINOR_GAP_THRESHOLD_MIN: float = 15.0   # minutes below which a gap is "Minor"


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
class GapReport:
    gaps: list[DataGap] = field(default_factory=list)
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


def data_completeness_pct(ts: pd.Series, actual_count: int | None = None) -> float | None:
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
    ts = pd.to_datetime(ts, errors='coerce').dropna().sort_values()
    if len(ts) < 2:
        return None
    interval = _modal_interval_seconds(ts)
    span = (ts.iloc[-1] - ts.iloc[0]).total_seconds()
    if span <= 0 or interval <= 0:
        return None
    expected = span / interval + 1.0
    actual = float(actual_count) if actual_count is not None else float(len(ts))
    return round(min(100.0, 100.0 * actual / max(1.0, expected)), 1)


def _fmt_duration(seconds: float) -> str:
    """Human-readable duration string."""
    if seconds < 60:
        return f"{int(seconds)} second{'s' if seconds != 1 else ''}"
    if seconds < 3600:
        m = round(seconds / 60)
        return f"{m} minute{'s' if m != 1 else ''}"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} min" if m else "")


# ---------------------------------------------------------------------------
# Core detection
# ---------------------------------------------------------------------------

def detect_gaps(df: pd.DataFrame, time_col: str) -> GapReport:
    """
    Analyse a (sorted, deduplicated) dataframe for temporal gaps.
    Returns a GapReport with all gaps and uptime statistics.
    """
    report = GapReport()

    ts = parse_timestamps_robust(df[time_col])[0].dropna()
    ts = ts.sort_values().reset_index(drop=True)

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
    gap_indices = gap_mask[gap_mask].index.tolist()

    missing_total: float = float(
        (deltas[gap_mask] - interval_sec).clip(lower=0).sum()
    )

    gaps: list[DataGap] = []
    for idx in gap_indices:
        delta_sec = float(deltas.iloc[idx])
        dur_min = delta_sec / 60.0
        if dur_min < MINOR_GAP_THRESHOLD_MIN:
            category = "Minor"
            reason = "Probable Device Restart / Calibration"
        else:
            category = "Major"
            reason = "Probable Power Supply Failure / System Crash"

        gaps.append(DataGap(
            gap_start=ts.iloc[idx - 1],
            gap_end=ts.iloc[idx],
            duration_seconds=delta_sec,
            category=category,
            reason=reason,
        ))

    report.gaps = gaps
    report.missing_seconds = missing_total
    report.continuous = len(gaps) == 0

    # Uptime = fraction of the expected row-count that is actually present
    expected_rows = max(int(span_sec / interval_sec) + 1, report.total_rows)
    report.uptime_pct = round(min(100.0, (report.total_rows / expected_rows) * 100.0), 1)

    return report


def gap_report_to_dict(report: GapReport) -> dict:
    """Serialise GapReport to a JSON-safe dict for the API response."""
    gaps_list = []
    for g in report.gaps:
        label = (
            f"Gap Detected: {g.gap_start.strftime('%Y-%m-%d %H:%M:%S')} to "
            f"{g.gap_end.strftime('%H:%M:%S')} "
            f"({_fmt_duration(g.duration_seconds)}) — {g.reason}."
        )
        gaps_list.append({
            "start":            g.gap_start.strftime('%Y-%m-%d %H:%M:%S'),
            "end":              g.gap_end.strftime('%Y-%m-%d %H:%M:%S'),
            "duration_seconds": round(g.duration_seconds, 1),
            "duration_human":   _fmt_duration(g.duration_seconds),
            "category":         g.category,
            "reason":           g.reason,
            "label":            label,
        })

    return {
        "continuous":               report.continuous,
        "gaps":                     gaps_list,
        "gap_count":                len(report.gaps),
        "minor_gap_count":          sum(1 for g in report.gaps if g.category == "Minor"),
        "major_gap_count":          sum(1 for g in report.gaps if g.category == "Major"),
        "total_rows":               report.total_rows,
        "actual_span_seconds":      round(report.actual_span_seconds, 1),
        "missing_seconds":          round(report.missing_seconds, 1),
        "uptime_pct":               report.uptime_pct,
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
            df = df.sort_values(time_col).drop_duplicates(subset=[time_col]).reset_index(drop=True)
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
        _before = len(merged)
        merged = merged.sort_values(time_col).drop_duplicates(subset=[time_col]).reset_index(drop=True)
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
