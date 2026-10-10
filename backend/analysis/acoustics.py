# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""Acoustics utilities for environmental noise metrics."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import pandas as pd


def _to_float_array(values):
    arr = np.asarray(list(values) if not isinstance(values, (pd.Series, np.ndarray)) else values, dtype=float)
    return arr[np.isfinite(arr)]


def energetic_mean_db(levels_db: Iterable[float] | pd.Series | np.ndarray) -> float | None:
    """Compute the energy-average level (LAeq-style) from dB samples."""
    arr = _to_float_array(levels_db)
    if arr.size == 0:
        return None
    mean_linear = float(np.mean(np.power(10.0, arr / 10.0)))
    if mean_linear <= 0:
        return None
    return 10.0 * math.log10(mean_linear)


def exceedance_levels_db(levels_db: Iterable[float] | pd.Series | np.ndarray) -> dict[str, float] | None:
    """Compute exceedance levels L5/L10/L50/L90/L95 from dB samples."""
    arr = _to_float_array(levels_db)
    if arr.size == 0:
        return None

    def p(q):
        """Percentile of the finite sample array."""
        return float(np.percentile(arr, q))

    return {
        "L5": p(95),
        "L10": p(90),
        "L50": p(50),
        "L90": p(10),
        "L95": p(5),
    }


def energy_concentration(levels_db: Iterable[float] | pd.Series | np.ndarray) -> dict | None:
    """Report how much of the total acoustic energy sits in the loudest samples."""
    arr = _to_float_array(levels_db)
    if arr.size < 20:
        return None

    energy = np.power(10.0, arr / 10.0)
    total = float(energy.sum())
    if total <= 0:
        return None

    order = np.sort(energy)[::-1]
    n_top = max(1, int(round(arr.size * 0.001)))     # loudest 0.1%
    top1_pct = 100.0 * float(order[0]) / total
    top01_pct = 100.0 * float(order[:n_top].sum()) / total

    cut = np.sort(arr)[::-1][n_top - 1] if n_top <= arr.size else arr.max()
    remainder = arr[arr < cut]
    laeq_excl = energetic_mean_db(remainder) if remainder.size else None

    laeq = float(10.0 * math.log10(total / arr.size))
    l5 = float(np.percentile(arr, 95))

    return {
        "n": int(arr.size),
        "laeq": laeq,
        "l5": l5,
        "laeq_minus_l5": laeq - l5,
        "top1_energy_pct": top1_pct,
        "top01pct_energy_pct": top01_pct,
        "n_top01pct": int(n_top),
        "laeq_excluding_top01pct": float(laeq_excl) if laeq_excl is not None else None,
        "dominated": bool(top1_pct >= 10.0 or (laeq - l5) > 1.0),
    }


@dataclass(frozen=True)
class DayEveningNightDefinition:
    """Definition for day/evening/night hour ranges."""

    day_start: int
    day_end: int
    evening_start: Optional[int] = None
    evening_end: Optional[int] = None
    night_start: int = 22
    night_end: int = 7


LDN_DEFAULT = DayEveningNightDefinition(day_start=7, day_end=22, evening_start=None, evening_end=None, night_start=22, night_end=7)
LDEN_DEFAULT = DayEveningNightDefinition(day_start=7, day_end=19, evening_start=19, evening_end=23, night_start=23, night_end=7)


def _mask_in_range(hours, start, end):
    if start == end:
        return pd.Series(True, index=hours.index)
    if start < end:
        return (hours >= start) & (hours < end)
    # wraps midnight
    return (hours >= start) | (hours < end)


