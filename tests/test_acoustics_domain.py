"""Regulatory-reference unit tests for the acoustic domain math."""

import math

import numpy as np
import pandas as pd
import pytest

from backend.analysis.acoustics import (
    DayEveningNightDefinition,
    compute_ldn_lden,
    energetic_mean_db,
    energy_concentration,
    exceedance_levels_db,
    time_above_level_pct,
)

# Tolerance for dB comparisons.
DB_TOL = 1e-6


# Energy averaging (ISO 1996-1)


def test_energetic_mean_of_identical_levels_is_that_level():
    """A constant signal must energy-average to its own level."""
    assert energetic_mean_db([70.0] * 100) == pytest.approx(70.0, abs=DB_TOL)


def test_energetic_mean_of_two_equal_sources_adds_three_db():
    """Doubling acoustic energy raises the level by 10*log10(2) = 3.0103 dB."""
    combined = 10.0 * math.log10(2 * 10 ** (70.0 / 10.0))
    assert combined == pytest.approx(73.0103, abs=1e-4)


def test_energy_average_exceeds_arithmetic_mean_for_varying_levels():
    """For any non-constant series the energy mean must exceed the arithmetic mean."""
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


# Exceedance percentiles (Lx family)


def test_exceedance_levels_use_complement_percentiles():
    """L10 is the level exceeded 10% of the time = the 90th percentile."""
    out = exceedance_levels_db(list(range(101)))
    assert out["L10"] == pytest.approx(90.0, abs=DB_TOL)
    assert out["L50"] == pytest.approx(50.0, abs=DB_TOL)
    assert out["L90"] == pytest.approx(10.0, abs=DB_TOL)


def test_exceedance_levels_are_monotonically_ordered():
    """By construction L5 >= L10 >= L50 >= L90 >= L95 for any dataset."""
    rng = np.random.default_rng(42)
    out = exceedance_levels_db(rng.normal(55.0, 8.0, 5000))
    assert out["L5"] >= out["L10"] >= out["L50"] >= out["L90"] >= out["L95"]


# Ldn: +10 dB night penalty, duration-weighted


def _series_at_constant_level(level_db, hours, minutes_per_hour=60):
    stamps = []
    for h in hours:
        for m in range(minutes_per_hour):
            stamps.append(pd.Timestamp("2026-03-02") + pd.Timedelta(hours=h, minutes=m))
    ts = pd.Series(stamps)
    return ts, pd.Series([float(level_db)] * len(ts))


def test_ldn_of_uniform_60db_day_and_night():
    """A flat 60 dB(A) 24 h record has a hand-computable Ldn."""
    ts, levels = _series_at_constant_level(60.0, range(24))
    out = compute_ldn_lden(ts, levels)
    expected = 10.0 * math.log10((15 * 10 ** 6.0 + 9 * 10 ** 7.0) / 24.0)
    assert expected == pytest.approx(66.4098, abs=1e-4)
    assert out["Ldn"] == pytest.approx(expected, abs=1e-4)


def test_lden_of_uniform_60db_matches_eu_annex_i():
    """Flat 60 dB(A): Lden per EU 2002/49/EC Annex I, hand-computed."""
    ts, levels = _series_at_constant_level(60.0, range(24))
    out = compute_ldn_lden(ts, levels)
    expected = 10.0 * math.log10(
        (12 * 10 ** 6.0 + 4 * 10 ** 6.5 + 8 * 10 ** 7.0) / 24.0
    )
    assert expected == pytest.approx(66.3952, abs=1e-4)
    assert out["Lden"] == pytest.approx(expected, abs=1e-4)


def test_night_penalty_is_exactly_ten_db():
    """Raising only the night period by X dB must raise Ldn by X dB."""
    ts_a, lv_a = _series_at_constant_level(50.0, range(24))
    base = compute_ldn_lden(ts_a, lv_a)["Ldn"]
    ts_b, lv_b = _series_at_constant_level(60.0, range(24))
    lifted = compute_ldn_lden(ts_b, lv_b)["Ldn"]
    assert lifted - base == pytest.approx(10.0, abs=1e-6)


def test_ldn_is_duration_weighted_not_sample_weighted():
    """Oversampling the day must NOT bias Ldn downward."""
    dense_day = _series_at_constant_level(60.0, range(7, 22), minutes_per_hour=600)
    sparse_night = _series_at_constant_level(60.0, list(range(22, 24)) + list(range(0, 7)),
                                             minutes_per_hour=6)
    ts = pd.concat([dense_day[0], sparse_night[0]], ignore_index=True)
    lv = pd.concat([dense_day[1], sparse_night[1]], ignore_index=True)

    out = compute_ldn_lden(ts, lv)
    expected = 10.0 * math.log10((15 * 10 ** 6.0 + 9 * 10 ** 7.0) / 24.0)
    assert out["Ldn"] == pytest.approx(expected, abs=1e-4)


def test_ldn_is_none_when_a_period_is_missing():
    """A night-only record cannot yield an Ldn."""
    ts, levels = _series_at_constant_level(45.0, [23, 0, 1, 2, 3, 4, 5, 6])
    out = compute_ldn_lden(ts, levels)
    assert out.get("Ldn") is None
    assert out.get("Lden") is None
    # The night average itself is still well defined and should be reported.
    assert out["LAeq_night_lden"] == pytest.approx(45.0, abs=DB_TOL)


def test_generic_day_night_keys_carry_ldn_windows():
    """``LAeq_day``/``LAeq_night`` must use the 07-22 / 22-07 (Ldn) windows."""
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
    """The 22:00-07:00 night window must wrap across midnight."""
    ts, levels = _series_at_constant_level(60.0, range(24))
    out = compute_ldn_lden(ts, levels)
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


# Energy-domination diagnostic (QA/QC)


def test_single_loud_sample_is_flagged_as_dominating():
    """One 120 dB(A) spike among quiet samples must be flagged."""
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


# Time-above-level exposure statistic


def test_time_above_level_pct_is_exact_on_a_known_split():
    """30 of 100 samples at or above the threshold must report 30%."""
    levels = pd.Series([70.0] * 30 + [40.0] * 70)
    assert time_above_level_pct(levels, 65.0) == pytest.approx(30.0, abs=1e-9)


def test_time_above_level_pct_bounds():
    """The statistic must stay within [0, 100] at the extremes."""
    levels = pd.Series([50.0] * 100)
    assert time_above_level_pct(levels, 0.0) == pytest.approx(100.0, abs=1e-9)
    assert time_above_level_pct(levels, 200.0) == pytest.approx(0.0, abs=1e-9)
