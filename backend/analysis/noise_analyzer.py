import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime
import json

from analysis.acoustics import compute_ldn_lden, energetic_mean_db, exceedance_levels_db
from analysis.standards_reference import who_2018_environmental_noise_guideline_levels

class NoiseAnalyzer:
    """
    Comprehensive noise data analysis covering:
    - Basic statistics (mean, median, std dev, etc.)
    - Time-series analysis
    - Frequency analysis
    - Trend analysis
    - Percentile analysis (L5, L10, L50, L90, L95)
    - Peak analysis
    - Compliance checking
    """
    
    def __init__(self, df):
        self.df = df.copy()
        self.noise_columns = self._identify_noise_columns()
        self.validate_data()
        self._ts_cache = None       # memoized parsed primary timestamp series
        self._ts_cache_col = None

    def _parsed_timestamps(self, time_col: str | None = None) -> pd.Series:
        """Parse the primary timestamp column once and reuse it.

        Parsing a multi-million-row column is expensive; several analysis methods
        need it, so we memoize the result on the instance.
        """
        if time_col is None:
            cands = [c for c in self.df.columns
                     if any(t in c.lower() for t in ['timestamp', 'datetime', 'time', 'date'])]
            time_col = cands[0] if cands else None
        if time_col is None:
            return pd.Series(dtype='datetime64[ns]')
        if self._ts_cache is not None and self._ts_cache_col == time_col:
            return self._ts_cache
        try:
            from analysis.timestamp_utils import parse_timestamps_robust
            parsed, _ = parse_timestamps_robust(self.df[time_col])
        except Exception:
            parsed = pd.to_datetime(self.df[time_col], errors='coerce', dayfirst=True, cache=True)
        self._ts_cache = parsed
        self._ts_cache_col = time_col
        return parsed
    
    def _identify_noise_columns(self):
        """Identify columns containing noise measurements (dB values).

        This must be resilient to datasets that contain derived or categorical fields
        (e.g. 'noise_simple') that are not numeric measurement series.
        """

        def _looks_like_noise_measurement_name(col_lower: str) -> bool:
            # Prefer explicit acoustics/dB indicators.
            if any(token in col_lower for token in ['db', 'dba', 'decibel', 'spl', 'leq', 'laeq', 'lmax', 'lmin', 'l-max', 'l-min', 'l_eq']):
                return True

            # Allow 'noise level' style headers.
            if 'noise' in col_lower and 'level' in col_lower:
                return True

            return False

        def _is_probably_numeric(series: pd.Series) -> bool:
            # Accept numeric dtypes.
            if pd.api.types.is_numeric_dtype(series):
                return True

            # Otherwise, attempt coercion and require a high non-null ratio.
            coerced = pd.to_numeric(series, errors='coerce')
            if len(coerced) == 0:
                return False
            non_null_ratio = float(coerced.notna().mean())
            return non_null_ratio >= 0.80

        # First pass: name-based candidates that are numeric (or mostly numeric).
        candidates = []
        for col in self.df.columns:
            col_lower = str(col).lower()
            if not _looks_like_noise_measurement_name(col_lower):
                continue
            if _is_probably_numeric(self.df[col]):
                candidates.append(col)

        if candidates:
            return candidates

        # Fallback: numeric columns excluding obvious non-noise fields.
        exclude_tokens = ['time', 'date', 'timestamp', 'datetime', 'hour', 'minute', 'second', 'id', 'index', 'freq', 'frequency', 'hz', 'duration']
        numeric_cols = self.df.select_dtypes(include=[np.number]).columns.tolist()
        numeric_cols = [
            col for col in numeric_cols
            if not any(token in str(col).lower() for token in exclude_tokens)
        ]

        return numeric_cols
    
    def validate_data(self):
        """Validate the input data"""
        if self.df.empty:
            raise ValueError("DataFrame is empty")
        
        if not self.noise_columns:
            raise ValueError("No noise measurement columns found. Please ensure your data contains noise level columns (dB values)")
        
        # Convert to numeric
        for col in self.noise_columns:
            self.df[col] = pd.to_numeric(self.df[col], errors='coerce')
        
        # Remove rows with NaN values
        initial_rows = len(self.df)
        self.df = self.df.dropna(subset=self.noise_columns)
        if len(self.df) < initial_rows:
            print(f"Removed {initial_rows - len(self.df)} rows with missing values")

        # It's possible that a dataset contains candidate columns that are non-numeric
        # or fully missing after coercion; don't proceed with an empty cleaned frame.
        if self.df.empty:
            raise ValueError(
                "No valid noise measurements found after cleaning. "
                "Please verify the noise columns contain numeric dB values (e.g., LEQ/LAeq, L-Max, L-Min)."
            )
    
    def comprehensive_analysis(self):
        """Perform comprehensive noise analysis"""
        results = {
            'timestamp': datetime.now().isoformat(),
            'data_summary': self._get_data_summary(),
            'statistics': self._calculate_statistics(),
            'percentiles': self._calculate_percentiles(),
            'environmental_metrics': self._calculate_environmental_metrics(),
            'peaks': self._analyze_peaks(),
            'time_analysis': self._time_series_analysis(),
            'frequency_distribution': self._frequency_distribution(),
            'trends': self._analyze_trends(),
            'compliance': self._check_compliance(),
            'interpretations': self._generate_interpretations()
        }
        return results
    
    def _get_data_summary(self):
        """Get basic data summary"""
        summary = {
            'total_records': len(self.df),
            'noise_columns': self.noise_columns,
            'measurement_period': self._get_measurement_period(),
            'data_quality': self._assess_data_quality()
        }
        return summary
    
    def _get_measurement_period(self):
        """Try to identify the measurement period"""
        # Look for time/date columns
        time_cols = [col for col in self.df.columns if any(term in col.lower() for term in ['time', 'date', 'timestamp', 'datetime'])]
        
        if time_cols:
            time_col = time_cols[0]
            try:
                ts = self._parsed_timestamps(time_col)
                ts = ts.dropna()
                if not ts.empty:
                    return {
                        'start': ts.min().isoformat(),
                        'end': ts.max().isoformat()
                    }
            except Exception:
                # Fall back to raw string values if parsing fails.
                try:
                    return {
                        'start': str(self.df[time_col].iloc[0]),
                        'end': str(self.df[time_col].iloc[-1])
                    }
                except Exception:
                    pass
        
        return {'start': 'Unknown', 'end': 'Unknown'}
    
    def _assess_data_quality(self):
        """Assess quality of the data"""
        if len(self.df) == 0:
            return {}
        quality_metrics = {}
        for col in self.noise_columns:
            missing_pct = (self.df[col].isna().sum() / len(self.df)) * 100
            outliers = self._detect_outliers(self.df[col])
            quality_metrics[col] = {
                'missing_percent': round(missing_pct, 2),
                'outliers_detected': len(outliers),
                'outlier_percent': round((len(outliers) / len(self.df)) * 100, 2)
            }
        return quality_metrics
    
    def _calculate_statistics(self):
        """Calculate comprehensive statistics for each noise column"""
        stats_dict = {}
        
        for col in self.noise_columns:
            data = self.df[col].dropna()

            laeq = energetic_mean_db(data)
            laeq_val = round(float(laeq), 2) if laeq is not None else None
            mean_arithmetic = float(data.mean()) if len(data) else float('nan')
            
            stats_dict[col] = {
                # For dB-series, the physically-meaningful average is energy-averaged LAeq.
                # Kept under 'mean' for backwards compatibility with the UI.
                'mean': laeq_val if laeq_val is not None else round(float(mean_arithmetic), 2),
                'mean_arithmetic_db': round(float(mean_arithmetic), 2),
                'laeq_db': laeq_val,
                'median': round(float(data.median()), 2),
                'std_dev': round(float(data.std()), 2),
                'min': round(float(data.min()), 2),
                'max': round(float(data.max()), 2),
                'range': round(float(data.max() - data.min()), 2),
                'variance': round(float(data.var()), 2),
                'skewness': round(float(stats.skew(data)), 2),
                'kurtosis': round(float(stats.kurtosis(data)), 2),
                'cv': round(float((data.std() / data.mean()) * 100), 2),  # Coefficient of variation
                'interquartile_range': round(float(data.quantile(0.75) - data.quantile(0.25)), 2)
            }
        
        return stats_dict
    
    def _calculate_percentiles(self):
        """Calculate noise level percentiles (L5, L10, L50, L90, L95)"""
        percentiles_dict = {}
        
        for col in self.noise_columns:
            data = self.df[col].dropna().values
            exc = exceedance_levels_db(data)
            if not exc:
                percentiles_dict[col] = {}
                continue

            percentiles_dict[col] = {k: round(float(v), 2) for k, v in exc.items()}
        
        return percentiles_dict

    def _calculate_environmental_metrics(self):
        """Compute standard environmental noise metrics (when timestamps exist).

        Adds LAeq_day/LAeq_night and penalty-based Ldn/Lden.
        """
        # Find a usable timestamp column.
        time_cols = [
            col for col in self.df.columns
            if any(term in col.lower() for term in ['timestamp', 'datetime', 'time', 'date'])
        ]
        if not time_cols:
            return {}

        time_col = time_cols[0]
        ts = self._parsed_timestamps(time_col)
        if ts.notna().sum() == 0:
            return {}

        def _is_lmax(col_name: str) -> bool:
            l = (col_name or "").lower()
            return ("lmax" in l) or ("l-max" in l) or ("max" in l and "leq" not in l and "laeq" not in l)

        def _is_lmin(col_name: str) -> bool:
            l = (col_name or "").lower()
            return ("lmin" in l) or ("l-min" in l) or ("min" in l and "leq" not in l and "laeq" not in l)

        def _leq_columns() -> list[str]:
            # Prefer explicit LEQ/LAeq columns.
            leq_like = [
                c for c in self.noise_columns
                if ("leq" in c.lower() or "laeq" in c.lower() or "l_eq" in c.lower())
                and not _is_lmax(c)
                and not _is_lmin(c)
            ]
            if leq_like:
                return leq_like

            # Otherwise, use "non-peak" columns as a best-effort LEQ stream.
            non_peak = [c for c in self.noise_columns if not _is_lmax(c) and not _is_lmin(c)]
            if non_peak:
                return non_peak

            # Last resort: single-column datasets.
            return self.noise_columns[:] if len(self.noise_columns) == 1 else []

        metrics: dict[str, dict[str, float]] = {}
        for col in _leq_columns():
            y = pd.to_numeric(self.df[col], errors='coerce')
            out = compute_ldn_lden(ts, y)
            if out:
                metrics[col] = {k: round(float(v), 2) for k, v in out.items()}

        return metrics
    
    def _analyze_peaks(self):
        """Analyze peak noise levels"""
        peaks_dict = {}
        
        for col in self.noise_columns:
            data = self.df[col].dropna()
            
            # Find peaks using various methods
            from scipy.signal import find_peaks
            peaks, properties = find_peaks(data.values, prominence=data.std())
            
            peaks_dict[col] = {
                'number_of_peaks': int(len(peaks)),
                'peak_values': [round(float(data.iloc[p]), 2) for p in peaks[-5:]],  # Top 5 peaks
                'average_peak': round(float(data[data > data.quantile(0.9)].mean()), 2),
                'max_peak': round(float(data.max()), 2),
                'peak_frequency': round(float(len(peaks) / len(data)) * 1000, 2)  # Peaks per 1000 measurements
            }
        
        return peaks_dict
    
    def _time_series_analysis(self):
        """Analyze temporal patterns if time data exists"""
        analysis = {}
        
        time_cols = [col for col in self.df.columns if any(term in col.lower() for term in ['time', 'date', 'timestamp', 'datetime'])]
        
        if not time_cols or not self.noise_columns:
            return {'status': 'No time data available'}
        
        try:
            time_col = time_cols[0]
            self.df[time_col] = pd.to_datetime(self.df[time_col], errors='coerce')
            
            for noise_col in self.noise_columns:
                # Calculate hourly averages if possible
                test_df = self.df[[time_col, noise_col]].dropna()
                test_df = test_df.set_index(time_col)
                
                hourly = test_df.resample('h').agg({noise_col: ['mean', 'std', 'min', 'max', 'count']})
                
                if not hourly.empty:
                    analysis[noise_col] = {
                        'hourly_pattern': 'Available',
                        'peak_hour': 'See charts',
                        'quiet_hour': 'See charts'
                    }
        except:
            pass
        
        return analysis if analysis else {'status': 'Time series analysis not available'}
    
    def _frequency_distribution(self):
        """Analyze distribution of noise levels"""
        dist_dict = {}
        
        for col in self.noise_columns:
            data = self.df[col].dropna()
            
            # Create bins
            bins = np.linspace(data.min(), data.max(), 11)
            hist, bin_edges = np.histogram(data, bins=bins)
            
            dist_dict[col] = {
                'bins': [round(float(x), 1) for x in bin_edges],
                'frequencies': [int(x) for x in hist],
                'distribution_type': self._identify_distribution(data)
            }
        
        return dist_dict
    
    def _identify_distribution(self, data):
        """Identify the type of distribution"""
        skewness = stats.skew(data)
        kurtosis = stats.kurtosis(data)
        
        if abs(skewness) < 0.5 and abs(kurtosis) < 3:
            return "Approximately Normal"
        elif skewness > 0.5:
            return "Right-skewed"
        elif skewness < -0.5:
            return "Left-skewed"
        else:
            return "Non-normal"
    
    def _analyze_trends(self):
        """Analyze trends in the data"""
        trends = {}
        
        for col in self.noise_columns:
            data = self.df[col].dropna().reset_index(drop=True)
            
            if len(data) > 10:
                # Simple linear regression
                x = np.arange(len(data))
                z = np.polyfit(x, data.values, 1)
                p = np.poly1d(z)
                
                # Calculate trend strength
                predicted = p(x)
                ss_res = np.sum((data.values - predicted) ** 2)
                ss_tot = np.sum((data.values - data.mean()) ** 2)
                r_squared = 1 - (ss_res / ss_tot) if ss_tot > 0 else 0
                
                trend = "Increasing" if z[0] > 0.001 else "Decreasing" if z[0] < -0.001 else "Stable"
                
                trends[col] = {
                    'trend': trend,
                    'slope': round(float(z[0]), 4),
                    'r_squared': round(float(r_squared), 4),
                    'trend_strength': 'Strong' if abs(r_squared) > 0.7 else 'Moderate' if abs(r_squared) > 0.4 else 'Weak'
                }
            else:
                trends[col] = {'status': 'Insufficient data for trend analysis'}
        
        return trends
    
    def _detect_outliers(self, data):
        """Detect outliers using IQR method"""
        Q1 = data.quantile(0.25)
        Q3 = data.quantile(0.75)
        IQR = Q3 - Q1
        outliers = data[(data < (Q1 - 1.5 * IQR)) | (data > (Q3 + 1.5 * IQR))]
        return outliers
    
    def _check_compliance(self):
        """Assess measurements against published guideline levels.

        Notes:
        - Many "standards" are methods (e.g., ISO 1996) or are jurisdiction-specific.
        - WHO 2018 provides health-based guideline levels for long-term exposure.
        - Transport guidelines are expressed in Lden/Lnight; leisure is LAeq,24h.
        """

        def _status(limit_db: float, value_db: float | None) -> str:
            if value_db is None:
                return "N/A"
            return "PASS" if value_db <= limit_db else "FAIL"

        def _result(limit_db: float, value_db: float | None, *, metric: str, strength: str) -> dict:
            exceeded = None
            if value_db is not None:
                exceeded = max(0.0, float(value_db) - float(limit_db))
            return {
                "status": _status(limit_db, value_db),
                "limit_db": float(limit_db),
                "value_db": round(float(value_db), 2) if value_db is not None else None,
                "exceeded_by_db": round(float(exceeded), 2) if exceeded is not None else None,
                "metric": metric,
                "recommendation_strength": strength,
            }

        def _is_lmax(col_name: str) -> bool:
            l = (col_name or "").lower()
            return ("lmax" in l) or ("l-max" in l) or ("max" in l and "leq" not in l and "laeq" not in l)

        def _is_lmin(col_name: str) -> bool:
            l = (col_name or "").lower()
            return ("lmin" in l) or ("l-min" in l) or ("min" in l and "leq" not in l and "laeq" not in l)

        def _leq_columns() -> list[str]:
            leq_like = [
                c for c in self.noise_columns
                if ("leq" in c.lower() or "laeq" in c.lower() or "l_eq" in c.lower())
                and not _is_lmax(c)
                and not _is_lmin(c)
            ]
            if leq_like:
                return leq_like
            non_peak = [c for c in self.noise_columns if not _is_lmax(c) and not _is_lmin(c)]
            if non_peak:
                return non_peak
            return self.noise_columns[:] if len(self.noise_columns) == 1 else []

        leq_cols = set(_leq_columns())

        # Attempt to compute environmental metrics once (timestamps required).
        env_metrics_by_col: dict[str, dict[str, float]] = {}
        time_cols = [
            c for c in self.df.columns
            if any(term in c.lower() for term in ["timestamp", "datetime", "time", "date"])
        ]
        if time_cols:
            time_col = time_cols[0]
            ts = self._parsed_timestamps(time_col)
            if ts.notna().sum() > 0:
                # Only compute Lden/Lnight metrics for LEQ-like columns.
                for col in leq_cols:
                    y = pd.to_numeric(self.df[col], errors="coerce")
                    out = compute_ldn_lden(ts, y)
                    if out:
                        env_metrics_by_col[col] = out

        who = who_2018_environmental_noise_guideline_levels()
        g = who.get("guidelines", {})

        compliance: dict[str, dict] = {}
        for col in self.noise_columns:
            data = self.df[col].dropna()
            laeq = energetic_mean_db(data)
            current_leq = float(laeq) if laeq is not None else float(data.mean())

            env = env_metrics_by_col.get(col, {})
            current_lden = env.get("Lden")
            current_lnight = env.get("Lnight")
            current_laeq_24h = env.get("LAeq_24h")

            out: dict[str, object] = {
                "current_leq": round(current_leq, 2),
                "current_Lden": round(float(current_lden), 2) if current_lden is not None else "N/A",
                "current_Lnight": round(float(current_lnight), 2) if current_lnight is not None else "N/A",
                "current_LAeq_24h": round(float(current_laeq_24h), 2) if current_laeq_24h is not None else "N/A",
            }

            # WHO 2018 (transport): compare only for LEQ-like columns.
            if col in leq_cols:
                # Without identifying the dominant source (road/rail/aircraft/wind),
                # only apply a conservative, general transport reference comparison.
                for key, label in [
                    ("road_traffic", "WHO 2018 Transport (road traffic reference)"),
                ]:
                    entry = g.get(key, {})
                    metrics_db = (entry.get("metrics_db") or {})
                    strength = str(entry.get("recommendation_strength") or "")

                    if "Lden" in metrics_db:
                        out[f"{label} — Lden"] = _result(
                            float(metrics_db["Lden"]),
                            float(current_lden) if current_lden is not None else None,
                            metric="Lden",
                            strength=strength,
                        )

                    if "Lnight" in metrics_db:
                        out[f"{label} — Lnight"] = _result(
                            float(metrics_db["Lnight"]),
                            float(current_lnight) if current_lnight is not None else None,
                            metric="Lnight",
                            strength=strength,
                        )

            # WHO 2018 (leisure): LAeq,24h only makes sense for LEQ-like columns.
            if col in leq_cols:
                leisure = g.get("leisure", {})
                leisure_metrics = (leisure.get("metrics_db") or {})
                if "LAeq_24h" in leisure_metrics:
                    out["WHO 2018 Leisure — LAeq,24h"] = _result(
                        float(leisure_metrics["LAeq_24h"]),
                        float(current_laeq_24h) if current_laeq_24h is not None else None,
                        metric="LAeq_24h",
                        strength=str(leisure.get("recommendation_strength") or ""),
                    )

            # Single-event sleep disturbance (LAmax) should come from L-Max streams.
            # This is not a WHO 2018 Lden/Lnight guideline comparison.
            if time_cols and _is_lmax(col):
                try:
                    y = pd.to_numeric(self.df[col], errors="coerce")
                    h = ts.dt.hour
                    is_night = (h >= 23) | (h < 7)
                    night_max = float(y[is_night].max()) if (is_night.any() and y[is_night].notna().any()) else None
                    if night_max is not None:
                        out["Sleep disturbance (facade) — LAmax night"] = _result(
                            60.0,
                            night_max,
                            metric="LAmax_night",
                            strength="contextual",
                        )
                except Exception:
                    pass

            compliance[col] = out

        return compliance
    
    def _generate_interpretations(self):
        """Generate human-readable interpretations"""
        interpretations = []
        
        for col in self.noise_columns:
            data = self.df[col].dropna()
            mean_level = energetic_mean_db(data)
            mean_level = float(mean_level) if mean_level is not None else float(data.mean())
            
            # Interpret noise levels
            if mean_level < 30:
                interpretation = f"{col}: Very quiet environment (peaceful setting)"
            elif mean_level < 50:
                interpretation = f"{col}: Quiet (suitable for residential areas)"
            elif mean_level < 60:
                interpretation = f"{col}: Moderate (busy residential area)"
            elif mean_level < 70:
                interpretation = f"{col}: Noisy (commercial area level)"
            elif mean_level < 80:
                interpretation = f"{col}: Very noisy (industrial area level)"
            else:
                interpretation = f"{col}: Extremely noisy (potentially harmful)"
            
            interpretations.append(interpretation)
        
        return interpretations
