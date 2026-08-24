# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
import pandas as pd
import numpy as np
import re
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
from datetime import datetime
import io
from analysis.acoustics import energetic_mean_db


def _emean(series):
    """Energy-average (LAeq) of dB values; NaN if empty. Accepts Series/ndarray/list."""
    v = energetic_mean_db(pd.to_numeric(pd.Series(series).to_numpy().ravel(), errors='coerce'))
    return float(v) if v is not None else float('nan')


class AdvancedChartGenerator:
    """Generate advanced charts: heatmaps, radar charts, and enhanced visualizations"""
    
    def __init__(self, df):
        self.df = df.copy()
        self.noise_columns = self._identify_noise_columns()
        self.time_col = self._identify_time_column()
        self._prepare_data()

    def _pick_first(self, cols):
        return cols[0] if cols else None

    def _ensure_datetime_column(self):
        """Ensure we have a usable datetime column (combine Date+Time when split)."""
        if self.df.empty:
            return self.time_col

        # Prefer explicit datetime/timestamp columns.
        datetime_cols = [c for c in self.df.columns if any(k in c.lower() for k in ['datetime', 'timestamp'])]
        if datetime_cols:
            return datetime_cols[0]

        date_cols = [c for c in self.df.columns if ('date' in c.lower()) and ('time' not in c.lower())]
        time_cols = [c for c in self.df.columns if ('time' in c.lower()) and ('date' not in c.lower())]

        primary = self.time_col
        if primary and primary in self.df.columns:
            sample = self.df[primary].dropna()
            if len(sample) > 5000:
                sample = sample.iloc[:5000]
            parsed = pd.to_datetime(sample, errors='coerce', cache=True)
            if parsed.notna().any():
                unique_dates = parsed.dt.normalize().nunique(dropna=True)
                unique_times = (parsed.dt.hour.astype('Int64').astype(str) + ':' +
                                parsed.dt.minute.astype('Int64').astype(str)).nunique(dropna=True)
                name = primary.lower()
                looks_time_only = ('time' in name and 'date' not in name and unique_dates <= 1 and unique_times > 1)
                looks_date_only = ('date' in name and 'time' not in name and unique_times <= 1 and unique_dates >= 1)
                if looks_time_only and date_cols:
                    time_cols = [primary] + [c for c in time_cols if c != primary]
                elif looks_date_only and time_cols:
                    date_cols = [primary] + [c for c in date_cols if c != primary]

        date_col = self._pick_first(date_cols)
        time_col = self._pick_first(time_cols)

        if date_col and time_col:
            combined_name = '__combined_datetime__'
            if combined_name in self.df.columns:
                suffix = 2
                while f'{combined_name}_{suffix}' in self.df.columns:
                    suffix += 1
                combined_name = f'{combined_name}_{suffix}'

            date_part = pd.to_datetime(self.df[date_col], errors='coerce', cache=True).dt.strftime('%Y-%m-%d')
            time_raw = self.df[time_col]
            time_parsed = pd.to_datetime(time_raw, errors='coerce', cache=True)
            time_part = time_parsed.dt.strftime('%H:%M:%S')

            if time_part.isna().all():
                numeric = pd.to_numeric(time_raw, errors='coerce')
                if numeric.notna().any():
                    hours = numeric.round().astype('Int64')
                    time_part = hours.map(lambda h: f'{int(h):02d}:00:00' if pd.notna(h) else None)

            combined_str = (date_part.fillna('') + ' ' + time_part.fillna('')).str.strip()
            self.df[combined_name] = pd.to_datetime(combined_str, errors='coerce')
            if self.df[combined_name].notna().sum() > 0:
                return combined_name

        return self.time_col
    
    def _identify_noise_columns(self):
        """Identify columns containing noise measurements"""
        potential_cols = []
        for col in self.df.columns:
            col_lower = col.lower()
            if any(term in col_lower for term in ['db', 'decibel', 'level', 'sound', 'noise', 'spl', 'leq', 'lp']):
                potential_cols.append(col)
        
        if not potential_cols:
            numeric_cols = self.df.select_dtypes(include=[np.number]).columns.tolist()
            potential_cols = [col for col in numeric_cols 
                            if not any(term in col.lower() for term in ['time', 'date', 'hour', 'minute', 'second', 'id', 'index'])]
        
        return potential_cols

    def _pick_primary_noise_column(self):
        """Prefer an Leq/L_EQ-like column for charts when available."""
        if not self.noise_columns:
            return None

        def score(col: str) -> tuple[int, int]:
            c = (col or '').lower()
            if 'leq' in c or 'l_eq' in c or 'l-eq' in c:
                return (0, len(c))
            if re.search(r'\beq\b', c):
                return (1, len(c))
            if 'max' in c or 'peak' in c or 'l-max' in c or 'lmax' in c:
                return (3, len(c))
            if 'min' in c or 'background' in c or 'l-min' in c or 'lmin' in c:
                return (3, len(c))
            return (2, len(c))

        scored = sorted(((score(c), i, c) for i, c in enumerate(self.noise_columns)), key=lambda x: (x[0], x[1]))
        return scored[0][2]
    
    def _identify_time_column(self):
        """Identify the time/date column via the shared authoritative resolver."""
        from analysis.timestamp_utils import resolve_time_column
        return resolve_time_column(self.df)
    
    def _prepare_data(self):
        """Prepare and clean data"""
        for col in self.noise_columns:
            self.df[col] = pd.to_numeric(self.df[col], errors='coerce')

        self.time_col = self._ensure_datetime_column()
        if self.time_col and self.time_col in self.df.columns:
            # Robust parse: charts must sit on the SAME timeline as the tables
            # and the narrative, or a figure will disagree with the text beside it.
            from analysis.timestamp_utils import parse_timestamps_robust
            self.df[self.time_col], _ = parse_timestamps_robust(self.df[self.time_col])

    def _generate_date_hour_heatmap(self, df, noise_col, title):
        """Internal helper to build a Date (X) × Hour (Y) heatmap.

        Uses LAeq (energy-average) per cell. Y-axis strings align tick centres
        to cell centres — no half-hour offset.
        """
        if df.empty:
            return None

        df = df[[self.time_col, noise_col]].copy()
        df = df.dropna()
        if df.empty:
            return None

        df['Date'] = df[self.time_col].dt.floor('D')
        df['Hour'] = df[self.time_col].dt.hour

        def laeq(series: pd.Series) -> float:
            s = pd.to_numeric(series, errors='coerce').dropna().astype(float)
            if s.empty:
                return float('nan')
            return float(10.0 * np.log10(np.mean(np.power(10.0, s / 10.0))))

        heatmap_data = df.pivot_table(
            values=noise_col,
            index='Hour',
            columns='Date',
            aggfunc=laeq,
        )

        heatmap_data = heatmap_data.reindex(index=list(range(24)))

        if len(heatmap_data.columns) > 0:
            min_d = pd.to_datetime(min(heatmap_data.columns))
            max_d = pd.to_datetime(max(heatmap_data.columns))
            all_dates = pd.date_range(min_d, max_d, freq='D')
            heatmap_data = heatmap_data.reindex(columns=all_dates)
        else:
            all_dates = []

        x_labels = [pd.to_datetime(d).strftime('%Y-%m-%d') for d in getattr(heatmap_data, 'columns', [])]
        # Use string labels so Plotly places ticks at cell centres (categorical mode).
        y_labels = [f'{h:02d}:00' for h in heatmap_data.index]

        cb_title = 'LAeq dB(A)' if noise_col and any(k in str(noise_col).lower() for k in ['leq', 'l_eq', 'l-eq']) else 'dB(A)'

        # Plain Python list to avoid Plotly binary (bdata) encoding in newer plotly.py
        z_list = [[None if (v != v) else float(v) for v in row] for row in heatmap_data.values]

        fig = go.Figure(data=go.Heatmap(
            z=z_list,
            x=x_labels,
            y=y_labels,
            colorscale='RdYlGn_r',
            xgap=1,
            ygap=1,
            hovertemplate='Date: %{x}<br>Hour: %{y}<br>LAeq: %{z:.1f} dB(A)<extra></extra>',
            colorbar=dict(title=cb_title),
        ))

        fig.update_layout(
            title=title,
            xaxis_title='Date',
            yaxis_title='Hour of Day',
            autosize=True,
            height=650,
            hovermode='closest',
            xaxis=dict(tickangle=-45, automargin=True),
            yaxis=dict(
                tickmode='array',
                tickvals=y_labels,
                ticktext=y_labels,
                autorange='reversed',
                automargin=True,
            ),
        )

        return fig
    
    def generate_heatmap_hourly(self, noise_col=None):
        """Generate date-hour heatmap for the most recent 7 days.

        X-axis: Date
        Y-axis: Hour (0-23)
        Cell value: mean Leq (dB(A))
        """
        if not self.time_col:
            return None
        
        if noise_col is None:
            noise_col = self._pick_primary_noise_column()
        
        if noise_col is None:
            return None
        
        df = self.df[[self.time_col, noise_col]].copy()
        df = df.dropna()
        if df.empty:
            return None

        # Filter to the most recent 7 days to keep the chart readable.
        max_ts = df[self.time_col].max()
        if pd.notna(max_ts):
            start = (max_ts.normalize() - pd.Timedelta(days=6))
            df = df[df[self.time_col] >= start]

        return self._generate_date_hour_heatmap(
            df=df,
            noise_col=noise_col,
            title='Heatmap: Noise Levels by Hour and Date (Last 7 Days)',
        )
    
    def generate_heatmap_daily(self, noise_col=None):
        """Generate daily heatmap with X=Date and Y=Hour (0-23).

        Each cell represents the (mean) Leq level for that hour of that day.
        """
        if not self.time_col:
            return None
        
        if noise_col is None:
            noise_col = self._pick_primary_noise_column()
        
        if noise_col is None:
            return None
        
        df = self.df[[self.time_col, noise_col]].copy()
        df = df.dropna()

        return self._generate_date_hour_heatmap(
            df=df,
            noise_col=noise_col,
            title='Heatmap: Noise Levels by Hour and Date',
        )
    
    def generate_radar_chart(self, noise_col=None):
        """Generate radar chart with key metrics"""
        if noise_col is None:
            noise_col = self._pick_primary_noise_column()
        
        if noise_col is None:
            return None
        
        data = self.df[noise_col].dropna()
        
        # Calculate metrics (normalized to 0-100 scale for radar)
        mean_noise = _emean(data)  # LAeq (energy average)
        max_noise = data.max()
        percentile_5 = data.quantile(0.05)
        percentile_95 = data.quantile(0.95)
        std_dev = data.std()
        
        # Normalize metrics to 0-100 scale
        max_expected = 100
        metrics = {
            'Mean Level': min((mean_noise / max_expected) * 100, 100),
            'Peak Level': min((max_noise / max_expected) * 100, 100),
            'P5 Level': min((percentile_5 / max_expected) * 100, 100),
            'P95 Level': min((percentile_95 / max_expected) * 100, 100),
            'Variability': min((std_dev / 20) * 100, 100)  # Std dev scale
        }
        
        fig = go.Figure(data=go.Scatterpolar(
            r=list(metrics.values()),
            theta=list(metrics.keys()),
            fill='toself',
            name=noise_col,
            line_color='rgba(102, 126, 234, 1)',
            fillcolor='rgba(102, 126, 234, 0.5)'
        ))
        
        fig.update_layout(
            polar=dict(
                radialaxis=dict(
                    visible=True,
                    range=[0, 100]
                )
            ),
            title=f'Noise Level Radar Chart - {noise_col}',
            width=700,
            height=700,
            showlegend=True
        )
        
        return fig
    
    def generate_multi_radar_chart(self):
        """Generate radar chart comparing all noise columns"""
        if len(self.noise_columns) < 2:
            return None
        
        fig = go.Figure()
        
        max_expected = 100
        colors = ['rgba(102, 126, 234, 0.5)', 'rgba(118, 75, 162, 0.5)', 
                  'rgba(231, 76, 60, 0.5)', 'rgba(46, 204, 113, 0.5)']
        
        for idx, col in enumerate(self.noise_columns[:4]):
            data = self.df[col].dropna()
            
            _laeq = _emean(data)  # LAeq (energy average) for the "Mean" spoke
            metrics = {
                'Mean': min((_laeq / max_expected) * 100, 100),
                'Peak': min((data.max() / max_expected) * 100, 100),
                'Min': min((data.min() / max_expected) * 100, 100),
                'Std Dev': min((data.std() / 20) * 100, 100),
                'CV%': min((data.std() / data.mean() * 100) if data.mean() > 0 else 0, 100)
            }
            
            fig.add_trace(go.Scatterpolar(
                r=list(metrics.values()),
                theta=list(metrics.keys()),
                fill='toself',
                name=col,
                line_color=colors[idx % len(colors)],
                fillcolor=colors[idx % len(colors)]
            ))
        
        fig.update_layout(
            polar=dict(
                radialaxis=dict(
                    visible=True,
                    range=[0, 100]
                )
            ),
            title='Multi-Column Noise Comparison Radar',
            width=900,
            height=700,
            showlegend=True
        )
        
        return fig
    
    def generate_time_series_heatmap(self, noise_col=None):
        """Generate time series with background heatmap effect"""
        if not self.time_col or noise_col is None:
            noise_col = self.noise_columns[0] if self.noise_columns else None
        
        if noise_col is None or not self.time_col:
            return None
        
        df = self.df[[self.time_col, noise_col]].copy()
        df = df.dropna()
        df = df.sort_values(self.time_col)
        
        # Create color scale based on noise level
        colors = []
        for value in df[noise_col].values:
            if value < 55:
                colors.append('green')
            elif value < 70:
                colors.append('orange')
            else:
                colors.append('red')
        
        fig = go.Figure()
        
        fig.add_trace(go.Scatter(
            x=df[self.time_col],
            y=df[noise_col],
            mode='lines+markers',
            name=noise_col,
            line=dict(color='rgba(102, 126, 234, 0.8)', width=2),
            marker=dict(size=4, color=colors, opacity=0.8)
        ))
        
        # Add standard level lines
        fig.add_hline(y=55, line_dash="dash", line_color="green", 
                      annotation_text="Residential Limit (55 dB)")
        fig.add_hline(y=70, line_dash="dash", line_color="orange", 
                      annotation_text="Alert Level (70 dB)")
        fig.add_hline(y=80, line_dash="dash", line_color="red", 
                      annotation_text="Danger Level (80 dB)")
        
        fig.update_layout(
            title=f'Time Series with Compliance Zones - {noise_col}',
            xaxis_title='Time',
            yaxis_title='Noise Level (dB)',
            width=1200,
            height=500,
            hovermode='x unified',
            plot_bgcolor='rgba(240, 240, 240, 0.5)'
        )
        
        return fig
    
    def generate_distribution_heatmap(self, noise_col=None):
        """Generate 2D histogram/heatmap of noise distribution"""
        if not self.time_col:
            return None
        
        if noise_col is None:
            noise_col = self._pick_primary_noise_column()
        
        if noise_col is None:
            return None
        
        df = self.df[[self.time_col, noise_col]].copy()
        df = df.dropna()
        
        # Create 2D histogram
        fig = go.Figure(data=go.Histogram2d(
            x=df[self.time_col],
            y=df[noise_col],
            colorscale='Jet',
            nbinsx=50,
            nbinsy=30,
            colorbar=dict(title="Frequency")
        ))
        
        fig.update_layout(
            title=f'Noise Level Distribution Over Time - {noise_col}',
            xaxis_title='Time',
            yaxis_title='Noise Level (dB)',
            width=1000,
            height=600
        )
        
        return fig
    
    def generate_diurnal_box_whisker_chart(self, noise_col=None):
        """Generate a diurnal box-and-whisker chart for hourly LEQ volatility."""
        if not self.time_col:
            return None

        if noise_col is None:
            noise_col = self._pick_primary_noise_column()

        if noise_col is None:
            return None

        df = self.df[[self.time_col, noise_col]].copy().dropna()
        if df.empty:
            return None

        df['Hour'] = pd.to_datetime(df[self.time_col], errors='coerce').dt.hour
        df[noise_col] = pd.to_numeric(df[noise_col], errors='coerce')
        df = df.dropna(subset=['Hour', noise_col])
        if df.empty:
            return None

        hour_labels = [f"{h:02d}:00" for h in range(24)]
        fig = go.Figure()
        for hour in range(24):
            hour_values = df.loc[df['Hour'] == hour, noise_col].dropna()
            if hour_values.empty:
                continue
            hour_label = f"{hour:02d}:00"
            fig.add_trace(go.Box(
                y=hour_values.tolist(),
                name=hour_label,
                boxpoints='outliers',
                quartilemethod='linear',
                marker=dict(color='#0066cc'),
                line=dict(color='#003f7d', width=1.5),
                fillcolor='rgba(70, 130, 200, 0.35)',
                hovertemplate=f'Hour {hour_label}<br>LEQ: %{{y:.1f}} dB(A)<extra></extra>',
                showlegend=False,
            ))

        fig.update_layout(
            title=dict(
                text='Diurnal Box-and-Whisker: 24-Hour Noise Pattern',
                font=dict(size=20, color='#333333'),
                x=0.5,
                xanchor='center'
            ),
            xaxis=dict(
                title='Hour of Day',
                tickmode='array',
                tickvals=hour_labels,
                ticktext=hour_labels,
                categoryorder='array',
                categoryarray=hour_labels,
                automargin=True,
            ),
            yaxis=dict(title='LEQ dB(A)', gridcolor='rgba(0, 0, 0, 0.08)', zeroline=False),
            width=1000,
            height=620,
            showlegend=False,
            margin=dict(l=80, r=80, t=100, b=80),
            paper_bgcolor='white',
            plot_bgcolor='rgba(245, 245, 245, 1)',
            hovermode='closest',
            font=dict(family='Arial, sans-serif', size=12, color='#333333')
        )

        return fig

    def generate_polar_24hour_chart(self, noise_col=None):
        """Produce a 24-hour polar/radar chart showing mean LEQ per hour.

        This reintroduces a genuine polar visualization (was previously
        a compatibility wrapper). It computes mean levels per hour and
        plots them around a circle so readers can inspect diurnal patterns.
        """
        if noise_col is None:
            noise_col = self._pick_primary_noise_column()

        if noise_col is None or self.time_col is None:
            return None

        df = self.df[[self.time_col, noise_col]].copy().dropna()
        if df.empty:
            return None

        df[self.time_col] = pd.to_datetime(df[self.time_col], errors='coerce')
        df['hour'] = df[self.time_col].dt.hour
        # Diurnal hourly level = LAeq (energy average) per hour, not arithmetic.
        hourly_mean = df.groupby('hour')[noise_col].apply(_emean).reindex(range(24))
        # If there are no per-hour values (all NaN), try a fallback using overall LAeq
        if hourly_mean.dropna().empty:
            overall_mean = _emean(df[noise_col])
            if pd.isna(overall_mean):
                return None
            hourly_mean = hourly_mean.fillna(overall_mean)

        # Close the loop so the polar chart connects 23 -> 0
        r = hourly_mean.fillna(np.nan).tolist()
        theta = [f'{h:02d}:00' for h in range(24)]
        # Append first value to close the polygon
        r.append(r[0])
        theta.append(theta[0])

        fig = go.Figure(data=go.Scatterpolar(
            r=r,
            theta=theta,
            mode='lines+markers',
            fill='toself',
            name=noise_col,
            line_color='rgba(102, 126, 234, 1)',
            marker=dict(size=6)
        ))

        fig.update_layout(
            polar=dict(
                radialaxis=dict(visible=True, title='Mean dB', tickfont=dict(size=10)),
                angularaxis=dict(direction='clockwise')
            ),
            title=f'Polar 24-Hour Mean Noise Pattern - {noise_col}',
            width=700,
            height=700,
        )

        return fig
    
    def generate_all_charts_html(self):
        """Generate HTML with all charts embedded"""
        charts = []
        
        # Hourly heatmap
        hm_hourly = self.generate_heatmap_hourly()
        if hm_hourly:
            charts.append(('Heatmap (Last 7 Days)', hm_hourly.to_html(include_plotlyjs='cdn')))
        else:
            charts.append(('Heatmap (Last 7 Days)', '<p>No heatmap data available</p>'))
        
        # Daily heatmap
        hm_daily = self.generate_heatmap_daily()
        if hm_daily:
            charts.append(('Heatmap (All Dates)', hm_daily.to_html(include_plotlyjs=False)))
        else:
            charts.append(('Heatmap (All Dates)', '<p>No heatmap data available</p>'))
        
        # Time series heatmap
        ts_hm = self.generate_time_series_heatmap()
        if ts_hm:
            charts.append(('Time Series Heatmap', ts_hm.to_html(include_plotlyjs=False)))
        else:
            charts.append(('Time Series Heatmap', '<p>No time series data available</p>'))
        
        # Distribution heatmap
        dist_hm = self.generate_distribution_heatmap()
        if dist_hm:
            charts.append(('Distribution Heatmap', dist_hm.to_html(include_plotlyjs=False)))
        else:
            charts.append(('Distribution Heatmap', '<p>No distribution data available</p>'))
        
        # Diurnal box-and-whisker chart
        diurnal = self.generate_diurnal_box_whisker_chart()
        if diurnal:
            charts.append(('Diurnal Box-and-Whisker', diurnal.to_html(include_plotlyjs=False)))
        else:
            charts.append(('Diurnal Box-and-Whisker', '<p>No hourly pattern data available</p>'))

        # Polar 24-hour chart (optional)
        polar = self.generate_polar_24hour_chart()
        if polar:
            charts.append(('Polar 24-Hour Pattern', polar.to_html(include_plotlyjs=False)))

        # Radar summaries (single and multi-column) when available
        radar = self.generate_radar_chart()
        if radar:
            charts.append(('Radar Chart', radar.to_html(include_plotlyjs=False)))

        multi_radar = self.generate_multi_radar_chart()
        if multi_radar:
            charts.append(('Multi-Column Radar', multi_radar.to_html(include_plotlyjs=False)))
        
        # Generate HTML
        html_content = """
        <!DOCTYPE html>
        <html lang="en">
        <head>
            <meta charset="UTF-8">
            <meta name="viewport" content="width=device-width, initial-scale=1.0">
            <title>Advanced Charts - Noise Analysis Platform</title>
            <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
            <style>
                * {
                    margin: 0;
                    padding: 0;
                    box-sizing: border-box;
                }
                
                body {
                    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                    background-color: #f5f5f5;
                    color: #333;
                }
                
                header {
                    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white;
                    padding: 30px;
                    text-align: center;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.2);
                }
                
                header h1 {
                    font-size: 28px;
                    margin-bottom: 10px;
                }
                
                header p {
                    font-size: 14px;
                    opacity: 0.9;
                }
                
                .container {
                    max-width: 1400px;
                    margin: 0 auto;
                    padding: 20px;
                }
                
                .chart-section {
                    background: white;
                    border-radius: 8px;
                    margin-bottom: 30px;
                    padding: 20px;
                    box-shadow: 0 2px 8px rgba(0,0,0,0.1);
                }
                
                .chart-section h2 {
                    color: #667eea;
                    margin-bottom: 15px;
                    font-size: 20px;
                    border-bottom: 2px solid #667eea;
                    padding-bottom: 10px;
                }
                
                .chart-container {
                    width: 100%;
                    overflow-x: auto;
                }
                
                footer {
                    background: #333;
                    color: white;
                    text-align: center;
                    padding: 20px;
                    margin-top: 40px;
                }
                
                .download-section {
                    background: #f0f0f0;
                    padding: 15px;
                    border-radius: 5px;
                    margin-bottom: 20px;
                    text-align: center;
                }
                
                .btn {
                    background: #667eea;
                    color: white;
                    padding: 10px 20px;
                    border: none;
                    border-radius: 5px;
                    cursor: pointer;
                    font-size: 14px;
                    margin: 5px;
                    transition: background 0.3s;
                }
                
                .btn:hover {
                    background: #764ba2;
                }
                
                .info-box {
                    background: #ecf0f1;
                    border-left: 4px solid #667eea;
                    padding: 15px;
                    margin-bottom: 15px;
                    border-radius: 3px;
                }
            </style>
        </head>
        <body>
            <header>
                <h1>🎨 Advanced Noise Analysis Charts</h1>
                <p>Generated on """ + datetime.now().strftime("%Y-%m-%d %H:%M:%S") + """</p>
            </header>
            
            <div class="container">
                <div class="download-section">
                    <button class="btn" onclick="window.print()">🖨️ Print All Charts</button>
                    <button class="btn" onclick="downloadCharts()">💾 Download as HTML</button>
                </div>
                
                <div class="info-box">
                    <strong>ℹ️ Chart Descriptions:</strong>
                    <ul style="margin-left: 20px; margin-top: 10px;">
                        <li><strong>Heatmap (Last 7 Days):</strong> X=Date, Y=Hour (0–23), cell=Leq dB(A) (mean)</li>
                        <li><strong>Heatmap (All Dates):</strong> Same layout across the full date range in the file</li>
                        <li><strong>Radar Chart:</strong> Visualizes key noise metrics in a polar plot</li>
                        <li><strong>Multi-Column Radar:</strong> Compares all measurement columns</li>
                        <li><strong>Time Series Heatmap:</strong> Shows full time series with compliance zones</li>
                        <li><strong>Distribution Heatmap:</strong> 2D histogram of noise distribution over time</li>
                    </ul>
                </div>
        """
        
        for title, chart_html in charts:
            html_content += f"""
                <div class="chart-section">
                    <h2>{title}</h2>
                    <div class="chart-container">
                        {chart_html}
                    </div>
                </div>
            """
        
        html_content += """
            </div>
            
            <footer>
                <p>Advanced Chart Generation - Noise Analysis Platform</p>
                <p style="font-size: 12px; margin-top: 10px;">For professional acoustic assessment, consult a certified acoustic engineer.</p>
            </footer>
            
            <script>
                function downloadCharts() {
                    const element = document.documentElement;
                    const opt = {
                        margin: 10,
                        filename: 'advanced_charts.html',
                        image: { type: 'png', quality: 0.98 },
                        html2canvas: { scale: 2 },
                        jsPDF: { orientation: 'landscape', unit: 'mm', format: 'a4' }
                    };
                    const link = document.createElement('a');
                    link.href = 'data:text/html,' + encodeURIComponent(document.documentElement.outerHTML);
                    link.download = 'advanced_charts.html';
                    link.click();
                }
            </script>
        </body>
        </html>
        """
        
        return html_content
