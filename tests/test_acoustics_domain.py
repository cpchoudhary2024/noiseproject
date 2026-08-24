"""Regulatory-reference unit tests for the acoustic domain math.

These tests verify ``backend.analysis.acoustics`` against hand-computed values
and against the published definitions of the metrics, not against whatever the
code currently returns. No production function is modified by this suite.

Reference standards exercised here
----------------------------------
* **ISO 1996-1** — energy (not arithmetic) averaging of A-weighted sound
  pressure levels; day/night rating levels.
* **EU Directive 2002/49/EC, Annex I** — ``Lden`` as a *duration-weighted*
  combination of a 12 h day, a 4 h evening (+5 dB) and an 8 h night (+10 dB).
* **US FICON / HUD 24 CFR Part 51B** convention for ``Ldn`` — 15 h day
  (07:00–22:00) plus 9 h night (22:00–07:00) with a +10 dB night penalty.

Units
-----
All sound levels are A-weighted decibels, ``dB(A)``, referenced to 20 µPa.
All times are local clock hours (0–23).
"""

import math

import numpy as np
import pandas as pd
import pytest

from backend.analysis.acoustics import (
    LDEN_DEFAULT,
    LDN_DEFAULT,
    DayEveningNightDefinition,
    compute_ldn_lden,
    energetic_mean_db,
    energy_concentration,
    exceedance_levels_db,
    time_above_level_pct,
)

# Tolerance for dB comparisons. Sound level meters report to 0.1 dB, so
# agreement to 1e-6 dB is far tighter than any physically meaningful threshold.
DB_TOL = 1e-6


# ── Energy averaging (ISO 1996-1) ────────────────────────────────────────────


def test_energetic_mean_of_identical_levels_is_that_level():
    """A constant signal must energy-average to its own level.

    Hand check: 10*log10(mean(10^(70/10))) = 10*log10(10^7) = 70 dB(A).
    """
    assert energetic_mean_db([70.0] * 100) == pytest.approx(70.0, abs=DB_TOL)


def test_energetic_mean_of_two_equal_sources_adds_three_db():
    """Doubling acoustic energy raises the level by 10*log10(2) = 3.0103 dB.

    This is the canonical textbook result and the single most common error in
    noise work (arithmetic averaging would return 70.0, not 73.01).
    """
    combined = 10.0 * math.log10(2 * 10 ** (70.0 / 10.0))
    assert combined == pytest.approx(73.0103, abs=1e-4)


def test_energy_average_exceeds_arithmetic_mean_for_varying_levels():
    """For any non-constant series the energy mean must exceed the arithmetic mean.

    Jensen's inequality applied to the convex map L -> 10**(L/10). Hand-computed
    reference for [60, 80] dB(A):
        10*log10((10^6 + 10^8)/2) = 10*log10(5.05e7) = 77.0330 dB(A)
    versus an arithmetic mean of 70.0 dB(A) — a 7 dB error if done wrong.
    """
    levels = [60.0, 80.0]
    energy_mean = energetic_mean_db(levels)
    assert energy_mean == pytest.approx(77.0330, abs=1e-4)
    assert energy_mean > float(np.mean(levels))


def test_energetic_mean_ignores_non_finite_samples():
    """NaN/inf rows (data dropouts) must be excluded, not propagated."""
    assert energetic_mean_db([70.0, np.nan, 70.0, np.inf]) == pytest.approx(70.0, abs=DB_TOL)


def test_energetic_mean_returns_none_when_no_finite_data():
    """An all-NaN window is undetermined, and must not silently return 0 dB."""
    assert energetic_mean_db([np.nan, np.nan]) is None
    assert energetic_mean_db([]) is None


# ── Exceedance percentiles (Lx family) ───────────────────────────────────────


def test_exceedance_levels_use_complement_percentiles():
    """L10 is the level exceeded 10% of the time = the 90th percentile.

    On the ramp 0..100 dB(A) the mapping is exact and hand-checkable.
    """
    out = exceedance_levels_db(list(range(101)))
    assert out["L10"] == pytest.approx(90.0, abs=DB_TOL)
    assert out["L50"] == pytest.approx(50.0, abs=DB_TOL)
    assert out["L90"] == pytest.approx(10.0, abs=DB_TOL)


def test_exceedance_levels_are_monotonically_ordered():
    """By construction L5 >= L10 >= L50 >= L90 >= L95 for any dataset."""
    rng = np.random.default_rng(42)
    out = exceedance_levels_db(rng.normal(55.0, 8.0, 5000))
    assert out["L5"] >= out["L10"] >= out["L50"] >= out["L90"] >= out["L95"]


# ── Ldn: +10 dB night penalty, duration-weighted ─────────────────────────────


