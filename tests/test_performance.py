"""Self-contained cache tests; no external server or research files required."""
from test_dashboard_regressions import webapp, upload, record


def test_analysis_cache_reused_and_filters_do_not_poison_it(webapp):
    client = webapp.app.test_client()
    token = upload(client, record((1, 2)))
    body = {'filepath': token}
    first = client.post('/api/analyze', json=body)
    assert first.status_code == 200
    entry = next(iter(webapp._DATA_CACHE.values()))
    cached = entry['analysis']
    second = client.post('/api/analyze', json=body)
    assert second.status_code == 200
    assert entry['analysis'] is cached
    filtered = client.post('/api/analyze', json={**body, 'filters': {'bound_end': '2026-09-01'}})
    assert filtered.status_code == 200
    assert filtered.json['key_findings']['lden'] < first.json['key_findings']['lden']
    again = client.post('/api/analyze', json=body)
    assert again.json['key_findings'] == first.json['key_findings']
