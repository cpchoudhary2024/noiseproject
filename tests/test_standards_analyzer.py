"""StandardsAnalyzer must stay descriptive: no ISO, EPA or OSHA verdicts."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from analysis.iso_epa_standards import StandardsAnalyzer  # noqa: E402


@pytest.fixture
def result():
    # Constant 60 dB(A): the energy average is exactly 60.0 dB by definition.
    df = pd.DataFrame({'LEQ dB -A': [60.0] * 100})
    return StandardsAnalyzer(df).analyze()


def test_issues_no_regulatory_verdicts(result):
    assert result['iso_analysis']['compliance_status'] == {}
    assert result['epa_analysis']['osha_pel_compliance'] == {}
    assert result['epa_analysis']['standards'] == {}


def test_descriptive_level_is_the_energy_average(result):
    detail = result['epa_analysis']['detailed_assessment']['LEQ dB -A']
    assert detail['measured_laeq'] == pytest.approx(60.0)
    assert detail['measured_max'] == pytest.approx(60.0)


def test_note_points_to_nothing_that_does_not_exist(result):
    """The note once directed users to an occupational endpoint that was removed."""
    assert 'endpoint' not in result['epa_analysis']['epa_note']
