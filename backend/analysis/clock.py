# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""Logger clock handling: time-zone conversion and daylight-saving edge cases."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from zoneinfo import ZoneInfo, available_timezones

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)

FOLD_COLUMN = '__clock_fold__'
DEFAULT_CLOCK = 'America/New_York'

# Curated choices shown in the interface; any other IANA zone is also accepted.
CLOCK_CHOICES: dict[str, str] = {
    'America/New_York': 'US Eastern, with daylight saving (EST/EDT)',
    'Etc/GMT+5': 'US Eastern standard time all year (UTC−5)',
    'America/Chicago': 'US Central, with daylight saving (CST/CDT)',
    'America/Denver': 'US Mountain, with daylight saving (MST/MDT)',
    'America/Phoenix': 'US Mountain, no daylight saving (Arizona, UTC−7)',
    'America/Los_Angeles': 'US Pacific, with daylight saving (PST/PDT)',
    'UTC': 'Coordinated Universal Time (UTC)',
    'Europe/London': 'UK (GMT/BST)',
    'Europe/Berlin': 'Central Europe (CET/CEST)',
    'Asia/Kolkata': 'India (IST, UTC+5:30)',
}

_FALLBACK_TOLERANCE_S = 5.0


class ClockError(ValueError):
    """The requested clock setting is invalid or cannot be applied to this record."""


@dataclass
class ClockSetting:
    source: str = DEFAULT_CLOCK
    target: str = DEFAULT_CLOCK

    @property
    def is_identity(self):
        return self.source == self.target

    @classmethod
    def from_request(cls, raw):
        raw = raw or {}
        setting = cls(source=str(raw.get('source') or DEFAULT_CLOCK).strip(),
                      target=str(raw.get('target') or DEFAULT_CLOCK).strip())
        for name in (setting.source, setting.target):
            if not is_valid_zone(name):
                raise ClockError(f"'{name}' is not a recognised time zone.")
        return setting

    def to_dict(self):
        return {'source': self.source, 'target': self.target,
                'source_label': zone_label(self.source), 'target_label': zone_label(self.target)}


def is_valid_zone(name: str) -> bool:
    return name in CLOCK_CHOICES or name in available_timezones()


def zone_label(name: str) -> str:
    return CLOCK_CHOICES.get(name, name)


def zone_abbreviations(name: str, start: pd.Timestamp, end: pd.Timestamp) -> str:
    """Abbreviations in force over [start, end], e.g."""
    tz = ZoneInfo(name)
    probes = pd.date_range(start.normalize(), end.normalize() + pd.Timedelta(days=1), freq='D')
    abbrs = []
    for p in probes:
        a = p.to_pydatetime().replace(hour=12, tzinfo=tz).tzname()
        if a and a not in abbrs:
            abbrs.append(a)
    return '/'.join(abbrs)


# Fall-back (repeated hour) handling

def mark_repeated_hour(df: pd.DataFrame, time_col: str) -> pd.DataFrame:
    """Mark the second pass of a repeated hour, in recorded order."""
    ts = pd.to_datetime(df[time_col], errors='coerce')
    d = ts.diff().dt.total_seconds()
    positive = d[d > 0]
    interval = float(positive.mode().iloc[0]) if not positive.empty else 1.0
    # The wall clock steps back one hour less one logging interval.
    tolerance = max(_FALLBACK_TOLERANCE_S, 2.0 * interval)
    jumps = d[(d + 3600.0).abs() <= tolerance]
    if jumps.empty:
        return df
    fold = np.zeros(len(df), dtype='int8')
    pos = {idx: i for i, idx in enumerate(df.index)}
    ts_values = ts.to_numpy()
    for idx in jumps.index:
        i = pos[idx]
        peak = ts_values[i - 1]                      # last reading of the first pass
        j = i
        while j < len(df) and not pd.isna(ts_values[j]) and ts_values[j] <= peak:
            fold[j] = 1
            j += 1
        logger.info("[CLOCK] Repeated hour detected: %d readings after %s marked as second pass",
                    j - i, pd.Timestamp(peak))
    out = df.copy()
    out[FOLD_COLUMN] = fold
    return out


