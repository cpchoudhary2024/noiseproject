"""Timestamp parsing: dates are checked against independently known values."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

import analysis.timestamp_utils as timestamp_utils  # noqa: E402
from analysis.noise_analyzer import NoiseAnalyzer  # noqa: E402


def _logger_frame(stamps):
    return pd.DataFrame({'Time (Date hh:mm:ss.ms)': stamps,
                         'LEQ dB -A': [50.0] * len(stamps)})


YEAR_FIRST = ['2026/03/05 10:00:00.000', '2026/03/06 10:00:00.000', '2026/03/07 10:00:00.000']
# The same instants as calendar dates: 5, 6 and 7 March 2026.
YEAR_FIRST_TRUE = ['2026-03-05', '2026-03-06', '2026-03-07']


def test_analyzer_reads_year_first_logger_dates():
    analyzer = NoiseAnalyzer(_logger_frame(YEAR_FIRST))
    parsed = analyzer._parsed_timestamps('Time (Date hh:mm:ss.ms)')
    assert list(parsed.dt.strftime('%Y-%m-%d')) == YEAR_FIRST_TRUE


def test_analyzer_does_not_substitute_dayfirst_dates_when_parsing_fails(monkeypatch):
    """A parser failure must surface, not be replaced by a day-first reading."""
    def broken_parser(raw):
        raise RuntimeError('simulated parser failure')

    monkeypatch.setattr(timestamp_utils, 'parse_timestamps_robust', broken_parser)
    analyzer = NoiseAnalyzer(_logger_frame(YEAR_FIRST))
    with pytest.raises(RuntimeError, match='simulated parser failure'):
        analyzer._parsed_timestamps('Time (Date hh:mm:ss.ms)')


@pytest.mark.xfail(strict=True, reason=(
    'OPEN DECISION: a month-first (US) export whose days are all <= 12 is read '
    'day-first, so 1-3 March become 3 Jan, 3 Feb and 3 Mar. Choosing a US default '
    'or flagging the ambiguity changes results for some inputs and is left to the '
    'project owner. Study loggers write year-first dates and are not affected.'))
def test_short_us_month_first_export_keeps_true_dates():
    us = pd.Series(['03/01/2026 10:00:00', '03/02/2026 10:00:00', '03/03/2026 10:00:00'])
    parsed, _method = timestamp_utils.parse_timestamps_robust(us)
    assert list(parsed.dt.strftime('%Y-%m-%d')) == ['2026-03-01', '2026-03-02', '2026-03-03']


def test_time_series_summary_output_is_unchanged():
    """Pins the summary this method has always returned for timed and untimed data."""
    stamps = [f'2026/03/05 {h:02d}:00:00.000' for h in range(6)]
    timed = NoiseAnalyzer(_logger_frame(stamps))._time_series_analysis()
    assert timed == {'LEQ dB -A': {'hourly_pattern': 'Available',
                                   'peak_hour': 'See charts', 'quiet_hour': 'See charts'}}

    untimed = NoiseAnalyzer(pd.DataFrame({'LEQ dB -A': [50.0, 51.0]}))._time_series_analysis()
    assert untimed == {'status': 'No time data available'}
