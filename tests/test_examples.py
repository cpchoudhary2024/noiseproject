"""The published example record must stay loadable and give the documented numbers."""
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from analysis.acoustics import energetic_mean_db  # noqa: E402

EXAMPLE = Path(__file__).resolve().parents[1] / 'examples' / 'demo-site-4-days.parquet'


@pytest.fixture(scope='module')
def demo():
    return pd.read_parquet(EXAMPLE)


def test_example_record_shape(demo):
    assert len(demo) == 338400                      # 4 days at 1 Hz minus a 2 hour gap
    assert demo.iloc[0, 0] == pd.Timestamp('2026-04-06 00:00:00')
    assert demo.iloc[-1, 0] == pd.Timestamp('2026-04-09 23:59:59')


def test_example_laeq_matches_the_definition(demo):
    levels = demo[' LEQ dB -A '].to_numpy()
    by_hand = 10 * np.log10(np.mean(10 ** (levels / 10)))
    assert energetic_mean_db(levels) == pytest.approx(by_hand, abs=1e-9)
    assert by_hand == pytest.approx(51.29, abs=0.01)   # value quoted in examples/sample-report.pdf


def test_example_has_the_two_hour_gap(demo):
    steps = demo.iloc[:, 0].diff().dt.total_seconds()
    assert steps.max() == pytest.approx(7201.0)
