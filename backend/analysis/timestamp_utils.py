"""Robust timestamp parsing and integrity assessment.

Device exports are inconsistent: some carry full ISO datetimes, some carry
locale-specific ``dd/mm/yyyy`` strings, and some corrupted CSV re-exports lose
the date entirely (Excel reformats a datetime column down to ``MM:SS.s``).

This module:
  1. Attempts to *recover* a usable timestamp series from several common formats.
  2. *Assesses* whether the recovered series is trustworthy, so callers can warn
     the user and suppress time-dependent outputs (Lden/Lnight, diurnal, daily,
     heatmap) instead of silently presenting fabricated values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

# A bare ``MM:SS`` / ``MM:SS.s`` / ``HH:MM:SS`` token with no date component.
# These cannot be located on a calendar — the date was lost in export.
_TIME_ONLY_RE = re.compile(r"^\s*\d{1,3}:\d{2}(:\d{2})?(\.\d+)?\s*$")

# Minimum fraction of rows that must parse for the series to be usable.
_MIN_PARSE_FRACTION = 0.60

# Explicit formats tried (in order) when pandas' inference parses too few rows.
_EXPLICIT_FORMATS = [
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S.%f",
    "%d/%m/%Y %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%d-%m-%Y %H:%M:%S",
    "%d.%m.%Y %H:%M:%S",
]


@dataclass
class TimestampIntegrity:
    """Verdict on a parsed timestamp series."""
    status: str            # 'ok' | 'recovered' | 'unusable'
    parsed: pd.Series | None  # best-effort parsed datetimes (NaT where unparseable)
    n_total: int = 0
    n_parsed: int = 0
    parsed_pct: float = 0.0
    unique_dates: int = 0
    span_days: float = 0.0
    method: str = ""       # which strategy succeeded
    message: str = ""
    time_metrics_valid: bool = True

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "n_total": int(self.n_total),
            "n_parsed": int(self.n_parsed),
            "parsed_pct": round(float(self.parsed_pct), 1),
            "unique_dates": int(self.unique_dates),
            "span_days": round(float(self.span_days), 2),
            "method": self.method,
            "message": self.message,
            "time_metrics_valid": bool(self.time_metrics_valid),
        }


def _looks_time_only(raw: pd.Series, sample: int = 200) -> bool:
    """True if the sampled raw values are bare time tokens with no date."""
    vals = raw.dropna().astype(str).head(sample)
    if vals.empty:
        return False
    hits = sum(1 for v in vals if _TIME_ONLY_RE.match(v))
    return hits / len(vals) >= 0.8


def parse_timestamps_robust(raw: pd.Series) -> tuple[pd.Series, str]:
    """Recover a datetime series from messy input.

    Returns ``(parsed, method)`` where ``parsed`` is a datetime64 Series aligned
    to ``raw`` (NaT where unparseable) and ``method`` names the winning strategy.
    """
    n = len(raw)
    if n == 0:
        return pd.Series(pd.to_datetime([]), dtype="datetime64[ns]"), "empty"

    # Already datetime-typed (e.g. native xlsx datetimes) — trust it.
    if pd.api.types.is_datetime64_any_dtype(raw):
        return pd.to_datetime(raw, errors="coerce"), "native"

    def _frac(s: pd.Series) -> float:
        return float(s.notna().sum()) / max(1, n)

    # Year-first dates (e.g. '2026/05/12 18:36:28.000') are UNAMBIGUOUS and must
    # be parsed month-first. Under dayfirst=True pandas swaps month/day — and
    # when every day-of-month is <= 12 it still parses 100% but silently yields
    # WRONG dates ('2026/03/05' -> May 3, '2026/03/06' -> Jun 3 ...), scattering
    # consecutive samples across different months and inventing huge gaps. So
    # detect year-first explicitly and force month-first; only genuinely
    # ambiguous day-first inputs use the dayfirst heuristic.
    sample = raw.dropna().astype(str).str.strip()
    year_first = bool(
        not sample.empty
        and sample.head(1000).str.match(r"\d{4}[-/]\d{1,2}[-/]\d{1,2}").mean() >= 0.5
    )
    if year_first:
        best = pd.to_datetime(raw, errors="coerce", dayfirst=False)
        best_method = "inferred-yearfirst"
        best_frac = _frac(best)
    else:
        best = pd.to_datetime(raw, errors="coerce", dayfirst=True)
        best_method = "inferred-dayfirst"
        best_frac = _frac(best)
        # Fall back to month-first if it recovers strictly more rows.
        cand = pd.to_datetime(raw, errors="coerce", dayfirst=False)
        if _frac(cand) > best_frac:
            best, best_frac, best_method = cand, _frac(cand), "inferred-monthfirst"

    # Try explicit formats only if inference is weak.
    if best_frac < 0.95:
        for fmt in _EXPLICIT_FORMATS:
            cand = pd.to_datetime(raw, errors="coerce", format=fmt)
            f = _frac(cand)
            if f > best_frac:
                best, best_frac, best_method = cand, f, f"format:{fmt}"
                if best_frac >= 0.99:
                    break

    # Numeric epoch fallback (seconds or milliseconds since 1970).
    if best_frac < _MIN_PARSE_FRACTION:
        num = pd.to_numeric(raw, errors="coerce")
        if num.notna().mean() >= _MIN_PARSE_FRACTION:
            med = float(num.dropna().median())
            unit = "ms" if med > 1e11 else "s"
            cand = pd.to_datetime(num, errors="coerce", unit=unit)
            if _frac(cand) > best_frac:
                best, best_frac, best_method = cand, _frac(cand), f"epoch:{unit}"

    return best, best_method


def assess_timestamp_integrity(
    raw: pd.Series,
    processing_now: pd.Timestamp | None = None,
) -> TimestampIntegrity:
    """Parse ``raw`` and judge whether the result can be trusted for time metrics."""
    raw = pd.Series(raw)
    n_total = int(len(raw))
    now = pd.Timestamp(processing_now) if processing_now is not None else pd.Timestamp.now()

    # Hard fail: values are bare time tokens — the date is gone and unrecoverable.
    if _looks_time_only(raw):
        return TimestampIntegrity(
            status="unusable", parsed=None, n_total=n_total, n_parsed=0,
            parsed_pct=0.0, method="time-only",
            message=("Timestamps contain only minutes/seconds (e.g. '24:44.0') with no "
                     "date — the calendar date was lost when the file was exported. "
                     "Date- and time-based metrics (Lden, Lnight, diurnal profile, daily "
                     "summary, heatmap) cannot be computed. Re-export the original file "
                     "keeping the full 'YYYY-MM-DD HH:MM:SS' timestamp column."),
            time_metrics_valid=False,
        )

    parsed, method = parse_timestamps_robust(raw)
    n_parsed = int(parsed.notna().sum())
    parsed_pct = 100.0 * n_parsed / max(1, n_total)
    valid = parsed.dropna()

    unique_dates = int(valid.dt.normalize().nunique()) if not valid.empty else 0
    span_days = float((valid.max() - valid.min()).total_seconds() / 86400.0) if n_parsed > 1 else 0.0

    # Fabricated-date signal: parsing invented "today" because the raw strings
    # carry no year (all on the processing date, no 4-digit year in the source).
    has_year = bool(raw.dropna().astype(str).head(200).str.contains(r"\d{4}").mean() >= 0.5)
    fabricated = (
        not valid.empty
        and unique_dates <= 1
        and valid.min().normalize() == now.normalize()
        and not has_year
    )

    if parsed_pct < _MIN_PARSE_FRACTION * 100 or fabricated:
        reason = ("only %.0f%% of timestamps could be parsed" % parsed_pct) if not fabricated \
            else "the source has no calendar date, so all rows collapsed onto the processing date"
        return TimestampIntegrity(
            status="unusable", parsed=parsed, n_total=n_total, n_parsed=n_parsed,
            parsed_pct=parsed_pct, unique_dates=unique_dates, span_days=span_days,
            method=method,
            message=(f"Timestamps are unreliable — {reason}. Date- and time-based metrics "
                     "(Lden, Lnight, diurnal profile, daily summary, heatmap) are suppressed "
                     "to avoid presenting fabricated values. Overall LAeq and statistical "
                     "percentiles remain valid. Re-export the file with a full date+time column."),
            time_metrics_valid=False,
        )

    # A fully-parsed series is trustworthy regardless of which strategy won.
    # Only flag "recovered" when a meaningful share of rows needed dropping.
    status = "ok" if parsed_pct >= 99.5 else "recovered"
    msg = ""
    if status == "recovered":
        msg = (f"Timestamps were recovered using the '{method}' strategy, but "
               f"{100.0 - parsed_pct:.0f}% of rows could not be parsed and were dropped "
               f"from time-based metrics. Verify the date range looks correct.")
    return TimestampIntegrity(
        status=status, parsed=parsed, n_total=n_total, n_parsed=n_parsed,
        parsed_pct=parsed_pct, unique_dates=unique_dates, span_days=span_days,
        method=method, message=msg, time_metrics_valid=True,
    )


def primary_time_column(df: pd.DataFrame) -> str | None:
    """Return the most likely timestamp column name, or None."""
    for c in df.columns:
        cl = str(c).lower()
        if any(t in cl for t in ("timestamp", "datetime", "date", "time")):
            return c
    return None
