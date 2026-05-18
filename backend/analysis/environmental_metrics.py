"""
Enhanced Environmental Metrics Module
Calculates advanced environmental analysis metrics for noise data
"""

import pandas as pd
import numpy as np
from datetime import datetime, timedelta


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
        datetime_cols = [c for c in self.df.columns 
                        if any(k in c.lower() for k in ['datetime', 'timestamp', 'time'])]
        return datetime_cols[0] if datetime_cols else None
    
    def _prepare_data(self):
        """Prepare temporal features"""
        if self.time_column:
            self.df[self.time_column] = pd.to_datetime(self.df[self.time_column], errors='coerce')
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
        
        for std_name, limit in standards.items():
            exceeds = data > limit
            metrics[std_name] = {
                'exceedance_count': int(exceeds.sum()),
                'exceedance_frequency_pct': float((exceeds.sum() / len(data) * 100)),
                'avg_exceedance_amount': float((data[exceeds] - limit).mean()) if exceeds.any() else 0,
                'max_exceedance': float((data[exceeds] - limit).max()) if exceeds.any() else 0,
                'hours_above': int(exceeds.sum()),
                'hours_below': int((~exceeds).sum()),
            }
        
        return metrics
    
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
        
        # Add special metrics
        metrics['LAeq'] = float(data.mean())  # Energy average
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
            'mean': float(day_data.mean()),
            'std': float(day_data.std()),
            'max': float(day_data.max()),
            'min': float(day_data.min()),
            'count': len(day_data)
        }
        
        metrics['night'] = {
            'mean': float(night_data.mean()),
            'std': float(night_data.std()),
            'max': float(night_data.max()),
            'min': float(night_data.min()),
            'count': len(night_data)
        }
        
        metrics['day_night_ratio'] = float(
            metrics['day']['mean'] / metrics['night']['mean'] 
            if metrics['night']['mean'] > 0 else 0
        )
        
        # Weekday vs Weekend
        if 'day_of_week' in self.df.columns:
            weekday = self.df[~self.df['is_weekend']][noise_col]
            weekend = self.df[self.df['is_weekend']][noise_col]
            
            metrics['weekday'] = {
                'mean': float(weekday.mean()),
                'std': float(weekday.std()),
                'max': float(weekday.max()),
                'min': float(weekday.min())
            }
            
            metrics['weekend'] = {
                'mean': float(weekend.mean()),
                'std': float(weekend.std()),
                'max': float(weekend.max()),
                'min': float(weekend.min())
            }
            
            metrics['weekday_weekend_ratio'] = float(
                metrics['weekday']['mean'] / metrics['weekend']['mean']
                if metrics['weekend']['mean'] > 0 else 0
            )
        
        # Peak hours (find top 3 hours with highest average)
        if 'hour' in self.df.columns:
            hourly_mean = self.df.groupby('hour')[noise_col].mean()
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
            daily_mean = self.df.groupby('date')[noise_col].mean()
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