def compute_ldn_lden(
    timestamps: pd.Series,
    levels_db: pd.Series,
    *,
    ldn_def: DayEveningNightDefinition = LDN_DEFAULT,
    lden_def: DayEveningNightDefinition = LDEN_DEFAULT,
) -> dict[str, float] | None:
    """Compute LAeq-day, LAeq-night, Ldn, and Lden from timestamped dB samples."""
    if timestamps is None or levels_db is None:
        return None

    ts = pd.to_datetime(timestamps, errors="coerce")
    y = pd.to_numeric(levels_db, errors="coerce")
    m = ts.notna() & y.notna()
    if int(m.sum()) == 0:
        return None

    ts = ts[m]
    y = y[m].astype(float)
    hours = ts.dt.hour.astype(int)

    # Ldn masks (typically: day 07-22, night 22-07)
    day_mask_ldn = _mask_in_range(hours, ldn_def.day_start, ldn_def.day_end)
    night_mask_ldn = _mask_in_range(hours, int(ldn_def.night_start), int(ldn_def.night_end))

    laeq_24h = energetic_mean_db(y)

    # Lden/Lnight masks (EU definition typically: day 07-19, evening 19-23, night 23-07)
    day_mask_lden = _mask_in_range(hours, int(lden_def.day_start), int(lden_def.day_end))
    if lden_def.evening_start is not None and lden_def.evening_end is not None:
        evening_mask = _mask_in_range(hours, int(lden_def.evening_start), int(lden_def.evening_end))
    else:
        evening_mask = pd.Series(False, index=y.index)
    night_mask_lden = _mask_in_range(hours, int(lden_def.night_start), int(lden_def.night_end))

    laeq_day_lden = energetic_mean_db(y[day_mask_lden])
    laeq_evening_lden = energetic_mean_db(y[evening_mask])
    laeq_night_lden = energetic_mean_db(y[night_mask_lden])

    laeq_day_ldn = energetic_mean_db(y[day_mask_ldn])
    laeq_night_ldn = energetic_mean_db(y[night_mask_ldn])

    # weight each period by its hours, not by how many samples it holds
    def weighted_db(components):
        if any(lvl is None for _, lvl in components):
            return None
        total_hours = sum(h for h, _ in components)
        if total_hours <= 0:
            return None
        energy = sum(h * (10.0 ** (lvl / 10.0)) for h, lvl in components)
        if energy <= 0:
            return None
        return 10.0 * math.log10(energy / total_hours)

    # Ldn: 15 h day (07-22) + 9 h night (22-07) with a +10 dB night penalty.
    ldn = weighted_db([
        (15.0, laeq_day_ldn),
        (9.0, (laeq_night_ldn + 10.0) if laeq_night_ldn is not None else None),
    ])

    # Lden: 12 h day (07-19) + 4 h evening (19-23, +5 dB) + 8 h night (23-07, +10 dB).
    lden = weighted_db([
        (12.0, laeq_day_lden),
        (4.0, (laeq_evening_lden + 5.0) if laeq_evening_lden is not None else None),
        (8.0, (laeq_night_lden + 10.0) if laeq_night_lden is not None else None),
    ])

    out = {}
    if laeq_24h is not None:
        out["LAeq_24h"] = float(laeq_24h)

    # Explicit windowed LAeq values
    if laeq_day_ldn is not None:
        out["LAeq_day_ldn"] = float(laeq_day_ldn)
    if laeq_night_ldn is not None:
        out["LAeq_night_ldn"] = float(laeq_night_ldn)

    if laeq_day_lden is not None:
        out["LAeq_day_lden"] = float(laeq_day_lden)
    if laeq_evening_lden is not None:
        out["LAeq_evening_lden"] = float(laeq_evening_lden)
    if laeq_night_lden is not None:
        out["LAeq_night_lden"] = float(laeq_night_lden)
        out["Lnight"] = float(laeq_night_lden)

    if laeq_day_ldn is not None:
        out["LAeq_day"] = float(laeq_day_ldn)
    if laeq_evening_lden is not None:
        out["LAeq_evening"] = float(laeq_evening_lden)
    if laeq_night_ldn is not None:
        out["LAeq_night"] = float(laeq_night_ldn)
    if ldn is not None:
        out["Ldn"] = float(ldn)
    if lden is not None:
        out["Lden"] = float(lden)

    return out if out else None


def time_above_level_pct(levels_db: pd.Series, threshold_db: float) -> float | None:
    """Share of measured readings at or above ``threshold_db``."""
    clean = pd.to_numeric(levels_db, errors='coerce').dropna()
    if clean.empty:
        return None
    return float((clean >= float(threshold_db)).sum()) / len(clean) * 100.0


def time_above_level_in_window(
    timestamps: pd.Series,
    levels_db: pd.Series,
    *,
    threshold_db: float,
    start_hour: int,
    end_hour: int,
) -> tuple[float | None, int]:
    """Share of time at or above ``threshold_db``, within one hour window."""
    df = pd.DataFrame({'ts': pd.to_datetime(timestamps, errors='coerce'),
                       'leq': pd.to_numeric(levels_db, errors='coerce')}).dropna()
    if df.empty:
        return None, 0
    in_window = df.loc[_mask_in_range(df['ts'].dt.hour, int(start_hour), int(end_hour)), 'leq']
    return time_above_level_pct(in_window, threshold_db), int(len(in_window))


def time_above_level_by_period(
    timestamps: pd.Series,
    levels_db: pd.Series,
    *,
    day_threshold_db: float,
    night_threshold_db: float,
    day_def: DayEveningNightDefinition = LDN_DEFAULT,
) -> dict[str, float | int | None]:
    """Share of day-period and of night-period time at or above each threshold."""
    day_pct, n_day = time_above_level_in_window(
        timestamps, levels_db, threshold_db=day_threshold_db,
        start_hour=day_def.day_start, end_hour=day_def.day_end)
    night_pct, n_night = time_above_level_in_window(
        timestamps, levels_db, threshold_db=night_threshold_db,
        start_hour=day_def.night_start, end_hour=day_def.night_end)
    return {'day_pct': day_pct, 'night_pct': night_pct,
            'n_day': n_day, 'n_night': n_night}