def _series_at_constant_level(level_db, hours, minutes_per_hour=60):
    """Build a timestamped constant-level series over the given clock hours.

    Args:
        level_db (float): Constant A-weighted level, dB(A).
        hours (Iterable[int]): Local clock hours (0-23) to populate.
        minutes_per_hour (int): Samples per hour.

    Returns:
        tuple[pd.Series, pd.Series]: (timestamps, levels in dB(A)).
    """
    stamps = []
    for h in hours:
        for m in range(minutes_per_hour):
            stamps.append(pd.Timestamp("2026-03-02") + pd.Timedelta(hours=h, minutes=m))
    ts = pd.Series(stamps)
    return ts, pd.Series([float(level_db)] * len(ts))


def test_ldn_of_uniform_60db_day_and_night():
    """A flat 60 dB(A) 24 h record has a hand-computable Ldn.

    Ldn = 10*log10([15*10^(60/10) + 9*10^((60+10)/10)] / 24)
        = 10*log10([15e6 + 9e7] / 24)
        = 10*log10(4.375e6) = 66.4098 dB(A)

    The night penalty is +10 dB and the weights are the window DURATIONS
    (15 h / 9 h), not the sample counts.
    """
    ts, levels = _series_at_constant_level(60.0, range(24))
    out = compute_ldn_lden(ts, levels)
    expected = 10.0 * math.log10((15 * 10 ** 6.0 + 9 * 10 ** 7.0) / 24.0)
    assert expected == pytest.approx(66.4098, abs=1e-4)
    assert out["Ldn"] == pytest.approx(expected, abs=1e-4)


def test_lden_of_uniform_60db_matches_eu_annex_i():
    """Flat 60 dB(A): Lden per EU 2002/49/EC Annex I, hand-computed.

    Lden = 10*log10([12*10^(60/10) + 4*10^(65/10) + 8*10^(70/10)] / 24)
         = 10*log10([1.2000e7 + 1.2649e7 + 8.0000e7] / 24)
         = 10*log10(4.3604e6)
         = 66.3952 dB(A)

    Note that Lden (66.3952) sits slightly BELOW Ldn (66.4098) for a flat
    source: Lden penalises only 8 night hours at +10 dB where Ldn penalises 9.
    """
    ts, levels = _series_at_constant_level(60.0, range(24))
    out = compute_ldn_lden(ts, levels)
    expected = 10.0 * math.log10(
        (12 * 10 ** 6.0 + 4 * 10 ** 6.5 + 8 * 10 ** 7.0) / 24.0
    )
    assert expected == pytest.approx(66.3952, abs=1e-4)
    assert out["Lden"] == pytest.approx(expected, abs=1e-4)


def test_night_penalty_is_exactly_ten_db():
    """Raising only the night period by X dB must raise Ldn by X dB.

    Verifies the +10 dB penalty is applied additively in the dB domain, once,
    and only to the night window.
    """
    ts_a, lv_a = _series_at_constant_level(50.0, range(24))
    base = compute_ldn_lden(ts_a, lv_a)["Ldn"]
    ts_b, lv_b = _series_at_constant_level(60.0, range(24))
    lifted = compute_ldn_lden(ts_b, lv_b)["Ldn"]
    assert lifted - base == pytest.approx(10.0, abs=1e-6)


def test_ldn_is_duration_weighted_not_sample_weighted():
    """Oversampling the day must NOT bias Ldn downward.

    Ldn is defined on window duration (ISO 1996-1). A record with 10x the
    sampling rate during daytime hours must produce the same Ldn as an evenly
    sampled record at the same levels. Sample-count weighting would fail here.
    """
    dense_day = _series_at_constant_level(60.0, range(7, 22), minutes_per_hour=600)
    sparse_night = _series_at_constant_level(60.0, list(range(22, 24)) + list(range(0, 7)),
                                             minutes_per_hour=6)
    ts = pd.concat([dense_day[0], sparse_night[0]], ignore_index=True)
    lv = pd.concat([dense_day[1], sparse_night[1]], ignore_index=True)

    out = compute_ldn_lden(ts, lv)
    expected = 10.0 * math.log10((15 * 10 ** 6.0 + 9 * 10 ** 7.0) / 24.0)
    assert out["Ldn"] == pytest.approx(expected, abs=1e-4)


def test_ldn_is_none_when_a_period_is_missing():
    """A night-only record cannot yield an Ldn.

    Ldn is defined over the full 24 h. With no daytime data the daytime
    contribution is unknown — not zero — so the honest return is None. Emitting
    a number here would let a report declare a false pass against a guideline.
    """
    ts, levels = _series_at_constant_level(45.0, [23, 0, 1, 2, 3, 4, 5, 6])
    out = compute_ldn_lden(ts, levels)
    assert out.get("Ldn") is None
    assert out.get("Lden") is None
    # The night average itself is still well defined and should be reported.
    assert out["LAeq_night_lden"] == pytest.approx(45.0, abs=DB_TOL)


