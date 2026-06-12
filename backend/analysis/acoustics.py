"""Acoustics utilities for environmental noise metrics.

This module centralizes correct decibel-domain computations.
Many aggregates (e.g., LAeq) require energy averaging rather than arithmetic
averaging in dB.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import pandas as pd


def _to_float_array(values: Iterable[float] | pd.Series | np.ndarray) -> np.ndarray:
    arr = np.asarray(list(values) if not isinstance(values, (pd.Series, np.ndarray)) else values, dtype=float)
    return arr[np.isfinite(arr)]


def energetic_mean_db(levels_db: Iterable[float] | pd.Series | np.ndarray) -> float | None:
    """Compute the energy-average level (LAeq-style) from dB samples.

    For samples $L_i$ in dB, the energy mean is:
    `10 * log10(mean(10 ** (L_i / 10)))`.

    Returns None if there are no finite values.
    """
    arr = _to_float_array(levels_db)
    if arr.size == 0:
        return None
    mean_linear = float(np.mean(np.power(10.0, arr / 10.0)))
    if mean_linear <= 0:
        return None
    return 10.0 * math.log10(mean_linear)


def exceedance_levels_db(levels_db: Iterable[float] | pd.Series | np.ndarray) -> dict[str, float] | None:
    """Compute exceedance levels L5/L10/L50/L90/L95 from dB samples.

    Lx is the level exceeded x% of the time.
    With ascending percentiles, that corresponds to the (100-x)th percentile.
    """
    arr = _to_float_array(levels_db)
    if arr.size == 0:
        return None

    def p(q: float) -> float:
        return float(np.percentile(arr, q))

    return {
        "L5": p(95),
        "L10": p(90),
        "L50": p(50),
        "L90": p(10),
        "L95": p(5),
    }


@dataclass(frozen=True)
class DayEveningNightDefinition:
    """Definition for day/evening/night hour ranges.

    Hours are integers 0-23.
    Ranges are half-open: start inclusive, end exclusive.
    """

    day_start: int
    day_end: int
    evening_start: Optional[int] = None
    evening_end: Optional[int] = None
    night_start: int = 22
    night_end: int = 7


LDN_DEFAULT = DayEveningNightDefinition(day_start=7, day_end=22, evening_start=None, evening_end=None, night_start=22, night_end=7)
LDEN_DEFAULT = DayEveningNightDefinition(day_start=7, day_end=19, evening_start=19, evening_end=23, night_start=23, night_end=7)


def _is_in_range(hour: int, start: int, end: int) -> bool:
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    # wraps midnight
    return hour >= start or hour < end


def _mask_in_range(hours: pd.Series, start: int, end: int) -> pd.Series:
    """Vectorized half-open hour-range membership (start inclusive, end exclusive).

    Equivalent to ``hours.map(lambda h: _is_in_range(h, start, end))`` but runs as
    a single numpy boolean op — critical on multi-million-row series.
    """
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
    """Compute LAeq-day, LAeq-night, Ldn, and Lden from timestamped dB samples.

    Notes:
    - This implementation assumes samples are equally-weighted in time.
      If your dataset has variable integration times, you should weight by
      duration (not currently supported here).
    """
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

    # Ldn: +10 dB penalty during night hours
    y_ldn = y.copy()
    y_ldn.loc[night_mask_ldn] = y_ldn.loc[night_mask_ldn] + 10.0
    ldn = energetic_mean_db(y_ldn)

    # Lden: +5 dB evening, +10 dB night
    y_lden = y.copy()
    y_lden.loc[evening_mask] = y_lden.loc[evening_mask] + 5.0
    y_lden.loc[night_mask_lden] = y_lden.loc[night_mask_lden] + 10.0
    lden = energetic_mean_db(y_lden)

    out: dict[str, float] = {}
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

    # Backward-compatible aliases used by older UI/report code.
    # These refer to the Lden/Lnight (23:00–07:00) night window.
    if laeq_day_lden is not None:
        out["LAeq_day"] = float(laeq_day_lden)
    if laeq_evening_lden is not None:
        out["LAeq_evening"] = float(laeq_evening_lden)
    if laeq_night_lden is not None:
        out["LAeq_night"] = float(laeq_night_lden)
    if ldn is not None:
        out["Ldn"] = float(ldn)
    if lden is not None:
        out["Lden"] = float(lden)

    return out if out else None
