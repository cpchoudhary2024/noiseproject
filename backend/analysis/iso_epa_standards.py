# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
import math

import pandas as pd
import numpy as np
from datetime import datetime

from analysis.acoustics import energetic_mean_db
from analysis.acoustics import compute_ldn_lden
from analysis.standards_reference import who_2018_environmental_noise_guideline_levels

class StandardsAnalyzer:
    """
    Analyze noise data against ISO and EPA standards:
    
    ISO Standards:
    - ISO 1996: Environmental noise
    - ISO 3744: Acoustics measurement of sound pressure level
    - ISO 3746: Sound pressure levels from construction equipment
    
    EPA Standards:
    - EPA Noise Abatement Criteria
    - OSHA Permissible Exposure Limits
    """
    
    def __init__(self, df):
        self.df = df.copy()
        self.noise_columns = self._identify_noise_columns()
    
    def _identify_noise_columns(self):
        """Identify noise measurement columns.

        Must be robust to derived/categorical fields (e.g. 'noise_simple')
        that include the word 'noise' but are not numeric measurement series.
        """

        def _looks_like_noise_measurement_name(col_lower: str) -> bool:
            if any(token in col_lower for token in ['db', 'dba', 'decibel', 'spl', 'leq', 'laeq', 'lmax', 'lmin', 'l-max', 'l-min', 'l_eq', 'lp']):
                return True
            if 'noise' in col_lower and 'level' in col_lower:
                return True
            if 'sound' in col_lower and 'level' in col_lower:
                return True
            return False

        def _is_probably_numeric(series: pd.Series) -> bool:
            if pd.api.types.is_numeric_dtype(series):
                return True
            coerced = pd.to_numeric(series, errors='coerce')
            if len(coerced) == 0:
                return False
            return float(coerced.notna().mean()) >= 0.80

        candidates: list[str] = []
        for col in self.df.columns:
            col_lower = str(col).lower()
            if not _looks_like_noise_measurement_name(col_lower):
                continue
            if _is_probably_numeric(self.df[col]):
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
        """Perform ISO and EPA standards analysis"""
        results = {
            'timestamp': datetime.now().isoformat(),
            'who_2018': who_2018_environmental_noise_guideline_levels(),
            'iso_analysis': self._iso_analysis(),
            'epa_analysis': self._epa_analysis(),
        }
        return results
    
    def _iso_analysis(self):
        """ISO 1996 reference.

        ISO 1996 is primarily a *methodology* standard for describing, measuring,
        and assessing environmental noise. It does not prescribe universal legal
        exposure limits; those are typically set by national/local regulations.

        To avoid presenting non-authoritative values as "ISO limits", this
        platform no longer returns ISO "compliance" without a jurisdiction-
        specific limit table.
        """

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
        """Environmental context only — no occupational verdict.

        This previously issued OSHA PEL "COMPLIANT"/"NON-COMPLIANT" verdicts, and
        a ``ppe_required`` flag, for environmental recordings. That was wrong three
        times over:

        1. **Wrong population.** The OSHA PEL governs worker exposure inside a
           workplace over a shift. It says nothing about a residence, and telling
           a resident that hearing protection is "required" in their own home is
           both meaningless and alarming.
        2. **Wrong metric.** The PEL is an 8-hour time-weighted average computed
           with a 5 dB exchange rate (a dose), not an energy-average LAeq.
        3. **Wrong averaging period.** The LAeq here spans the whole record —
           often 7 to 11 days — which is not comparable to an 8-hour limit
           without normalisation.

        Occupational exposure is assessed properly, and only on request, by
        :meth:`get_occupational_assessment`, which computes a normalised
        8-hour exposure level and an OSHA dose.
        """
        return {
            'standards': {},
            'epa_note': (
                'EPA environmental noise limits are not evaluated by default: EPA guidance '
                'varies by document and programme and is not applicable without an explicit, '
                'cited limit table, metric definition, averaging period and land-use context. '
                'Occupational limits (OSHA/NIOSH) are deliberately NOT applied to environmental '
                'measurements — they govern workplace exposure over a shift and are not '
                'comparable to a residential or community measurement. Use the dedicated '
                'occupational assessment endpoint for workplace data.'
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
    #
    # ``_get_iso_health_implications`` attributed a scale of health outcomes to
    # ISO 1996, which defines measurement and assessment METHODS and sets no
    # health thresholds at all — this module says exactly that in
    # ``_iso_analysis``, so the two contradicted each other.
    #
    # ``_get_epa_health_effects`` attributed thresholds to "EPA guidelines"
    # without a citable document, and mixed occupational hearing-damage language
    # ("protective equipment mandatory") into environmental assessment.
    #
    # This module produces no health interpretation. Guideline comparisons come
    # from analysis.compliance_matrix, each on the metric its source defines.
