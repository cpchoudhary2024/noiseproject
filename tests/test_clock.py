"""Clock conversion checked against independently known UTC offsets."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from analysis.clock import FOLD_COLUMN, ClockSetting, convert_times, mark_repeated_hour  # noqa: E402

EASTERN_TO_UTC = ClockSetting(source='America/New_York', target='UTC')


def _convert(stamps, fold=None, setting=EASTERN_TO_UTC):
    ts = pd.Series(pd.to_datetime(stamps))
    fold_series = pd.Series(fold, dtype='int8') if fold is not None else None
    return convert_times(ts, fold_series, setting)


def test_standard_and_daylight_offsets():
    converted, _fold, _info = _convert(['2026-01-15 12:00:00', '2026-07-01 12:00:00'])
    assert list(converted) == [pd.Timestamp('2026-01-15 17:00:00'),   # EST, UTC-5
                               pd.Timestamp('2026-07-01 16:00:00')]   # EDT, UTC-4


def test_spring_forward_gap_is_not_invented():
    converted, _fold, info = _convert(['2026-03-08 01:59:59', '2026-03-08 02:30:00',
                                       '2026-03-08 03:00:00'])
    assert converted[0] == pd.Timestamp('2026-03-08 06:59:59')
    assert pd.isna(converted[1])                      # 02:30 did not happen
    assert converted[2] == pd.Timestamp('2026-03-08 07:00:00')
    assert info['nonexistent_source_readings'] == 1


def test_repeated_autumn_hour_keeps_both_passes():
    # 01:30 on 1 Nov 2026 occurs at 05:30 UTC (EDT) and again at 06:30 UTC (EST).
    converted, _fold, _info = _convert(['2026-11-01 01:30:00', '2026-11-01 01:30:00'],
                                       fold=[0, 1])
    assert list(converted) == [pd.Timestamp('2026-11-01 05:30:00'),
                               pd.Timestamp('2026-11-01 06:30:00')]


def test_second_pass_is_identified_from_recorded_order():
    stamps = ['2026-11-01 00:30:00', '2026-11-01 01:00:00', '2026-11-01 01:30:00',
              '2026-11-01 01:00:00', '2026-11-01 01:30:00', '2026-11-01 02:00:00']
    df = pd.DataFrame({'Time': pd.to_datetime(stamps), 'LEQ': 50.0})
    marked = mark_repeated_hour(df, 'Time')
    assert list(marked[FOLD_COLUMN]) == [0, 0, 0, 1, 1, 0]


def test_identity_setting_returns_input_unchanged():
    setting = ClockSetting(source='America/New_York', target='America/New_York')
    stamps = ['2026-03-08 01:59:59', '2026-07-01 12:00:00']
    converted, _fold, info = _convert(stamps, setting=setting)
    assert list(converted) == list(pd.to_datetime(stamps))
    assert info['converted'] is False


def test_unknown_zone_is_refused():
    from analysis.clock import ClockError
    with pytest.raises(ClockError):
        ClockSetting.from_request({'source': 'Mars/Olympus_Mons', 'target': 'UTC'})
