"""Environmental metrics and visualisation engines on the sample record."""

import os
import sys
from pathlib import Path

import pandas as pd
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / 'backend'))

from analysis.environmental_viz import EnvironmentalVisualizationEngine  # noqa: E402
from analysis.environmental_metrics import EnvironmentalMetricsCalculator  # noqa: E402

NOISE_COL = 'Noise_Level_dB'


@pytest.fixture(scope='module')
def sample_df():
    return pd.read_csv(REPO_ROOT / 'sample_data.csv')


def test_environmental_metrics(sample_df):
    calc = EnvironmentalMetricsCalculator(sample_df, [NOISE_COL], 'Timestamp')
    metrics = calc.calculate_all_metrics(NOISE_COL)
    for key in ('exceedance', 'percentiles', 'temporal', 'variability', 'trend', 'anomalies'):
        assert key in metrics, f'missing {key} metrics'
    perc = metrics['percentiles']
    assert perc['L90'] <= perc['L50'] <= perc['L10']
    assert perc['IQR'] >= 0


@pytest.mark.parametrize('method, kwargs', [
    ('generate_exceedance_analysis', {}),
    ('generate_enhanced_temporal_heatmap', {'resolution': 'hourly'}),
    ('generate_diurnal_box_whisker', {}),
    ('generate_violin_distribution', {}),
    ('generate_cumulative_distribution', {}),
    ('generate_anomaly_detection', {'threshold_std': 2.5}),
])
def test_visualisations(sample_df, method, kwargs):
    viz = EnvironmentalVisualizationEngine(sample_df, NOISE_COL)
    fig = getattr(viz, method)(noise_col=NOISE_COL, **kwargs)
    assert len(fig.data) > 0, f'{method} returned an empty figure'
    assert len(fig.to_json()) > 100
