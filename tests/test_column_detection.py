"""Which columns count as sound levels."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from analysis.data_summarizer import DataSummarizer  # noqa: E402
from analysis.iso_epa_standards import StandardsAnalyzer  # noqa: E402
from analysis.noise_analyzer import NoiseAnalyzer  # noqa: E402

ANALYSERS = (NoiseAnalyzer, StandardsAnalyzer, DataSummarizer)


def test_all_analysers_agree_on_the_study_logger_layout():
    df = pd.DataFrame({'Time (Date hh:mm:ss.ms)': ['2026/03/05 10:00:00.000'] * 3,
                       'L-Max dB -A': [70.0, 71.0, 72.0],
                       'LEQ dB -A': [60.0, 61.0, 62.0],
                       'L-Min dB -A': [50.0, 51.0, 52.0]})
    expected = ['L-Max dB -A', 'LEQ dB -A', 'L-Min dB -A']
    for analyser in ANALYSERS:
        assert analyser(df).noise_columns == expected, analyser.__name__


@pytest.mark.xfail(strict=True, reason=(
    'OPEN DECISION: StandardsAnalyzer and DataSummarizer match the substring "lp" '
    '(meant for Lp), so a numeric column such as "alpha" is treated as a dB series; '
    'NoiseAnalyzer does not. One shared detector would fix it but changes which '
    'columns are analysed for such files, so it is left to the project owner.'))
def test_all_analysers_ignore_a_non_acoustic_column():
    df = pd.DataFrame({'LEQ dB': [60.0, 61.0, 62.0], 'alpha': [0.1, 0.2, 0.3]})
    for analyser in ANALYSERS:
        assert analyser(df).noise_columns == ['LEQ dB'], analyser.__name__
