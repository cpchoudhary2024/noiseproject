"""Files from different instruments must give the same, correct numbers.

Expected values are worked out here from the definitions, never by calling the
code under test.
"""
import io
import math
import struct
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from backend.analysis.ingest import IngestError, _role, read_cil

N = 3 * 3600   # three hours at 1 s
START = datetime(2026, 4, 6, 9, 0, 0)


def laeq(levels):
    return 10 * math.log10(sum(10 ** (v / 10) for v in levels) / len(levels))


@pytest.fixture(scope='module')
def levels():
    rng = np.random.default_rng(7)
    leq = np.round(50 + 8 * rng.random(N), 1)
    return leq, np.round(leq + 3.2, 1), np.round(leq - 2.1, 1)


def _analyse(client, payload, name):
    up = client.post('/api/upload', data={'file': (io.BytesIO(payload), name)},
                     content_type='multipart/form-data')
    body = up.get_json()
    if up.status_code != 200:
        return up.status_code, body, None
    res = client.post('/api/analyze', json={'filepath': body['filepath']}).get_json()
    return up.status_code, body, res


@pytest.mark.parametrize('name, role', [
    (' LEQ dB -A ', 'leq'), (' L-Max dB -A ', 'lmax'), (' L-Min dB -A ', 'lmin'),
    ('LAeq', 'leq'), ('LAeq,1min', 'leq'), ('Leq (dBA)', 'leq'), ('LAFmax', 'lmax'),
    ('LASmin', 'lmin'), ('Max level (dB)', 'lmax'),
    ('LCeq', 'skip'), ('LZeq', 'skip'), ('LApk', 'skip'), ('LCpk', 'skip'),
    ('Leq dB(C)', 'skip'), ('Lavg (dB)', 'skip'), ('LAE', 'skip'), ('SEL dB', 'skip'),
    ('Time (Date hh:mm:ss.ms)', None), ('Battery (%)', None), ('Location', None), ('Minute', None),
])
def test_column_roles(name, role):
    assert _role(name) == role


def _frame(levels, order, names, sep=',', decimal='.'):
    leq, lmax, lmin = levels
    times = [(START + timedelta(seconds=i)).strftime('%Y-%m-%d %H:%M:%S') for i in range(N)]
    cols = {'time': ('Time', times), 'leq': (names[0], leq), 'lmax': (names[1], lmax),
            'lmin': (names[2], lmin), 'extra': ('Battery (%)', np.full(N, 97))}
    df = pd.DataFrame({cols[k][0]: cols[k][1] for k in order})
    return df.to_csv(index=False, sep=sep, decimal=decimal).encode()


@pytest.mark.parametrize('order, names, sep, decimal', [
    (['time', 'lmax', 'leq', 'lmin'], (' LEQ dB -A ', ' L-Max dB -A ', ' L-Min dB -A '), '\t', '.'),
    (['leq', 'time', 'lmin', 'extra', 'lmax'], ('LAeq', 'LAFmax', 'LAFmin'), ',', '.'),
    (['time', 'lmin', 'lmax', 'leq'], ('Leq (dBA)', 'Lmax (dBA)', 'Lmin (dBA)'), ';', ','),
])
def test_layouts_give_the_true_values(client, levels, order, names, sep, decimal):
    status, up, res = _analyse(client, _frame(levels, order, names, sep, decimal), 'export.csv')
    assert status == 200, up
    assert up['rows'] == N                      # no reading dropped on the way in
    kf = res['key_findings']
    assert kf['avg_laeq'] == pytest.approx(laeq(levels[0]), abs=1e-9)
    assert kf['peak'] == pytest.approx(levels[1].max())


def test_unusable_level_columns_are_named_not_silently_used(client, levels):
    leq, lmax, _ = levels
    times = [(START + timedelta(seconds=i)).isoformat(sep=' ') for i in range(N)]
    df = pd.DataFrame({'Time': times, 'LCeq': leq + 10, 'LAeq': leq, 'LApk': lmax + 20, 'LAFmax': lmax})
    _, _, res = _analyse(client, df.to_csv(index=False).encode(), 'meter.csv')
    assert res['key_findings']['avg_laeq'] == pytest.approx(laeq(leq), abs=1e-9)
    notes = ' '.join(res['ingest_warnings'])
    assert '"LCeq"' in notes and '"LApk"' in notes


def test_two_candidate_leq_columns_are_refused(client, levels):
    times = [(START + timedelta(seconds=i)).isoformat(sep=' ') for i in range(N)]
    df = pd.DataFrame({'Time': times, 'LAeq': levels[0], 'Leq dBA': levels[0] + 1})
    status, body, _ = _analyse(client, df.to_csv(index=False).encode(), 'two.csv')
    assert status == 400 and 'More than one column' in body['error']


