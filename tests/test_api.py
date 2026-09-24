"""API tests run in-process through Flask's test client (no server needed).

Covers the upload → analyze → report flow, the analysis cache, upload
isolation (unguessable names, no path access) and JSON validity.
"""

import importlib
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLE_CSV = REPO_ROOT / 'sample_data.csv'


@pytest.fixture(scope='module')
def client(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('platform')
    os.environ['UPLOAD_FOLDER'] = str(tmp / 'uploads')
    os.environ['ARTIFACTS_DIR'] = str(tmp / 'artifacts')
    backend = str(REPO_ROOT / 'backend')
    if backend not in sys.path:
        sys.path.insert(0, backend)
    app_module = importlib.import_module('app')
    app_module.app.config['TESTING'] = True
    with app_module.app.test_client() as c:
        c.app_module = app_module
        yield c


def _upload(client, name='sample_data.csv'):
    with open(SAMPLE_CSV, 'rb') as fh:
        resp = client.post('/api/upload', data={'file': (fh, name)},
                           content_type='multipart/form-data')
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def test_upload_returns_unguessable_reference(client):
    a, b = _upload(client), _upload(client)
    assert a['filepath'] != b['filepath'], 'same-second uploads of one file must not collide'
    for ref in (a['filepath'], b['filepath']):
        assert os.sep not in ref
        # Opaque signed reference: no filename, no path, not guessable.
        assert len(ref) > 40 and '/' not in ref and '..' not in ref


@pytest.mark.parametrize('ref', [
    '../../etc/passwd',
    '/etc/passwd',
    'sample_data.csv',
    '20240115_080000_sample_data.csv',   # the old, predictable naming scheme
])
def test_references_not_issued_by_server_are_refused(client, ref):
    resp = client.post('/api/analyze', json={'filepath': ref})
    assert resp.status_code == 404


def test_upload_keeps_extension_of_non_ascii_names(client):
    ref = _upload(client, name='测试.csv')['filepath']
    # The reference is opaque, so check the stored file keeps the extension.
    raw = client.app_module.app.config['RAW_UPLOAD_FOLDER']
    stored = max((os.path.join(raw, f) for f in os.listdir(raw)), key=os.path.getmtime)
    assert stored.endswith('.csv')


def test_analyze_then_cached_rerun(client):
    ref = _upload(client)['filepath']

    t0 = time.perf_counter()
    first = client.post('/api/analyze', json={'filepath': ref})
    t_first = time.perf_counter() - t0
    assert first.status_code == 200, first.get_json()

    t0 = time.perf_counter()
    second = client.post('/api/analyze', json={'filepath': ref})
    t_second = time.perf_counter() - t0
    assert second.status_code == 200

    # Same record, same results.
    a, b = first.get_json(), second.get_json()
    assert a['compliance_matrix'] == b['compliance_matrix']
    # A cached run must not be materially slower than the first.
    assert t_second <= t_first * 1.5 + 0.5


def test_comparison_rows_are_comparisons_not_verdicts(client):
    ref = _upload(client)['filepath']
    rows = client.post('/api/analyze', json={'filepath': ref}).get_json()['compliance_matrix']
    assert rows, 'sample record has timestamps, so the comparison must be computed'
    for r in rows:
        assert r['status'] in {'ABOVE', 'AT OR BELOW'}
        assert r['kind'] in {'comparison', 'indicative'}
        assert (r['status'] == 'ABOVE') == (r['measured_db'] > r['limit_db'])


def test_pdf_report_is_generated(client):
    ref = _upload(client)['filepath']
    resp = client.post('/api/generate-report',
                       json={'filepath': ref, 'report_type': 'comprehensive', 'format': 'pdf'})
    assert resp.status_code == 200, resp.get_data(as_text=True)[:500]
    assert resp.data[:5] == b'%PDF-'


def test_responses_are_valid_json_for_numpy_and_missing_values(client):
    app_module = client.app_module
    payload = {'i': np.int64(3), 'b': np.bool_(True), 'na': pd.NA, 'nat': pd.NaT,
               'nan': float('nan'), 'arr': np.array([1.0, np.nan]), 'f32': np.float32(2.5)}
    with app_module.app.app_context():
        text = app_module.app.json.dumps(payload)
    assert json.loads(text) == {'i': 3, 'b': True, 'na': None, 'nat': None,
                                'nan': None, 'arr': [1.0, None], 'f32': 2.5}
