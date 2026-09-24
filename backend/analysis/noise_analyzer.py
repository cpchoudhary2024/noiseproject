# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
import logging
import pandas as pd
import numpy as np
from scipy import stats
from datetime import datetime
import json

logger = logging.getLogger(__name__)

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
            from analysis.timestamp_utils import resolve_time_column
            time_col = resolve_time_column(self.df)
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
        
        # Drop only rows where EVERY noise column is missing. Dropping a row when
        # ANY column was NaN meant a single gap in, say, L-Max also deleted the
        # perfectly valid LEQ sample on that row — silently discarding good
        # measurements and biasing every downstream metric.
        # Per-column NaNs are handled at each computation via .dropna().
        initial_rows = len(self.df)
        self.df = self.df.dropna(subset=self.noise_columns, how='all')
        self.rows_dropped = int(initial_rows - len(self.df))
        self.dropped_per_column = {
            col: int(pd.to_numeric(self.df[col], errors='coerce').isna().sum())
            for col in self.noise_columns
        }
        if self.rows_dropped:
            logger.warning(
                "Dropped %d row(s) with no usable measurement in any noise column "
                "(of %d total).", self.rows_dropped, initial_rows
            )

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
        from analysis.timestamp_utils import resolve_time_column
        time_col = resolve_time_column(self.df)

        if time_col:
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
                'skewness': round(float(stats.skew(data)), 2) if len(data) >= 3 and data.nunique() > 1 else None,
                'kurtosis': round(float(stats.kurtosis(data)), 2) if len(data) >= 4 and data.nunique() > 1 else None,
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
        from analysis.timestamp_utils import resolve_time_column
        time_col = resolve_time_column(self.df)
        if not time_col:
            return {}

        from analysis.timestamp_utils import assess_timestamp_integrity
        if not assess_timestamp_integrity(self.df[time_col]).to_dict().get('time_metrics_valid'):
            return {}
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
            return []

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
            
            # Rank by magnitude. Slicing peaks[-5:] returned the last five peaks
            # in time order, not the loudest five, while labelling them "Top 5".
            peak_levels = data.iloc[peaks] if len(peaks) else data.iloc[[]]
            top5 = sorted((round(float(v), 2) for v in peak_levels), reverse=True)[:5]
            top_decile = data[data > data.quantile(0.9)]

            peaks_dict[col] = {
                'number_of_peaks': int(len(peaks)),
                'peak_values_top5_by_level': top5,
                'last5_peaks_chronological': [round(float(data.iloc[p]), 2) for p in peaks[-5:]],
                # Energy average of the loudest decile of samples.
                'average_top_decile_db': round(
                    float(energetic_mean_db(top_decile) or (top_decile.mean() if len(top_decile) else float('nan'))), 2
                ) if len(top_decile) else None,
                'max_peak': round(float(data.max()), 2),
                'peaks_per_1000_samples': round(float(len(peaks) / len(data)) * 1000, 2),
            }
        
        return peaks_dict
    
    def _time_series_analysis(self):
        """Analyze temporal patterns if time data exists"""
        analysis = {}
        
        from analysis.timestamp_utils import resolve_time_column
        time_col = resolve_time_column(self.df)

        if not time_col or not self.noise_columns:
            return {'status': 'No time data available'}

        try:
            # Use the memoized robust parse. Never re-parse with a naive
            # pd.to_datetime here, and never write back into self.df — an
            # in-place coercion would silently replace the raw column with a
            # differently-parsed timeline for every method that runs after this.
            ts_parsed = self._parsed_timestamps(time_col)

            for noise_col in self.noise_columns:
                # Calculate hourly averages if possible
                test_df = pd.DataFrame({
                    '_ts': ts_parsed,
                    noise_col: self.df[noise_col],
                }).dropna()
                test_df = test_df.set_index('_ts')
                
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
        if len(data) < 4 or data.nunique() <= 1:
            return 'Insufficient variation to assess distribution shape'
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
        """Expose the same indicative rows as the dashboard (no legacy verdicts)."""
        from analysis.compliance_matrix import matrix_from_analysis, primary_column
        stats = self._calculate_statistics()
        env = self._calculate_environmental_metrics()
        col = primary_column(stats)
        if col is None:
            return {}
        rows = matrix_from_analysis({'statistics': stats, 'environmental_metrics': env})
        result = {'current_leq': stats[col].get('laeq_db'),
                  'current_Lden': env.get(col, {}).get('Lden'),
                  'current_Lnight': env.get(col, {}).get('Lnight')}
        for row in rows:
            result[row['standard'] + ' — ' + row['metric']] = {
                **row, 'value_db': row['measured_db'], 'exceeded_by_db': max(0, row['delta_db'])}
        return {col: result}

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