def _larson_davis(levels, overload_rows):
    leq, lmax, _ = levels
    stamps = [(START + timedelta(seconds=i)).strftime('%Y-%m-%d  %H:%M:%S') for i in range(N)]
    th = pd.DataFrame({'  Record Type  ': ['Start'] + [None] * (N - 2) + ['Stop'],
                       '              Date/Time     ': stamps,
                       '    LAeq    ': leq, '    LCeq    ': leq + 9, '    LCpk    ': lmax + 25,
                       '    LASmax    ': lmax - 1, '    LAFmax    ': lmax,
                       'Overload': [('Yes' if i in overload_rows else None) for i in range(N)]})
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as xw:
        pd.DataFrame([['Spartan 730 Summary']]).to_excel(xw, sheet_name='Summary', header=False, index=False)
        th.to_excel(xw, sheet_name='Time History', index=False)
    return buf.getvalue()


def test_larson_davis_export(client, levels):
    flagged = {10, 11, 500}
    status, up, res = _analyse(client, _larson_davis(levels, flagged), 'ld.xlsx')
    assert status == 200, up
    kept = [v for i, v in enumerate(levels[0]) if i not in flagged]
    assert res['key_findings']['avg_laeq'] == pytest.approx(laeq(kept), abs=1e-9)
    assert res['key_findings']['peak'] == pytest.approx(max(v for i, v in enumerate(levels[1]) if i not in flagged))
    notes = ' '.join(res['ingest_warnings'])
    assert '3 readings flagged overload' in notes
    assert '"LASmax"' in notes and '"LCeq"' in notes


def _cil(start_utc, step, leq, lmax, lmin):
    def text(s):
        return struct.pack('>i', len(s)) + s.encode()
    secs = (start_utc - datetime(1904, 1, 1, tzinfo=timezone.utc)).total_seconds()
    out = text('NSRTW_mk4_Audio') + text('1.70') + text('DEVICE') + text('1 Jan 2026 - 00:00') + struct.pack('>i', 1)
    for block in (lmax, leq, lmin):
        out += struct.pack('>ddi', secs, step, len(block)) + np.asarray(block, '>f8').tobytes()
    return out + b'\x00' * 40


def test_cil_across_the_november_clock_change(tmp_path):
    # 05:30 UTC on 1 Nov 2026 is 01:30 EDT; the 01:00 hour then repeats in EST
    start = datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)
    leq = np.arange(120) / 10 + 40
    path = tmp_path / 'night.cil'
    path.write_bytes(_cil(start, 60.0, leq, leq + 5, leq - 5))
    df = read_cil(path)
    ny = ZoneInfo('America/New_York')
    expected = [(start + timedelta(minutes=i)).astimezone(ny).replace(tzinfo=None) for i in range(120)]
    assert list(df.iloc[:, 0]) == expected
    assert np.array_equal(df[' LEQ dB -A '], leq)
    assert np.array_equal(df[' L-Max dB -A '], leq + 5)
    assert np.array_equal(df[' L-Min dB -A '], leq - 5)


def test_truncated_cil_is_refused(tmp_path):
    good = _cil(datetime(2026, 4, 6, tzinfo=timezone.utc), 1.0, *([np.full(100, 50.0)] * 3))
    path = tmp_path / 'cut.cil'
    path.write_bytes(good[:len(good) // 2])
    with pytest.raises(IngestError):
        read_cil(path)


def test_wlg_is_refused_not_guessed(client):
    status, body, _ = _analyse(client, b'\x00\x00\x02\xd4\xff\xff' + bytes(range(256)) * 400, 'old.wlg')
    assert status == 400 and '.cil' in body['error']


def _nsrt(start, n, leq):
    times = [(start + timedelta(seconds=i)).strftime('%Y/%m/%d %H:%M:%S.000') for i in range(n)]
    return pd.DataFrame({'Time (Date hh:mm:ss.ms)': times, ' L-Max dB -A ': leq + 3,
                         ' LEQ dB -A ': leq, ' L-Min dB -A ': leq - 2})


def test_overlapping_files_are_reported(client):
    a = _nsrt(START, 1000, np.full(1000, 50.0)).to_csv(index=False, sep='\t').encode()
    b = _nsrt(START + timedelta(seconds=900), 1000, np.full(1000, 60.0)).to_csv(index=False, sep='\t').encode()
    resp = client.post('/api/upload-multi', data={'files': [(io.BytesIO(a), 'a.csv'), (io.BytesIO(b), 'b.csv')]},
                       content_type='multipart/form-data')
    body = resp.get_json()
    assert resp.status_code == 200, body
    assert body['rows'] == 1900
    assert any(n.startswith('100 readings had the same time') for n in body['merge_notes'])


def test_unreadable_times_are_counted_for_the_user(client):
    df = _nsrt(START, 2000, np.full(2000, 55.0))
    df.iloc[[3, 50, 700, 701, 1999], 0] = 'not a time'
    _, _, res = _analyse(client, df.to_csv(index=False, sep='\t').encode(), 'bad.csv')
    assert any(n.startswith('5 readings have a date/time that could not be read') for n in res['ingest_warnings'])