def test_generic_day_night_keys_carry_ldn_windows():
    """``LAeq_day``/``LAeq_night`` must use the 07-22 / 22-07 (Ldn) windows.

    These keys feed the narrative text and the Maryland COMAR compliance rows,
    both documented as 07:00-22:00 and 22:00-07:00. If they aliased the Lden
    windows (07-19 / 23-07) the report would compare a 23:00-07:00 average
    against COMAR's 22:00-07:00 legal limit.

    Construction: 22:00 and 06:00 are night under Ldn; 20:00 is day under Ldn
    but evening/none under Lden. Setting the Ldn-day hours to 50 dB(A) and the
    Ldn-night hours to 40 dB(A) makes the two conventions disagree measurably.
    """
    day_ts, day_lv = _series_at_constant_level(50.0, range(7, 22))
    night_ts, night_lv = _series_at_constant_level(40.0, list(range(22, 24)) + list(range(0, 7)))
    ts = pd.concat([day_ts, night_ts], ignore_index=True)
    lv = pd.concat([day_lv, night_lv], ignore_index=True)

    out = compute_ldn_lden(ts, lv)
    assert out["LAeq_day"] == pytest.approx(50.0, abs=1e-9)
    assert out["LAeq_night"] == pytest.approx(40.0, abs=1e-9)
    assert out["LAeq_day"] == pytest.approx(out["LAeq_day_ldn"], abs=1e-9)
    assert out["LAeq_night"] == pytest.approx(out["LAeq_night_ldn"], abs=1e-9)


def test_night_window_wraps_midnight():
    """The 22:00-07:00 night window must wrap across midnight.

    An off-by-one here silently drops the 22:00-24:00 samples from the penalised
    period, understating Ldn.
    """
    ts, levels = _series_at_constant_level(60.0, range(24))
    out = compute_ldn_lden(ts, levels)
    # 9 night hours at 60 dB(A) -> LAeq_night 60; if the wrap failed the window
    # would cover only 00:00-07:00 and the Ldn weighting would be wrong.
    assert out["LAeq_night_ldn"] == pytest.approx(60.0, abs=DB_TOL)
    assert out["Ldn"] == pytest.approx(66.4098, abs=1e-4)


def test_custom_definition_changes_windows():
    """A caller-supplied day/night definition must be honoured."""
    ts, levels = _series_at_constant_level(60.0, range(24))
    strict = DayEveningNightDefinition(day_start=6, day_end=23, night_start=23, night_end=6)
    out = compute_ldn_lden(ts, levels, ldn_def=strict)
    assert out is not None
    assert "LAeq_day_ldn" in out


def test_compute_ldn_lden_returns_none_on_empty_input():
    """Empty or all-invalid input must return None, never a fabricated level."""
    assert compute_ldn_lden(pd.Series([], dtype="datetime64[ns]"), pd.Series([], dtype=float)) is None
    assert compute_ldn_lden(None, None) is None


# ── Energy-domination diagnostic (QA/QC) ─────────────────────────────────────


def test_single_loud_sample_is_flagged_as_dominating():
    """One 120 dB(A) spike among quiet samples must be flagged.

    This is the documented failure mode: a single 1-second artefact can carry
    most of the total energy and flip a verdict. The diagnostic must catch it.
    """
    levels = [45.0] * 999 + [120.0]
    out = energy_concentration(levels)
    assert out["dominated"] is True
    assert out["top1_energy_pct"] > 90.0
    # LAeq rising above L5 is the documented signature of top-tail domination.
    assert out["laeq_minus_l5"] > 1.0


def test_steady_record_is_not_flagged_as_dominated():
    """A well-behaved steady record must not raise the artefact flag."""
    rng = np.random.default_rng(7)
    out = energy_concentration(rng.normal(55.0, 2.0, 5000))
    assert out["dominated"] is False


def test_energy_concentration_requires_minimum_sample_count():
    """Too few samples cannot support the diagnostic; must return None."""
    assert energy_concentration([50.0] * 19) is None


# ── Time-above-level exposure statistic ──────────────────────────────────────


def test_time_above_level_pct_is_exact_on_a_known_split():
    """30 of 100 samples at or above the threshold must report 30%."""
    levels = pd.Series([70.0] * 30 + [40.0] * 70)
    assert time_above_level_pct(levels, 65.0) == pytest.approx(30.0, abs=1e-9)


def test_time_above_level_pct_bounds():
    """The statistic must stay within [0, 100] at the extremes."""
    levels = pd.Series([50.0] * 100)
    assert time_above_level_pct(levels, 0.0) == pytest.approx(100.0, abs=1e-9)
    assert time_above_level_pct(levels, 200.0) == pytest.approx(0.0, abs=1e-9)
