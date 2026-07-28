"""
Enhanced Environmental Metrics Module
Calculates advanced environmental analysis metrics for noise data
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from analysis.acoustics import energetic_mean_db


def _emean(series):
    """Energy-average (LAeq) of dB values; NaN if empty. Accepts Series/ndarray/list."""
    v = energetic_mean_db(pd.to_numeric(pd.Series(series).to_numpy().ravel(), errors='coerce'))
    return float(v) if v is not None else float('nan')


class EnvironmentalMetricsCalculator:
    """Calculate advanced environmental metrics for regulatory compliance and analysis"""
    
    def __init__(self, df, noise_columns, time_column=None):
        """
        Args:
            df: DataFrame with measurements
            noise_columns: List of noise measurement columns
            time_column: Name of datetime column (auto-detected if None)
        """
        self.df = df.copy()
        self.noise_columns = noise_columns
        self.time_column = time_column or self._identify_time_column()
        self._prepare_data()
    
    def _identify_time_column(self):
        """Find datetime column"""
        from analysis.timestamp_utils import resolve_time_column
        return resolve_time_column(self.df)
    
    def _prepare_data(self):
        """Prepare temporal features"""
        if self.time_column:
            from analysis.timestamp_utils import parse_timestamps_robust
            self.df[self.time_column], _ = parse_timestamps_robust(self.df[self.time_column])
            self.df['hour'] = self.df[self.time_column].dt.hour
            self.df['day_of_week'] = self.df[self.time_column].dt.dayofweek
            self.df['is_weekend'] = self.df['day_of_week'].isin([5, 6])
            self.df['date'] = self.df[self.time_column].dt.date
    
    def calculate_exceedance_metrics(self, noise_col, standards=None):
        """
        Calculate exceedance frequency and severity metrics
        
        Args:
            noise_col: Noise column to analyze
            standards: Dict with standard names and dB limits
        
        Returns:
            Dict with exceedance metrics
        """
        if standards is None:
            standards = {
                'residential_day': 55,
                'residential_night': 45,
                'commercial': 65,
                'industrial': 75
            }
        
        data = self.df[noise_col].dropna()
        metrics = {}

        # A sample count is NOT a duration. These loggers record at 1 Hz, so
        # reporting the number of exceeding samples as "hours_above" overstated
        # duration by a factor of 3600. Convert using the measured logging
        # interval instead.
        interval_s = self._logging_interval_seconds()

        for std_name, limit in standards.items():
            exceeds = data > limit
            n_above = int(exceeds.sum())
            n_below = int((~exceeds).sum())
            metrics[std_name] = {
                'exceedance_count': n_above,
                'exceedance_frequency_pct': float((exceeds.sum() / len(data) * 100)),
                # Mean amount by which the limit is exceeded, in dB.
                'avg_exceedance_amount': float((data[exceeds] - limit).mean()) if exceeds.any() else 0,
                'max_exceedance': float((data[exceeds] - limit).max()) if exceeds.any() else 0,
                'samples_above': n_above,
                'samples_below': n_below,
                'logging_interval_seconds': interval_s,
                'hours_above': (round(n_above * interval_s / 3600.0, 2)
                                if interval_s is not None else None),
                'hours_below': (round(n_below * interval_s / 3600.0, 2)
                                if interval_s is not None else None),
            }

        return metrics

    def _logging_interval_seconds(self) -> float | None:
        """Modal spacing between consecutive samples, in seconds.

        Returns None when there is no usable timestamp column, in which case
        sample counts cannot be converted to durations and the duration fields
        are reported as None rather than guessed.
        """
        if not self.time_column or self.time_column not in self.df.columns:
            return None
        ts = pd.to_datetime(self.df[self.time_column], errors='coerce').dropna().sort_values()
        if len(ts) < 2:
            return None
        deltas = ts.diff().dt.total_seconds().dropna()
        deltas = deltas[deltas > 0]
        if deltas.empty:
            return None
        mode = deltas.mode()
        return float(mode.iloc[0]) if not mode.empty else float(deltas.median())
    
    def calculate_percentile_metrics(self, noise_col):
        """
        Calculate extended percentile distribution
        
        Args:
            noise_col: Noise column to analyze
        
        Returns:
            Dict with percentile values
        """
        data = self.df[noise_col].dropna()
        
        percentiles = [1, 5, 10, 25, 50, 75, 90, 95, 99]
        metrics = {}
        
        for p in percentiles:
            metrics[f'L{p}'] = float(np.percentile(data, p))
        
        # Energy average (LAeq) — 10*log10(mean(10^(L/10))). An arithmetic mean of
        # decibels is not the equivalent continuous level and understates any
        # record containing loud events; on these datasets the two differ by
        # several dB.
        _laeq = _emean(data)
        metrics['LAeq'] = float(_laeq) if _laeq is not None else float('nan')
        metrics['L_arithmetic_mean'] = float(data.mean())
        metrics['Lmax'] = float(data.max())
        metrics['Lmin'] = float(data.min())
        metrics['Lrange'] = float(data.max() - data.min())
        
        # Interquartile range
        metrics['IQR'] = float(metrics['L75'] - metrics['L25'])
        
        return metrics
    
    def calculate_temporal_metrics(self, noise_col):
        """
        Calculate time-based metrics (day/night, weekday/weekend, peak hours)
        
        Args:
            noise_col: Noise column to analyze
        
        Returns:
            Dict with temporal metrics
        """
        metrics = {}
        
        if not self.time_column:
            return metrics
        
        # Day vs Night (arbitrary: day = 7-22, night = 22-7)
        day_data = self.df[(self.df['hour'] >= 7) & (self.df['hour'] < 22)][noise_col]
        night_data = self.df[((self.df['hour'] >= 22) | (self.df['hour'] < 7))][noise_col]
        
        metrics['day'] = {
            'mean': _emean(day_data),  # LAeq (energy average)
            'std': float(day_data.std()),
            'max': float(day_data.max()),
            'min': float(day_data.min()),
            'count': len(day_data)
        }

        metrics['night'] = {
            'mean': _emean(night_data),  # LAeq (energy average)
            'std': float(night_data.std()),
            'max': float(night_data.max()),
            'min': float(night_data.min()),
            'count': len(night_data)
        }
        
        # Decibels are a logarithmic interval scale with an arbitrary zero, so the
        # RATIO of two dB values is meaningless (55/45 is not "22% louder").
        # The physically meaningful comparison is the difference in dB, which
        # corresponds to an energy ratio of 10^(diff/10).
        _d, _n = metrics['day']['mean'], metrics['night']['mean']
        if _d is not None and _n is not None and np.isfinite(_d) and np.isfinite(_n):
            metrics['day_night_difference_db'] = float(_d - _n)
            metrics['day_night_energy_ratio'] = float(10.0 ** ((_d - _n) / 10.0))
        else:
            metrics['day_night_difference_db'] = None
            metrics['day_night_energy_ratio'] = None
        
        # Weekday vs Weekend
        if 'day_of_week' in self.df.columns:
            weekday = self.df[~self.df['is_weekend']][noise_col]
            weekend = self.df[self.df['is_weekend']][noise_col]
            
            metrics['weekday'] = {
                'mean': _emean(weekday),  # LAeq (energy average)
                'std': float(weekday.std()),
                'max': float(weekday.max()),
                'min': float(weekday.min())
            }

            metrics['weekend'] = {
                'mean': _emean(weekend),  # LAeq (energy average)
                'std': float(weekend.std()),
                'max': float(weekend.max()),
                'min': float(weekend.min())
            }
            
            # Difference in dB, not a ratio — see day_night_difference_db above.
            _wd, _we = metrics['weekday']['mean'], metrics['weekend']['mean']
            if _wd is not None and _we is not None and np.isfinite(_wd) and np.isfinite(_we):
                metrics['weekday_weekend_difference_db'] = float(_wd - _we)
            else:
                metrics['weekday_weekend_difference_db'] = None
        
        # Peak hours (find top 3 hours with highest average)
        if 'hour' in self.df.columns:
            hourly_mean = self.df.groupby('hour')[noise_col].apply(_emean)  # LAeq per hour
            top_3_hours = hourly_mean.nlargest(3)
            
            metrics['peak_hours'] = [
                {'hour': int(h), 'level': float(lvl)} 
                for h, lvl in top_3_hours.items()
            ]
            
            metrics['quiet_hours'] = [
                {'hour': int(h), 'level': float(lvl)} 
                for h, lvl in hourly_mean.nsmallest(3).items()
            ]
        
        return metrics
    
    def calculate_variability_metrics(self, noise_col):
        """
        Calculate noise stability and variability metrics
        
        Args:
            noise_col: Noise column to analyze
        
        Returns:
            Dict with variability metrics
        """
        data = self.df[noise_col].dropna()
        mean = data.mean()
        
        metrics = {}
        
        # Standard deviation (measure of spread)
        metrics['std_dev'] = float(data.std())
        
        # Coefficient of variation (normalized spread)
        metrics['cv'] = float((data.std() / mean) * 100) if mean > 0 else 0
        
        # Peak-to-average ratio
        metrics['peak_to_avg_ratio'] = float(data.max() / mean) if mean > 0 else 0
        
        # Variability index (high = unstable, low = steady)
        q3 = data.quantile(0.75)
        q1 = data.quantile(0.25)
        metrics['interquartile_range'] = float(q3 - q1)
        
        # Skewness (distribution shape)
        metrics['skewness'] = float(data.skew())
        # Interpretation: >0 = right-skewed (occasional high spikes)
        #                 <0 = left-skewed (occasional low levels)
        #                 ~0 = symmetric
        
        # Kurtosis (tail heaviness)
        metrics['kurtosis'] = float(data.kurtosis())
        # Interpretation: >0 = heavy tails (more outliers than normal)
        #                 <0 = light tails (fewer outliers)
        
        # Flicker index (rapid changes)
        if len(data) > 1:
            changes = np.abs(data.diff().dropna())
            metrics['mean_change'] = float(changes.mean())
            metrics['max_change'] = float(changes.max())
            metrics['flicker_index'] = float(changes.std())
        
        return metrics
    
    def calculate_trend_metrics(self, noise_col):
        """
        Calculate trend analysis (increasing/decreasing)
        
        Args:
            noise_col: Noise column to analyze
        
        Returns:
            Dict with trend metrics
        """
        metrics = {}
        
        if not self.time_column or len(self.df) < 10:
            return metrics
        
        data = self.df[[self.time_column, noise_col]].dropna()
        data = data.sort_values(self.time_column)
        
        # Linear regression trend
        x = np.arange(len(data))
        y = data[noise_col].values
        
        # Fit line: y = mx + b
        m, b = np.polyfit(x, y, 1)
        
        metrics['slope'] = float(m)  # dB per measurement
        metrics['trend_direction'] = 'increasing' if m > 0.1 else ('decreasing' if m < -0.1 else 'stable')
        metrics['intercept'] = float(b)
        
        # Calculate R-squared (goodness of fit)
        y_pred = m * x + b
        ss_res = np.sum((y - y_pred) ** 2)
        ss_tot = np.sum((y - y.mean()) ** 2)
        metrics['r_squared'] = float(1 - (ss_res / ss_tot)) if ss_tot > 0 else 0
        
        # Per-day trend if we have multiple days
        if self.time_column and 'date' in self.df.columns:
            daily_mean = self.df.groupby('date')[noise_col].apply(_emean)  # LAeq per day
            if len(daily_mean) > 1:
                x_days = np.arange(len(daily_mean))
                m_day, b_day = np.polyfit(x_days, daily_mean.values, 1)
                metrics['daily_slope'] = float(m_day)  # dB per day
                metrics['daily_trend'] = 'increasing' if m_day > 0.1 else ('decreasing' if m_day < -0.1 else 'stable')
        
        return metrics
    
    def detect_anomalies(self, noise_col, threshold_std=2.5):
        """
        Detect anomalous measurements (spikes, dips)
        
        Args:
            noise_col: Noise column to analyze
            threshold_std: Number of standard deviations for anomaly threshold
        
        Returns:
            List of anomaly events with metadata
        """
        data = self.df[[self.time_column, noise_col]].copy() if self.time_column else None
        
        if data is None or len(data) < 5:
            return []
        
        data = data.dropna()
        
        # Calculate rolling statistics
        window = max(12, len(data) // 20)  # Adaptive window size
        data['rolling_mean'] = data[noise_col].rolling(window=window, center=True).mean()
        data['rolling_std'] = data[noise_col].rolling(window=window, center=True).std()
        
        # Detect anomalies
        data['z_score'] = np.abs((data[noise_col] - data['rolling_mean']) / data['rolling_std'].replace(0, 1))
        data['is_anomaly'] = data['z_score'] > threshold_std
        
        # Extract anomaly events
        anomalies = []
        anomaly_groups = (data['is_anomaly'] != data['is_anomaly'].shift()).cumsum()
        
        for group_id, group in data[data['is_anomaly']].groupby(anomaly_groups):
            if len(group) > 0:
                anomalies.append({
                    'time': group[self.time_column].iloc[0],
                    'level': float(group[noise_col].max()),
                    'severity': float((group[noise_col] - group['rolling_mean']).max()),
                    'duration_minutes': len(group),
                    'event_type': 'spike' if group[noise_col].mean() > group['rolling_mean'].mean() else 'dip'
                })
        
        # Return top 10 most severe
        return sorted(anomalies, key=lambda x: x['severity'], reverse=True)[:10]
    
    def calculate_all_metrics(self, noise_col):
        """
        Calculate all environmental metrics in one call
        
        Args:
            noise_col: Noise column to analyze
        
        Returns:
            Comprehensive metrics dict
        """
        return {
            'exceedance': self.calculate_exceedance_metrics(noise_col),
            'percentiles': self.calculate_percentile_metrics(noise_col),
            'temporal': self.calculate_temporal_metrics(noise_col),
            'variability': self.calculate_variability_metrics(noise_col),
            'trend': self.calculate_trend_metrics(noise_col),
            'anomalies': self.detect_anomalies(noise_col)
        }


# Usage example
if __name__ == "__main__":
    # df = pd.read_csv('data.csv')
    # calc = EnvironmentalMetricsCalculator(df, ['Noise_Level_dB'], 'Timestamp')
    # metrics = calc.calculate_all_metrics('Noise_Level_dB')
    # print(metrics)
    pass
