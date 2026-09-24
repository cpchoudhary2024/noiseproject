"""Synthetic API regression tests; never reads study data or existing reports."""
import io
import json
import math
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))


@pytest.fixture
def webapp(tmp_path, monkeypatch):
    monkeypatch.setenv('UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setenv('ARTIFACTS_DIR', str(tmp_path / 'artifacts'))
    monkeypatch.setenv('RETENTION_ENABLED', '0')
    import app as module
    raw = tmp_path / 'uploads' / 'raw'
    raw.mkdir(parents=True, exist_ok=True)
    module.app.config.update(TESTING=True, RAW_UPLOAD_FOLDER=str(raw), UPLOAD_FOLDER=str(raw.parent))
    for name, suffix in [('ARTIFACTS_REPORTS_DIR','reports'), ('ARTIFACTS_CHARTS_DIR','charts'),
                         ('WEATHER_SCREENS_DIR','weather_screens'), ('WEATHER_CACHE_DIR','weather_cache')]:
        path = tmp_path / 'artifacts' / suffix
        path.mkdir(parents=True, exist_ok=True)
        monkeypatch.setattr(module, name, str(path))
        module.app.config[name] = str(path)
    module._DATA_CACHE.clear()
    return module


def upload(client, content, name='synthetic.csv'):
    r = client.post('/api/upload', data={'file': (io.BytesIO(content.encode()), name)})
    assert r.status_code == 200, r.get_data(as_text=True)
    return r.json['filepath']


def record(days=(1,), integer=False):
    return 'Time,L-Max dB -A,LEQ dB -A,L-Min dB -A\n' + ''.join(
        f'2026-09-{d:02} {h:02}:00:00,{80 if integer else 80.1},{40 if integer else (40.1 if d==1 else 80.1)},30\n'
        for d in days for h in range(24))


def test_filters_health_matrix_and_peak_agree(webapp):
    c = webapp.app.test_client()
    token = upload(c, record((1, 2)))
    body = dict(filepath=token, filters={'bound_end': '2026-09-01'})
    a = c.post('/api/analyze', json=body)
    assert a.status_code == 200
    expected = 40.1 + 10 * math.log10((12 + 4*10**0.5 + 8*10)/24)
    assert a.json['key_findings']['lden'] == pytest.approx(expected, abs=.01)
    m = c.post('/api/compliance-check', json=body)
    h = c.post('/api/health-assessment', json=body)
    assert m.status_code == h.status_code == 200
    assert m.json['compliance_matrix'] == a.json['compliance_matrix']
    assert h.json['health_assessment']['lden'] == pytest.approx(expected, abs=.01)
    assert h.json['health_assessment']['concern_level'] == a.json['analysis']['health_assessment']['concern_level']
    body['environment'] = 'indoor'
    indoor = c.post('/api/compliance-check', json=body).json['compliance_matrix']
    peak = next(r for r in indoor if r['metric'] == 'LAmax')
    assert peak['measured_db'] == 80.1
    assert peak['status'] == 'ABOVE'
    assert not any(r['category'] != 'who_indoor' for r in indoor)


def test_constant_integer_data_is_strict_json(webapp):
    c = webapp.app.test_client()
    token = upload(c, record(integer=True))
    for route in ('analyze', 'health-assessment', 'get-computed-summaries'):
        r = c.post('/api/' + route, json={'filepath': token})
        assert r.status_code == 200
        def reject(value):
            raise AssertionError('Non-JSON constant: ' + value)
        json.loads(r.get_data(as_text=True), parse_constant=reject)


def test_session_ownership_tampering_and_collision(webapp):
    c = webapp.app.test_client()
    other = webapp.app.test_client()
    first = upload(c, record())
    second = upload(c, record(integer=True))
    assert first != second
    for bad in [first, '../../sample_data.csv', '/etc/passwd', first+'x']:
        assert other.post('/api/analyze', json={'filepath': bad}).status_code == 404
    assert c.post('/api/export-daily-summary-csv', json={'filepath': first}).status_code == 200
    assert len(list(Path(webapp.app.config['RAW_UPLOAD_FOLDER']).glob('*.csv'))) == 2


def test_missing_period_is_not_zero_or_overall_pass(webapp):
    c = webapp.app.test_client()
    token = upload(c, 'Time,LEQ\n2026-09-01 01:00:00,35\n2026-09-01 02:00:00,35\n')
    r = c.post('/api/analyze', json={'filepath': token})
    assert r.status_code == 200
    h = r.json['analysis']['health_assessment']
    assert h['lden'] is None
    assert h['concern_level'] == 'INCOMPLETE'
    assert not any(row['metric'] == 'Lden' for row in r.json['compliance_matrix'])


def test_indicative_current_criteria(webapp):
    c = webapp.app.test_client()
    token = upload(c, record())
    rows = c.post('/api/compliance-check', json={'filepath': token}).json['compliance_matrix']
    assert all(r['kind'] == 'indicative' for r in rows)
    assert not any('goal' in r['standard'] for r in rows)
    assert all('26.02.03.02B(1)' in r['source'] for r in rows if r['category']=='maryland')


def test_comparison_report_rejects_unverified_client_numbers(webapp):
    c = webapp.app.test_client()
    r = c.post('/api/compare-report', json={'datasets':[{'lden': 1},{'lden': 2}]})
    assert r.status_code == 400


@pytest.mark.parametrize('route', ['export-data', 'export-daily-summary', 'export-hourly-summary',
                                    'export-daily-summary-csv', 'export-hourly-summary-csv',
                                    'export-weekly-summary', 'generate-advanced-charts'])
def test_exports_refuse_invalid_weather_screen(webapp, route):
    c = webapp.app.test_client()
    token = upload(c, record())
    result = c.post('/api/' + route, json={'filepath': token,
                   'filters': {'weather_screen_id': 'not-a-valid-screen'}})
    assert result.status_code == 400, result.get_data(as_text=True)


def test_csv_export_honors_selected_period(webapp):
    c = webapp.app.test_client()
    token = upload(c, record((1, 2)))
    result = c.post('/api/export-daily-summary-csv', json={'filepath': token,
                   'filters': {'bound_end': '2026-09-01'}})
    assert result.status_code == 200
    assert '2026-09-02' not in result.get_data(as_text=True)


def test_saved_weather_screen_matches_analysis_and_exports(webapp):
    import pandas as pd
    from analysis.weather_screen import ScreenConfig, block_keys
    from services.weather_screening import fingerprint, RECORD_VERSION
    c = webapp.app.test_client()
    token = upload(c, record((1, 2)))
    ts = pd.Series(pd.date_range('2026-09-01', periods=48, freq='h'))
    keys = block_keys(ts, 15)
    screen_id = '0123456789abcdef'
    saved = {'version': RECORD_VERSION, 'screen_id': screen_id, 'created_utc': '2026-09-24T00:00:00Z',
             'fingerprint': fingerprint(ts), 'clock': 'utc', 'tz': 'America/New_York',
             'station': {'station_id': 'TEST', 'name': 'Test', 'distance_km': 10.0, 'tz': 'America/New_York'},
             'location_basis': 'test', 'sources': 'synthetic weather', 'config': ScreenConfig().to_dict(),
             'wind_height_factor': 0.641939, 'range_blocks': [int(keys.min()), int(keys.max())],
             'excluded': {str(k): 'precipitation' for k in keys[24:]}, 'summary': {}}
    Path(webapp.WEATHER_SCREENS_DIR, screen_id + '.json').write_text(json.dumps(saved))
    from itsdangerous import URLSafeTimedSerializer
    with c.session_transaction() as session:
        owner = session['upload_owner']
    screen_token = URLSafeTimedSerializer(webapp.app.secret_key, salt='weather').dumps(
        {'owner': owner, 'value': screen_id})
    body = {'filepath': token, 'filters': {'weather_screen_id': screen_token}}
    analysis = c.post('/api/analyze', json=body)
    assert analysis.status_code == 200
    assert analysis.json['filter_summary']['weather_screen']['rows_removed'] == 24
    csv = c.post('/api/export-daily-summary-csv', json=body)
    assert csv.status_code == 200
    assert '2026-09-02' not in csv.get_data(as_text=True)
    excel = c.post('/api/export-daily-summary', json=body)
    assert excel.status_code == 200
    book = pd.ExcelFile(io.BytesIO(excel.data))
    assert 'Weather Screening' in book.sheet_names
    meta = pd.read_excel(book, sheet_name='Weather Screening', header=None)
    assert 'rows_removed' in meta[0].values
    assert meta.loc[meta[0] == 'rows_removed', 1].iloc[0] == 24
