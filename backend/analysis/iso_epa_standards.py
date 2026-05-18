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
        """Analyze against OSHA occupational limits and provide EPA context.

        EPA environmental noise guidance varies by document/program and is not
        universally applicable without context (metric definition, averaging
        period, land use, jurisdiction). This analyzer focuses on OSHA PEL/action
        levels which are explicit occupational exposure limits.
        """
        epa_results = {
            'standards': {
                'OSHA_PEL': 'OSHA Permissible Exposure Limits'
            },
            'epa_note': (
                'EPA environmental noise limits are not evaluated by default in this platform without an explicit, cited limit table and metric definition. '
                'OSHA occupational limits are evaluated below.'
            ),
            'osha_pel_compliance': {},
            'detailed_assessment': {}
        }
        
        for col in self.noise_columns:
            data = self.df[col].dropna()
            if data.empty:
                continue

            laeq = energetic_mean_db(data)
            leq = float(laeq) if laeq is not None else float(data.mean())
            max_level = data.max()
            
            # OSHA PEL (Occupational Safety and Health Administration)
            # 8-hour TWA = 90 dB(A) Action level = 85 dB(A)
            osha_results = {
                'permissible_exposure_limit_8h_twa': 90,
                'action_level': 85,
                'measured_level': round(leq, 2),
                'osha_compliance': 'COMPLIANT' if leq <= 90 else 'NON-COMPLIANT',
                'action_level_status': 'BELOW' if leq < 85 else 'AT/ABOVE ACTION LEVEL',
                'ppe_required': True if leq >= 85 else False
            }
            
            epa_results['osha_pel_compliance'][col] = osha_results
            
            epa_results['detailed_assessment'][col] = {
                'measured_leq': round(leq, 2),
                'measured_max': round(max_level, 2),
                'health_effects_epa': self._get_epa_health_effects(leq),
                'regulatory_framework': 'OSHA regulates occupational noise exposure; environmental noise limits are typically local/regional regulations and guidelines.'
            }
        
        return epa_results
    
    def _get_iso_health_implications(self, leq):
        """Get health implications based on ISO 1996"""
        if leq < 40:
            return "No significant health effects expected"
        elif leq < 50:
            return "Minor annoyance, possible sleep disturbance"
        elif leq < 60:
            return "Moderate annoyance, possible sleep disturbance"
        elif leq < 70:
            return "High annoyance, probable speech interference"
        elif leq < 80:
            return "Severe annoyance, significant speech interference, potential hearing damage with prolonged exposure"
        else:
            return "DANGEROUS - Risk of hearing damage, immediate mitigation required"
    
    def _get_epa_health_effects(self, leq):
        """Get health effects based on EPA guidelines"""
        if leq < 50:
            return "No significant risk"
        elif leq < 60:
            return "Minor interference with speech communication"
        elif leq < 70:
            return "Speech interference indoors, sleep disturbance possible"
        elif leq < 80:
            return "Hearing conservation program recommended in occupational settings"
        elif leq < 90:
            return "Hearing conservation program required; potential for hearing damage"
        else:
            return "CRITICAL - Immediate risk of hearing damage; protective equipment mandatory"
    
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
        env_metrics_by_col: dict[str, dict[str, float]] = {}
        time_cols = [
            c for c in self.df.columns
            if any(term in c.lower() for term in ["timestamp", "datetime", "time", "date"])
        ]
        if time_cols:
            ts = pd.to_datetime(self.df[time_cols[0]], errors="coerce", dayfirst=True, cache=True)
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
            'WHO Health Thresholds': {
                'Sleep LOAEL (Lnight)': 40,
                'Cardiovascular risk threshold (Lden)': 55,
                'Annoyance begins (Lden)': 50,
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
    
    def get_health_based_assessment(self):
        """Provide health-based noise assessment using WHO thresholds.
        
        This assessment identifies potential health impacts based on:
        - WHO 2018 Environmental Noise Guidelines
        - Sleep disturbance thresholds
        - Cardiovascular risk indicators
        - Child learning/cognitive impacts
        """
        who = who_2018_environmental_noise_guideline_levels()
        health_thresholds = who.get("health_thresholds", {})
        
        assessment = {
            "title": "Health-Based Noise Impact Assessment",
            "source": "WHO 2018 Environmental Noise Guidelines",
            "assessment_by_metric": {},
        }
        
        # Attempt to compute environmental metrics if timestamps exist
        time_cols = [
            c for c in self.df.columns
            if any(term in c.lower() for term in ["timestamp", "datetime", "time", "date"])
        ]
        
        has_time_data = False
        env_metrics_by_col = {}
        
        if time_cols:
            ts = pd.to_datetime(self.df[time_cols[0]], errors="coerce", dayfirst=True, cache=True)
            if ts.notna().sum() > 0:
                has_time_data = True
                for col in self.noise_columns:
                    y = pd.to_numeric(self.df[col], errors="coerce")
                    out = compute_ldn_lden(ts, y)
                    if out:
                        env_metrics_by_col[col] = out
        
        for col in self.noise_columns:
            data = self.df[col].dropna()
            if data.empty:
                continue
            
            laeq = energetic_mean_db(data)
            leq = float(laeq) if laeq is not None else float(data.mean())
            max_level = data.max()
            
            col_assessment = {
                "LAeq_overall": round(leq, 1),
                "LAmax": round(max_level, 1),
                "health_impacts": [],
                "concern_level": "Low",
            }
            
            # Environmental metrics if available
            if col in env_metrics_by_col:
                env = env_metrics_by_col[col]
                if "Lden" in env:
                    col_assessment["Lden"] = round(env["Lden"], 1)
                    # Check Lden against cardiovascular threshold
                    if env["Lden"] > 55:
                        col_assessment["health_impacts"].append({
                            "threshold": "Cardiovascular risk (55 dB Lden)",
                            "current_level": round(env["Lden"], 1),
                            "excess": round(env["Lden"] - 55, 1),
                            "effect": f"Cardiovascular disease risk increases ~8% per 10 dB above threshold"
                        })
                        col_assessment["concern_level"] = "Moderate to High"
                    elif env["Lden"] > 50:
                        col_assessment["health_impacts"].append({
                            "threshold": "Annoyance onset (50 dB Lden)",
                            "current_level": round(env["Lden"], 1),
                            "effect": "Noticeable annoyance; 6-12% of population highly annoyed"
                        })
                        col_assessment["concern_level"] = "Moderate"
                
                if "Lnight" in env:
                    col_assessment["Lnight"] = round(env["Lnight"], 1)
                    # Check Lnight against sleep thresholds
                    if env["Lnight"] > 45:
                        col_assessment["health_impacts"].append({
                            "threshold": "Sleep quality decline (45 dB Lnight)",
                            "current_level": round(env["Lnight"], 1),
                            "excess": round(env["Lnight"] - 45, 1),
                            "effect": "Self-reported sleep quality begins to decline; increased awakenings likely"
                        })
                        if col_assessment["concern_level"] == "Low":
                            col_assessment["concern_level"] = "Moderate"
                    elif env["Lnight"] > 40:
                        col_assessment["health_impacts"].append({
                            "threshold": "Sleep LOAEL (40 dB Lnight)",
                            "current_level": round(env["Lnight"], 1),
                            "effect": "Increased body movements during sleep; subtle sleep architecture changes"
                        })
            else:
                # Fallback to LAeq screening
                if leq > 85:
                    col_assessment["health_impacts"].append({
                        "category": "Hearing damage risk (occupational)",
                        "current_level": round(leq, 1),
                        "effect": "Immediate permanent hearing damage risk; hearing protection mandatory"
                    })
                    col_assessment["concern_level"] = "Critical"
                elif leq > 75:
                    col_assessment["health_impacts"].append({
                        "category": "Potential hearing damage",
                        "current_level": round(leq, 1),
                        "effect": "Long-term hearing damage risk; speech interference"
                    })
                    col_assessment["concern_level"] = "High"
                elif leq > 65:
                    col_assessment["health_impacts"].append({
                        "category": "Significant annoyance & speech interference",
                        "current_level": round(leq, 1),
                        "effect": "Annoyance, sleep disturbance possible, speech unclear"
                    })
                    col_assessment["concern_level"] = "Moderate"
                elif leq > 55:
                    col_assessment["health_impacts"].append({
                        "category": "Noticeable annoyance",
                        "current_level": round(leq, 1),
                        "effect": "Annoyance possible, conversation interference"
                    })
                    col_assessment["concern_level"] = "Low-Moderate"
            
            assessment["assessment_by_metric"][col] = col_assessment
        
        return assessment
    
    def get_occupational_assessment(self):
        """Assess occupational exposure against NIOSH and OSHA standards."""
        occ_standards = occupational_noise_standards()
        assessment = {
            "title": "Occupational Noise Exposure Assessment",
            "standards": occ_standards,
            "assessment_by_column": {},
        }
        
        for col in self.noise_columns:
            data = self.df[col].dropna()
            if data.empty:
                continue
            
            laeq = energetic_mean_db(data)
            leq = float(laeq) if laeq is not None else float(data.mean())
            
            col_assessment = {
                "LAeq_measured": round(leq, 1),
                "osha_status": {},
                "niosh_status": {},
                "recommendations": [],
            }
            
            # OSHA Assessment (90 dB PEL, 85 dB action level)
            if leq >= 90:
                col_assessment["osha_status"] = {
                    "compliance": "NON-COMPLIANT",
                    "level_comparison": "At or exceeds OSHA PEL (90 dB)",
                    "required_actions": ["Hearing protection mandatory", "Engineering controls required", "Medical surveillance required"]
                }
            elif leq >= 85:
                col_assessment["osha_status"] = {
                    "compliance": "ABOVE ACTION LEVEL",
                    "level_comparison": "Exceeds OSHA action level (85 dB)",
                    "required_actions": ["Hearing conservation program required", "Baseline and annual audiograms", "Hearing protection provided"]
                }
            else:
                col_assessment["osha_status"] = {
                    "compliance": "COMPLIANT",
                    "level_comparison": f"Below OSHA action level (85 dB). Current: {leq:.1f} dB"
                }
            
            # NIOSH Assessment (85 dB recommended with 3 dB exchange rate)
            if leq >= 85:
                col_assessment["niosh_status"] = {
                    "compliance": "AT/EXCEEDS NIOSH RECOMMENDED LIMIT",
                    "level_comparison": "At or exceeds NIOSH recommended limit (85 dB)",
                    "exchange_rate": "3 dB (more protective than OSHA 5 dB)",
                    "note": "NIOSH recommends maximum 85 dB; hearing damage risk increases above this"
                }
            else:
                col_assessment["niosh_status"] = {
                    "compliance": "WITHIN NIOSH LIMIT",
                    "level_comparison": f"Below NIOSH recommended limit (85 dB). Current: {leq:.1f} dB"
                }
            
            assessment["assessment_by_column"][col] = col_assessment
        
        return assessment

