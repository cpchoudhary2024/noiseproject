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


def energy_concentration(levels_db: Iterable[float] | pd.Series | np.ndarray) -> dict | None:
    """Report how much of the total acoustic energy sits in the loudest samples.

    LAeq is an energy average, so a very small number of very loud samples can
    dominate it completely. In one test record a SINGLE one-second sample of
    120 dB(A), out of 86,400, carried 98% of the total energy and moved LAeq from
    53.5 to 70.7 dB — flipping the reported verdict from "low concern" to
    "significantly exceeds WHO guidelines". The level was real arithmetic, but a
    conclusion resting on one unverified sample is not defensible, and nothing in
    the report disclosed that it did.

    This is also the signature of a sensor artefact: a handling knock, a dropped
    microphone, or clipping produces exactly one enormous reading. Whether the
    event is genuine or spurious cannot be settled from level data alone, so the
    platform's obligation is to surface the dependency rather than resolve it.

    A useful diagnostic is ``LAeq > L5``: because L5 is exceeded 5% of the time,
    an energy mean above it means the average is being carried by the top few
    percent of samples.

    Parameters
    ----------
    levels_db : array-like
        Sound levels in dB(A).

    Returns
    -------
    dict | None
        ``laeq``, ``l5``, ``laeq_minus_l5``, ``top1_energy_pct`` (energy share of
        the single loudest sample), ``top01pct_energy_pct`` (share of the loudest
        0.1% of samples), ``laeq_excluding_top01pct``, ``n`` and ``dominated``
        (True when the average is being driven by a handful of samples).
        None when there are too few finite values.
    """
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
        # Either a single sample carrying a large share, or an energy mean that
        # has risen above the level exceeded 5% of the time.
        "dominated": bool(top1_pct >= 10.0 or (laeq - l5) > 1.0),
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

    # Ldn and Lden are DURATION-weighted by definition (ISO 1996-1; EU 2002/49/EC
    # Annex I): each period contributes in proportion to its length in hours, not
    # in proportion to how many samples happened to land in it.
    #
    # Penalising per-sample and energy-averaging the pooled series instead makes
    # the result depend on sample density. A record that starts mid-morning and
    # ends mid-evening over-represents daytime, biasing Ldn/Lden low — measured at
    # up to 0.24 dB on partial-day datasets here. Weighting by window duration
    # removes that dependency and matches the published definition exactly.
    def _weighted_db(components: list[tuple[float, float | None]]) -> float | None:
        """Combine (hours, level_dB) pairs into a duration-weighted level.

        Returns None unless EVERY constituent period has data.

        Renormalising over only the periods present was unsafe. A night-only
        record has no day and no evening samples at all, yet renormalisation
        still produced an Lden — structurally just Lnight plus its penalty —
        which the report then declared "within the WHO guideline of 53 dB". That
        is a false pass on a metric that could not be computed: the daytime
        contribution is unknown, not zero, and it can only raise the result.

        Ldn and Lden are defined across the whole 24 hours. If any period is
        missing the metric is not determined, and the honest output is no value
        rather than a value that reads as a verdict.
        """
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
    ldn = _weighted_db([
        (15.0, laeq_day_ldn),
        (9.0, (laeq_night_ldn + 10.0) if laeq_night_ldn is not None else None),
    ])

    # Lden: 12 h day (07-19) + 4 h evening (19-23, +5 dB) + 8 h night (23-07, +10 dB).
    lden = _weighted_db([
        (12.0, laeq_day_lden),
        (4.0, (laeq_evening_lden + 5.0) if laeq_evening_lden is not None else None),
        (8.0, (laeq_night_lden + 10.0) if laeq_night_lden is not None else None),
    ])

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

    # ``LAeq_day`` / ``LAeq_night`` are the generic day/night averages consumed by
    # the narrative text and by the Maryland COMAR compliance rows, both of which
    # DOCUMENT them as 07:00–22:00 and 22:00–07:00. They must therefore carry the
    # **Ldn** windows.
    #
    # These previously aliased the Lden windows (07:00–19:00 / 23:00–07:00), so the
    # report printed "Daytime levels (07:00-22:00) averaged X" where X was actually
    # the 07:00–19:00 figure, and compared a 23:00–07:00 average against COMAR's
    # 22:00–07:00 legal limit. Use ``LAeq_*_lden`` explicitly for Lden components.
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
