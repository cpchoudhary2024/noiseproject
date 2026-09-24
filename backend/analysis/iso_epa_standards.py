# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
import math

import pandas as pd
import numpy as np
from datetime import datetime

from analysis.acoustics import energetic_mean_db
from analysis.acoustics import compute_ldn_lden
from analysis.standards_reference import who_2018_environmental_noise_guideline_levels, occupational_noise_standards

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
            'recommendations': self._generate_recommendations()
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
    # Health interpretation now comes solely from ``get_health_based_assessment``,
    # which is sourced to WHO 2018 and WHO 1999 with the metric each threshold is
    # defined on.

    def _generate_recommendations(self):
        """Generate recommendations based on analysis.

        Recommendations must be consistent with the guideline comparisons reported elsewhere.
        This function uses:
        - Derived WHO 2018 transport metrics (Lden/Lnight) when timestamps exist; otherwise
        - LAeq (energy-average) as a general screening indicator.

        Note: Without identifying the dominant noise source (road/rail/aircraft/etc.) or
        jurisdiction, this is guidance only.
        """
        recommendations = []

        # Attempt to compute derived environmental metrics once (timestamps required).
        from analysis.timestamp_utils import resolve_time_column, parse_timestamps_robust
        env_metrics_by_col: dict[str, dict[str, float]] = {}
        _tcol = resolve_time_column(self.df)
        if _tcol:
            ts, _ = parse_timestamps_robust(self.df[_tcol])
            if ts.notna().sum() > 0:
                for col in self.noise_columns:
                    y = pd.to_numeric(self.df[col], errors="coerce")
                    out = compute_ldn_lden(ts, y)
                    if out:
                        env_metrics_by_col[col] = out

        who = who_2018_environmental_noise_guideline_levels()
        who_guidelines = (who or {}).get("guidelines", {})
        road = who_guidelines.get("road_traffic", {})
        road_metrics = (road.get("metrics_db") or {})
        who_lden = float(road_metrics.get("Lden")) if road_metrics.get("Lden") is not None else None
        who_lnight = float(road_metrics.get("Lnight")) if road_metrics.get("Lnight") is not None else None
        
        for col in self.noise_columns:
            data = self.df[col].dropna()
            if data.empty:
                continue

            laeq = energetic_mean_db(data)
            leq = float(laeq) if laeq is not None else float(data.mean())

            env = env_metrics_by_col.get(col, {})
            current_lden = env.get("Lden")
            current_lnight = env.get("Lnight")

            # Prefer WHO-derived transport guidance when available.
            if (current_lden is not None or current_lnight is not None) and (who_lden is not None or who_lnight is not None):
                exceeds = []
                if who_lden is not None and current_lden is not None and float(current_lden) > who_lden:
                    exceeds.append(f"Lden exceeds WHO 2018 road-traffic reference ({who_lden:.0f} dB)")
                if who_lnight is not None and current_lnight is not None and float(current_lnight) > who_lnight:
                    exceeds.append(f"Lnight exceeds WHO 2018 road-traffic reference ({who_lnight:.0f} dB)")

                if exceeds:
                    recommendations.append(
                        f"{col}: Potential community impact — "
                        + "; ".join(exceeds)
                        + ". Consider mitigation and confirm applicability based on the dominant noise source and local regulations."
                    )
                else:
                    recommendations.append(
                        f"{col}: Derived Lden/Lnight are within WHO 2018 road-traffic reference values. "
                        "Confirm against local land-use limits and site-specific source context."
                    )
                continue

            # Fallback (no timestamps): cautious LAeq screening language
            if leq < 50:
                recommendations.append(f"{col}: Low overall sound levels based on LAeq (screening). Confirm against local limits if this is a regulated site.")
            elif leq < 55:
                recommendations.append(f"{col}: Moderate overall levels based on LAeq (screening). Sensitive receptors may still experience annoyance depending on time-of-day and events.")
            elif leq < 65:
                recommendations.append(f"{col}: Elevated levels based on LAeq (screening). Consider noise management for sensitive receptors and nighttime periods.")
            elif leq < 75:
                recommendations.append(f"{col}: High levels based on LAeq (screening). Implement mitigation and verify compliance with applicable local/jurisdictional requirements.")
            elif leq < 85:
                recommendations.append(f"{col}: Very high levels based on LAeq (screening). Strong engineering controls are recommended; evaluate occupational exposure controls if applicable.")
            else:
                recommendations.append(f"{col}: Extremely high levels based on LAeq (screening). Treat as potentially hazardous; implement immediate controls and evaluate occupational hearing protection needs if applicable.")
        
        return recommendations
    
    @staticmethod
    def get_standard_limits():
        """Get a reference table of standard limits"""
        limits = {
            'WHO 2018 (Environmental Noise Guidelines — Europe)': {
                'Road traffic: Lden': 53,
                'Road traffic: Lnight': 45,
                'Railway: Lden': 54,
                'Railway: Lnight': 44,
                'Aircraft: Lden': 45,
                'Aircraft: Lnight': 40,
                'Wind turbines: Lden (conditional)': 45,
                'Leisure: LAeq,24h (conditional)': 70,
                'Bedroom average: LAeq': 30,
                'Bedroom single event: LAmax': 45,
                'Living room / Classroom: LAeq': 35,
            },
            'WHO 2009 Night Noise Guidance': {
                'Outdoor annual night-noise target (Lnight)': 40,
                'Interim target, not a no-effect threshold (Lnight)': 55,
            },
            'OSHA (Occupational)': {
                'Permissible Exposure Limit (8h TWA)': 90,
                'Action Level': 85,
                'Peak Limit (dB(C))': 140,
            },
            'NIOSH Recommended (more protective)': {
                'Recommended Limit (8h TWA)': 85,
                'Exchange Rate': '3 dB (more protective than OSHA 5 dB)',
            }
        }
        return limits
    
    def get_health_based_assessment(self, environment='outdoor'):
        """One monitoring assessment reused by cards and reports; no clinical prediction."""
        from analysis.assessment import concern_level, assessment_note
        from analysis.compliance_matrix import primary_column
        from analysis.timestamp_utils import resolve_time_column, parse_timestamps_robust, assess_timestamp_integrity
        col = primary_column(self.noise_columns)
        values = pd.to_numeric(self.df[col], errors='coerce') if col else pd.Series(dtype=float)
        laeq = energetic_mean_db(values)
        env = {}
        tcol = resolve_time_column(self.df)
        if tcol and assess_timestamp_integrity(self.df[tcol]).to_dict().get('time_metrics_valid'):
            ts, _ = parse_timestamps_robust(self.df[tcol])
            env = compute_ldn_lden(ts, values) or {}
        lden, lnight = env.get('Lden'), env.get('Lnight')
        if environment == 'indoor':
            lden = lnight = None
        note = assessment_note(lden, lnight)
        if environment == 'indoor':
            note = 'Outdoor transport references do not apply to indoor measurements. See the conditional bedroom references in the comparison matrix.'
        level = concern_level(lden, lnight)
        primary = {'LAeq_overall': laeq, 'Lden': lden, 'Lnight': lnight,
                   'concern_level': level, 'health_impacts': []}
        recommendations = [note, 'Confirm instrument calibration, source and representative coverage before interpreting a reference comparison.']
        if level in ('MODERATE-HIGH', 'HIGH', 'SERIOUS'):
            recommendations.append('Investigate the noise sources and consider a professional acoustic assessment before choosing mitigation.')
        if laeq is not None and laeq >= 85:
            recommendations.append('High sound levels warrant hearing precautions. NIOSH recommends 85 dBA over an eight-hour workday with a 3 dB exchange rate; this environmental record does not establish personal occupational dose.')
        return dict(title='Monitoring-period reference assessment', primary_metric=col,
                    laeq=laeq, lden=lden, lnight=lnight, concern_level=level,
                    assessment_note=note, health_impacts=[], recommendations=recommendations,
                    assessment_by_metric={col: primary} if col else {})

    @staticmethod
    def _pick_primary_column(cols: list[str]) -> str | None:
        """Pick the primary energy-average (LEQ) column for the headline rollup."""
        if not cols:
            return None
        for c in cols:
            n = "".join(ch for ch in c.lower() if ch.isalnum())
            if "laeq" in n or "leq" in n:
                return c
        # Avoid headline-ing a max/min channel if a general level exists
        non_peak = [c for c in cols
                    if not any(k in c.lower() for k in ["max", "min", "peak"])]
        return non_peak[0] if non_peak else cols[0]

    def get_occupational_assessment(self, exposure_hours: float | None = None):
        """Assess occupational exposure against NIOSH and OSHA criteria.

        Occupational limits are **dose** criteria over a work shift, not levels.
        Comparing a raw LAeq against 85/90 dB — as this method previously did —
        ignores both the exposure duration and the exchange rate, and produced a
        "COMPLIANT" verdict for an 11-day residential record.

        Two quantities are computed instead:

        ``L_EX,8h`` (NIOSH / ISO 1999)
            The level which, sustained for 8 hours, delivers the same *energy* as
            the measured exposure: ``LAeq,T + 10*log10(T / 8h)``. This is the
            correct comparison for NIOSH's 85 dB REL, whose 3 dB exchange rate is
            energy-equivalent.

        ``OSHA dose``
            OSHA uses a 5 dB exchange rate, which is **not** energy-equivalent, so
            it must be accumulated per sample:
            ``D = 100 * Σ(t_i / T_i)`` with ``T_i = 8 / 2**((L_i - 90) / 5)``,
            counting only samples at or above the 80 dB(A) threshold, per
            29 CFR 1910.95 Appendix A. 100% dose corresponds to the PEL.

        Parameters
        ----------
        exposure_hours : float, optional
            Length of the work shift the measurement represents, in hours. When
            omitted, the record's own duration is used and the result is flagged,
            because a monitor left running for days does not represent a shift.

        Returns
        -------
        dict
            Assessment per column, including the applicability caveat.
        """
        occ_standards = occupational_noise_standards()
        assessment = {
            "title": "Occupational Noise Exposure Assessment",
            "standards": occ_standards,
            "applicability_warning": (
                "Occupational criteria (OSHA 29 CFR 1910.95, NIOSH REL) apply to WORKER "
                "exposure in a workplace over a work shift. They must not be applied to "
                "community, residential or environmental measurements, where WHO 2018 "
                "Lden/Lnight guidelines and local ordinances are the applicable criteria."
            ),
            "assessment_by_column": {},
        }

        # Duration the record actually represents.
        measured_hours = self._record_duration_hours()
        shift_hours = float(exposure_hours) if exposure_hours else measured_hours
        duration_assumed = exposure_hours is None

        for col in self.noise_columns:
            data = pd.to_numeric(self.df[col], errors='coerce').dropna()
            if data.empty:
                continue

            laeq = energetic_mean_db(data)
            if laeq is None:
                continue
            leq = float(laeq)

            col_assessment: dict = {
                "LAeq_measured": round(leq, 1),
                "measurement_duration_hours": (round(measured_hours, 2)
                                               if measured_hours else None),
                "shift_hours_used": round(shift_hours, 2) if shift_hours else None,
                "duration_assumed_from_record": duration_assumed,
            }

            # A record spanning days is not a work shift. Normalising 263 hours
            # of residential monitoring to an "8-hour equivalent" produces a
            # number that looks like an occupational exposure and is not one.
            if shift_hours and shift_hours > 24.0 and duration_assumed:
                col_assessment["niosh_status"] = {
                    "criterion": "NIOSH REL 85 dB(A) L_EX,8h",
                    "status": "NOT APPLICABLE",
                    "reason": (
                        f"This record spans {shift_hours:.1f} hours, which is not a work "
                        f"shift. Occupational criteria assess a worker's exposure over a "
                        f"shift; pass an explicit 'exposure_hours' for the shift this "
                        f"measurement represents, or use the environmental (WHO 2018) "
                        f"assessment instead."
                    ),
                }
                col_assessment["osha_status"] = dict(col_assessment["niosh_status"])
                col_assessment["osha_status"]["criterion"] = (
                    "OSHA PEL — 90 dB(A) 8-h TWA (29 CFR 1910.95)")
                assessment["assessment_by_column"][col] = col_assessment
                continue

            # ── NIOSH: 8-hour normalised exposure level ──────────────────────
            if shift_hours and shift_hours > 0:
                l_ex_8h = leq + 10.0 * math.log10(shift_hours / 8.0)
                col_assessment["L_EX_8h"] = round(l_ex_8h, 1)
                col_assessment["niosh_status"] = {
                    "criterion": "NIOSH REL 85 dB(A) L_EX,8h (3 dB exchange rate)",
                    "measured_L_EX_8h": round(l_ex_8h, 1),
                    "status": ("AT/ABOVE REL" if l_ex_8h >= 85.0 else "BELOW REL"),
                    "exceeded_by_db": (round(l_ex_8h - 85.0, 1)
                                       if l_ex_8h >= 85.0 else None),
                }
            else:
                col_assessment["niosh_status"] = {
                    "criterion": "NIOSH REL 85 dB(A) L_EX,8h",
                    "status": "NOT ASSESSABLE",
                    "reason": "Exposure duration unknown; an 8-hour normalisation requires it.",
                }

            # ── OSHA: 5 dB exchange rate dose ────────────────────────────────
            interval_s = self._sample_interval_seconds()
            if interval_s and interval_s > 0:
                lv = data.to_numpy(dtype=float)
                counted = lv[lv >= 80.0]           # 29 CFR 1910.95 App. A threshold
                if counted.size:
                    t_hours = interval_s / 3600.0
                    allowed = 8.0 / np.power(2.0, (counted - 90.0) / 5.0)
                    dose_pct = 100.0 * float(np.sum(t_hours / allowed))
                else:
                    dose_pct = 0.0
                # TWA equivalent of the accumulated dose. Below ~0.1% dose the
                # logarithm returns a physically meaningless figure (a 0.0% dose
                # yielded "21.7 dB"), so report it as not applicable instead.
                twa = (90.0 + 16.61 * math.log10(dose_pct / 100.0)) if dose_pct >= 0.1 else None
                col_assessment["osha_status"] = {
                    "criterion": "OSHA PEL — 90 dB(A) 8-h TWA, 5 dB exchange rate (29 CFR 1910.95)",
                    "dose_percent": round(dose_pct, 1),
                    "twa_equivalent_db": round(twa, 1) if twa is not None else None,
                    "status": ("EXCEEDS PEL" if dose_pct > 100.0 else
                               "AT/ABOVE ACTION LEVEL" if dose_pct >= 50.0 else
                               "BELOW ACTION LEVEL"),
                    "note": ("Dose accumulated only over samples at or above 80 dB(A), "
                             "per 29 CFR 1910.95 Appendix A. 100% dose = PEL; "
                             "50% dose = 85 dB(A) action level."),
                }
            else:
                col_assessment["osha_status"] = {
                    "criterion": "OSHA PEL — 90 dB(A) 8-h TWA",
                    "status": "NOT ASSESSABLE",
                    "reason": "Sample interval unknown; a dose cannot be accumulated without it.",
                }

            assessment["assessment_by_column"][col] = col_assessment

        return assessment

    def _record_duration_hours(self) -> float | None:
        """Span of the record in hours, or None when there is no usable timeline."""
        from analysis.timestamp_utils import resolve_time_column, parse_timestamps_robust
        col = resolve_time_column(self.df)
        if not col:
            return None
        ts, _ = parse_timestamps_robust(self.df[col])
        ts = ts.dropna()
        if len(ts) < 2:
            return None
        return float((ts.max() - ts.min()).total_seconds() / 3600.0)

    def _sample_interval_seconds(self) -> float | None:
        """Modal spacing between samples, in seconds."""
        from analysis.timestamp_utils import resolve_time_column, parse_timestamps_robust
        col = resolve_time_column(self.df)
        if not col:
            return None
        ts, _ = parse_timestamps_robust(self.df[col])
        ts = ts.dropna().sort_values()
        if len(ts) < 2:
            return None
        d = ts.diff().dt.total_seconds().dropna()
        d = d[d > 0]
        if d.empty:
            return None
        mode = d.mode()
        return float(mode.iloc[0]) if not mode.empty else float(d.median())