def ordering_key(ts: pd.Series, fold: pd.Series | None) -> pd.Series:
    """Monotonic sort key for wall-clock timestamps that may contain a repeated hour."""
    if fold is None or not fold.any():
        return ts
    key = ts.copy()
    f = fold.astype(bool).to_numpy()
    one_hour = pd.Timedelta(hours=1)
    # the second pass and everything after it moves an hour later
    for day in sorted(ts[f].dt.normalize().unique()):
        on_day = (ts.dt.normalize() == day).to_numpy()
        last_fold = ts[f & on_day].max()
        shift = (f & on_day) | (ts > last_fold).to_numpy()
        key[shift] = key[shift] + one_hour
    return key


# Conversion

def convert_times(ts: pd.Series, fold: pd.Series | None,
                  setting: ClockSetting) -> tuple[pd.Series, pd.Series | None, dict]:
    """Convert naive wall-clock ``ts`` from ``setting.source`` to ``setting.target``."""
    info = {**setting.to_dict(), 'converted': not setting.is_identity,
            'nonexistent_source_readings': 0}
    if setting.is_identity:
        return ts, fold, info

    ts = pd.to_datetime(ts, errors='coerce')
    valid = ts.notna()
    if fold is None:
        fold = pd.Series(0, index=ts.index, dtype='int8')
    # ambiguous=True means "the first (daylight) occurrence" in pandas.
    first_pass = (fold == 0).to_numpy()
    try:
        aware = ts.dt.tz_localize(setting.source, ambiguous=first_pass, nonexistent='NaT')
    except Exception as exc:
        raise ClockError(f"Timestamps could not be read as {zone_label(setting.source)}: {exc}") from exc
    nonexistent = int((aware.isna() & valid).sum())
    info['nonexistent_source_readings'] = nonexistent
    if nonexistent:
        logger.warning("[CLOCK] %d readings fall in a time that does not exist in %s",
                       nonexistent, setting.source)

    target = aware.dt.tz_convert(setting.target)
    offsets = target.map(lambda t: t.utcoffset() if not pd.isna(t) else pd.NaT)
    naive = target.dt.tz_localize(None)
    same_wall_earlier = naive.duplicated(keep='first') & naive.notna()
    target_fold = None
    if same_wall_earlier.any():
        smaller_offset = offsets < offsets.shift(1).ffill()
        target_fold = (same_wall_earlier | smaller_offset).astype('int8').where(naive.notna(), 0)
    info['offsets'] = sorted({str(o) for o in offsets.dropna().unique()})
    return naive, target_fold, info


def apply_clock(df: pd.DataFrame, time_col: str, setting: ClockSetting) -> pd.DataFrame:
    """Return ``df`` with ``time_col`` in the target clock and ``attrs['clock']`` set."""
    fold = df[FOLD_COLUMN] if FOLD_COLUMN in df.columns else None
    ts = pd.to_datetime(df[time_col], errors='coerce')
    converted, target_fold, info = convert_times(ts, fold, setting)
    out = df.copy()
    out[time_col] = converted
    if target_fold is not None:
        out[FOLD_COLUMN] = target_fold.to_numpy()
    elif FOLD_COLUMN in out.columns and not setting.is_identity:
        out = out.drop(columns=[FOLD_COLUMN])
    dropped = int(out[time_col].isna().sum() - ts.isna().sum())
    if dropped:
        out = out[out[time_col].notna()]
    out.attrs = {**df.attrs, 'clock': info}
    return out


def describe_time_basis(zone: str, ts: pd.Series | None = None) -> str:
    """'US Eastern, with daylight saving (EST/EDT); EDT in this record'."""
    label = zone_label(zone)
    if ts is not None:
        ts = pd.to_datetime(ts, errors='coerce').dropna()
        if not ts.empty:
            abbr = zone_abbreviations(zone, ts.min(), ts.max())
            if abbr and abbr not in label:
                label = f"{label}; {abbr} in this record"
    return label
