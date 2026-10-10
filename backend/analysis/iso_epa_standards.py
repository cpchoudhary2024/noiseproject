# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.

import pandas as pd
import numpy as np
from datetime import datetime

from analysis.acoustics import energetic_mean_db
from analysis.standards_reference import who_2018_environmental_noise_guideline_levels

class StandardsAnalyzer:
    """Descriptive levels for the standards payload; issues no regulatory verdict."""

    def __init__(self, df):
        self.df = df.copy()
        self.noise_columns = self._identify_noise_columns()

    def _identify_noise_columns(self):
        """Identify noise measurement columns."""

        def looks_like_noise_measurement_name(col_lower):
            if any(token in col_lower for token in ['db', 'dba', 'decibel', 'spl', 'leq', 'laeq', 'lmax', 'lmin', 'l-max', 'l-min', 'l_eq', 'lp']):
                return True
            if 'noise' in col_lower and 'level' in col_lower:
                return True
            if 'sound' in col_lower and 'level' in col_lower:
                return True
            return False

        def is_probably_numeric(series):
            if pd.api.types.is_numeric_dtype(series):
                return True
            coerced = pd.to_numeric(series, errors='coerce')
            if len(coerced) == 0:
                return False
            return float(coerced.notna().mean()) >= 0.80

        candidates = []
        for col in self.df.columns:
            col_lower = str(col).lower()
            if not looks_like_noise_measurement_name(col_lower):
                continue
            if is_probably_numeric(self.df[col]):
                candidates.append(col)

        if candidates:
            return candidates

        exclude_tokens = ['time', 'date', 'timestamp', 'datetime', 'hour', 'minute', 'second', 'id', 'index', 'freq', 'frequency', 'hz', 'duration']
        numeric_cols = self.df.select_dtypes(include=[np.number]).columns.tolist()
        numeric_cols = [
            col for col in numeric_cols
            if not any(token in str(col).lower() for token in exclude_tokens)
        ]
        return numeric_cols

    def analyze(self):
        results = {
            'timestamp': datetime.now().isoformat(),
            'who_2018': who_2018_environmental_noise_guideline_levels(),
            'iso_analysis': self._iso_analysis(),
            'epa_analysis': self._epa_analysis(),
        }
        return results

    def _iso_analysis(self):

        return {
            'standard': 'ISO 1996 (methodology standard)',
            'note': (
                'ISO 1996 provides methods for measurement and assessment; it does not define universal legal dB limits. '
                'Configure jurisdiction-specific limits if you need regulatory compliance checking.'
            ),
            'compliance_status': {},
            'detailed_assessment': {}
        }

    def _epa_analysis(self):
        return {
            'standards': {},
            'epa_note': (
                'EPA environmental noise limits are not evaluated by default: EPA guidance '
                'varies by document and programme and is not applicable without an explicit, '
                'cited limit table, metric definition, averaging period and land-use context. '
                'Occupational limits (OSHA/NIOSH) are deliberately NOT applied to environmental '
                'measurements — they govern workplace exposure over a shift and are not '
                'comparable to a residential or community measurement.'
            ),
            'osha_pel_compliance': {},
            'detailed_assessment': {
                col: {
                    'measured_laeq': round(float(energetic_mean_db(self.df[col].dropna())), 2),
                    'measured_max': round(float(self.df[col].dropna().max()), 2),
                    'note': 'Descriptive only. No regulatory verdict is issued from this module.',
                }
                for col in self.noise_columns
                if not self.df[col].dropna().empty
                and energetic_mean_db(self.df[col].dropna()) is not None
            },
        }

    # NOTE: two health-effect ladders were removed here.
