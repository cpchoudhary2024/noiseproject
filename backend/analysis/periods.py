# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""
Per-period summaries: the single definition of every day, night and 24-hour value.

Each period is bounded on the report clock and takes no readings from its
neighbours:

* Calendar day     00:00 on D to 00:00 on D+1, labelled D.
* Daytime (COMAR)  07:00 to 22:00 on D (COMAR 26.02.03.01B(5)), labelled D.
* Night (COMAR)    22:00 on D to 07:00 on D+1 (COMAR 26.02.03.01B(15)), labelled
                   with the evening it starts, D.
* Night (WHO)      23:00 on D to 07:00 on D+1 (Lnight, EU Directive 2002/49/EC
                   Annex I), labelled D.
* Lden day         07:00 on D to 07:00 on D+1: day 07-19, evening 19-23 and the
                   night 23-07 that follows (the default period start in Directive
                   2002/49/EC Annex I), labelled D.

Coverage is the measured time (readings × logging interval) over the period's
real length, taken from the time-zone rules, so a night spanning a
daylight-saving change is 7 or 9 hours long, not 8.
"""
from __future__ import annotations

from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from analysis.acoustics import compute_ldn_lden, energetic_mean_db

# Periods that start on one date and end on the next are keyed by their start date.
COMAR_DAY = (7, 22)
COMAR_NIGHT = (22, 7)
WHO_NIGHT = (23, 7)
LDEN_DAY_START = 7

# A period is reported as complete at or above this coverage; one missing
# logging interval at each edge must not mark a whole day partial.
COMPLETE_COVERAGE_PCT = 99.5


def period_key(ts: pd.Series, start_hour: int) -> pd.Series:
    """Date on which the period containing each reading began.

    For a period starting at ``start_hour`` and running up to 24 h, a reading
    before ``start_hour`` belongs to the period that began the previous day.
    """
    day = ts.dt.normalize()
    return day.where(ts.dt.hour >= start_hour, day - pd.Timedelta(days=1))


def _in_window(hours: pd.Series, start: int, end: int) -> pd.Series:
    return (hours >= start) & (hours < end) if start < end else (hours >= start) | (hours < end)


def period_length_seconds(day: pd.Timestamp, start_hour: int, end_hour: int, tz: str | None) -> float:
    """Real length of the period starting ``start_hour`` on ``day``.

    With a zone, the local bounds are converted through UTC, so the length
    reflects daylight-saving changes inside the period.
    """
    start = day + pd.Timedelta(hours=start_hour)
    end = day + pd.Timedelta(hours=end_hour) + (pd.Timedelta(days=1) if end_hour <= start_hour else pd.Timedelta(0))
    if not tz:
        return (end - start).total_seconds()
    zone = ZoneInfo(tz)
    s = start.to_pydatetime().replace(tzinfo=zone)
    e = end.to_pydatetime().replace(tzinfo=zone)
    return (e.astimezone(ZoneInfo('UTC')) - s.astimezone(ZoneInfo('UTC'))).total_seconds()


def _coverage_pct(n: int, interval_s: float, length_s: float) -> float:
    return float(min(100.0, 100.0 * n * interval_s / length_s)) if length_s > 0 else float('nan')


def _period_levels(ts: pd.Series, leq: pd.Series, start: int, end: int,
                   tz: str | None, interval_s: float) -> pd.DataFrame:
    """Energy average and coverage per period of the window [start, end)."""
    m = _in_window(ts.dt.hour, start, end)
    if not m.any():
        return pd.DataFrame(columns=['laeq', 'coverage_pct'])
    key = period_key(ts[m], start) if end <= start else ts[m].dt.normalize()
    g = leq[m].groupby(key)
    out = pd.DataFrame({'laeq': g.apply(energetic_mean_db), 'n': g.size()})
    out['coverage_pct'] = [
        _coverage_pct(int(n), interval_s, period_length_seconds(d, start, end, tz))
        for d, n in out['n'].items()
    ]
    return out[['laeq', 'coverage_pct']]


def lden_by_period(ts: pd.Series, leq: pd.Series, tz: str | None, interval_s: float) -> pd.DataFrame:
    """Lden for each 24-hour period starting 07:00, with its coverage."""
    key = period_key(ts, LDEN_DAY_START)
    rows = {}
    for d, idx in key.groupby(key).groups.items():
        res = compute_ldn_lden(ts.loc[idx], leq.loc[idx])
        rows[d] = {
            'lden': res.get('Lden') if res else None,
            'coverage_pct': _coverage_pct(len(idx), interval_s,
                                          period_length_seconds(d, LDEN_DAY_START, LDEN_DAY_START, tz)),
        }
    return pd.DataFrame.from_dict(rows, orient='index')


def nightly_values(ts: pd.Series, leq: pd.Series, window: tuple[int, int], tz: str | None,
                   interval_s: float) -> pd.DataFrame:
    """Energy average and coverage for each night, keyed by the date it began."""
    return _period_levels(ts, leq, window[0], window[1], tz, interval_s)


def daily_summary(ts: pd.Series, leq: pd.Series, tz: str | None, interval_s: float) -> pd.DataFrame:
    """One row per calendar date, every column bounded as described in the module docstring."""
    data = pd.DataFrame({'ts': pd.to_datetime(ts, errors='coerce'),
                         'leq': pd.to_numeric(leq, errors='coerce')}).dropna()
    if data.empty:
        return pd.DataFrame()
    ts, leq = data['ts'], data['leq']
    day = ts.dt.normalize()
    g = leq.groupby(day)
    out = pd.DataFrame({
        'Average_L_EQ_dB': g.apply(energetic_mean_db),
        'Min_L_EQ_dB': g.min(),
        'Max_L_EQ_dB': g.max(),
        'Std_Dev': g.std(),
        'n': g.size(),
    })
    out['Hours_Measured'] = out['n'] * interval_s / 3600.0
    out['Day_Coverage_pct'] = [_coverage_pct(int(n), interval_s, period_length_seconds(d, 0, 0, tz))
                               for d, n in out['n'].items()]

    daytime = _period_levels(ts, leq, *COMAR_DAY, tz, interval_s)
    night = nightly_values(ts, leq, COMAR_NIGHT, tz, interval_s)
    lden = lden_by_period(ts, leq, tz, interval_s)
    out['Daytime_LAeq'] = daytime['laeq'].reindex(out.index)
    out['Daytime_Coverage_pct'] = daytime['coverage_pct'].reindex(out.index)
    out['Nighttime_LAeq'] = night['laeq'].reindex(out.index)
    out['Night_Coverage_pct'] = night['coverage_pct'].reindex(out.index)
    out['Daily_Lden'] = lden['lden'].reindex(out.index) if not lden.empty else np.nan
    out['Lden_Coverage_pct'] = lden['coverage_pct'].reindex(out.index) if not lden.empty else np.nan
    out = out.drop(columns=['n'])
    out.index.name = 'Date'
    return out.reset_index()


def hourly_summary(ts: pd.Series, leq: pd.Series) -> pd.DataFrame:
    """One row per clock hour 0–23, pooled across all dates."""
    data = pd.DataFrame({'ts': pd.to_datetime(ts, errors='coerce'),
                         'leq': pd.to_numeric(leq, errors='coerce')}).dropna()
    if data.empty:
        return pd.DataFrame()
    g = data['leq'].groupby(data['ts'].dt.hour)
    out = pd.DataFrame({
        'Average_L_EQ_dB': g.apply(energetic_mean_db),
        'Min_L_EQ_dB': g.min(),
        'Max_L_EQ_dB': g.max(),
        'Std_Dev': g.std(),
    })
    out.index.name = 'Hour'
    return out.reset_index()
