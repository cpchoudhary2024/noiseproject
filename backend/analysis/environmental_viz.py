# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""
Enhanced Environmental Visualization Module
Implements Phase 1 quick wins for advanced environmental analysis
"""

from analysis.weather_screen import disclose_weather_screen
import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta
import plotly.io as pio


def _energy_mean_db(series) -> float:
    """Energy (logarithmic) average of dB values: 10*log10(mean(10^(L/10))).

    The correct way to average sound levels — a plain arithmetic mean of dB
    understates the true equivalent level. Accepts a Series, ndarray, or list;
    returns NaN for empty input.
    """
    arr = pd.to_numeric(pd.Series(series).to_numpy().ravel(), errors='coerce')
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return float('nan')
    return float(10.0 * np.log10(np.mean(np.power(10.0, arr / 10.0))))


class EnvironmentalVisualizationEngine:
    """Advanced visualization engine for environmental noise analysis"""
    
    def __init__(self, df, noise_columns):
        """
        Args:
            df: DataFrame with measurements
            noise_columns: List of noise measurement columns
        """
        self.df = df.copy()
        self.noise_columns = noise_columns
        self._identify_time_column()
        self._prepare_temporal_features()
    
    def _identify_time_column(self):
        """Identify datetime column via the shared authoritative resolver."""
        from analysis.timestamp_utils import resolve_time_column, parse_timestamps_robust
        self.time_col = resolve_time_column(self.df)

        if self.time_col:
            self.df[self.time_col], _ = parse_timestamps_robust(self.df[self.time_col])
    
    def _prepare_temporal_features(self):
        """Add temporal features for analysis"""
        if self.time_col:
            self.df['hour'] = self.df[self.time_col].dt.hour
            self.df['day_of_week'] = self.df[self.time_col].dt.dayofweek
            self.df['day_name'] = self.df[self.time_col].dt.day_name()
            self.df['date'] = self.df[self.time_col].dt.date
            self.df['week'] = self.df[self.time_col].dt.isocalendar().week
    
    def generate_exceedance_analysis(self, noise_col, standards=None):
        """
        Create exceedance frequency chart showing regulatory compliance over time
        
        Args:
            noise_col: Name of noise column to analyze
            standards: Dict with standard names and dB limits
                      Default: {'55 dB reference': 55, '45 dB reference': 45,
                               '65 dB reference': 65, '75 dB reference': 75}
        """
        if standards is None:
            standards = {
                '55 dB reference': 55,
                '45 dB reference': 45,
                '65 dB reference': 65,
                '75 dB reference': 75
            }
        
        if not self.time_col or noise_col not in self.df.columns:
            return None
        
        # Create hourly aggregates (use 'h' for newer pandas versions).
        # The hourly level is plotted as LAeq, so it must be energy-averaged.
        _grp = self.df.groupby(self.df[self.time_col].dt.floor('h'))[noise_col]
        hourly = _grp.agg(count='count', mean=_energy_mean_db, max='max').reset_index()
        hourly.columns = ['time', 'count', 'mean', 'max']
        if not hourly.empty:
            # Preserve missing hours: 12 retained bins need not span 12 hours.
            hourly = hourly.set_index('time').reindex(pd.date_range(
                hourly['time'].min(), hourly['time'].max(), freq='h')).rename_axis('time').reset_index()
        
        # Calculate exceedances for each standard
        fig = make_subplots(
            specs=[[{"secondary_y": True}]],
            subplot_titles=("Regulatory Exceedances Over Time",)
        )
        
        colors = ['rgba(46, 204, 113, 0.7)', 'rgba(231, 76, 60, 0.7)', 
                  'rgba(231, 76, 60, 0.8)', 'rgba(192, 57, 43, 0.8)']
        
        for (std_name, limit), color in zip(standards.items(), colors):
            exceedances = (hourly['mean'] > limit).astype(float).where(hourly['mean'].notna())
            exceedance_count = exceedances.rolling(window=12).sum()  # 12-hour rolling
            
            fig.add_trace(
                go.Scatter(
                    x=hourly['time'],
                    y=exceedance_count,
                    name=f"{std_name} ({limit} dB)",
                    fill='tozeroy',
                    line=dict(color=color.replace('0.7', '1'), width=2),
                    hovertemplate='<b>%{fullData.name}</b><br>Time: %{x}<br>Hourly bins above reference: %{y}<extra></extra>'
                ),
                secondary_y=False
            )
        
        # Overlay actual noise level
        fig.add_trace(
            go.Scatter(
                x=hourly['time'],
                y=hourly['mean'],
                name=f"{noise_col} (LAeq)",
                line=dict(color='blue', width=3, dash='dash'),
                hovertemplate='<b>Noise Level</b><br>Time: %{x}<br>Level: %{y:.1f} dB<extra></extra>'
            ),
            secondary_y=True
        )
        
        fig.update_xaxes(title_text="Time")
        fig.update_yaxes(title_text="Hourly LAeq bins above reference (rolling 12 bins)", secondary_y=False)
        fig.update_yaxes(title_text="Noise Level (dB)", secondary_y=True)
        
        fig.update_layout(
            title="Hourly reference exceedances — descriptive, not compliance",
            height=500,
            hovermode='x unified',
            template='plotly_white'
        )
        
        return disclose_weather_screen(fig, self.df)
    
    def generate_enhanced_temporal_heatmap(self, noise_col, resolution='hourly'):
        """
        Create enhanced heatmap showing hour x day patterns with compliance zones
        
        Args:
            noise_col: Noise column to analyze
            resolution: 'hourly' (hour x day_of_week) or 'daily' (day x week)
        """
        # Some Plotly installations may include templates that reference layout
        # submodules that are not available in all environments. To avoid
        # runtime import errors when copying templates, ensure no default
        # template is applied here.
        try:
            pio.templates.default = None
        except Exception:
            pass

        if not self.time_col or noise_col not in self.df.columns:
            return None
        
        if resolution == 'hourly':
            # Date × Hour heatmap (one column per calendar date, row per hour 0..23)
            # This provides precise temporal alignment and is easier to interpret
            # for multi-day measurement series.
            def _laeq_agg(series):
                s = pd.to_numeric(series, errors='coerce').dropna().astype(float)
                if s.empty:
                    return float('nan')
                return float(10.0 * np.log10(np.mean(np.power(10.0, s / 10.0))))

            self.df['date'] = pd.to_datetime(self.df[self.time_col]).dt.floor('D')
            pivot = self.df.pivot_table(
                values=noise_col,
                index='hour',
                columns='date',
                aggfunc=_laeq_agg,
            )

            # Ensure full 0..23 index ordering
            pivot = pivot.reindex(index=list(range(24)))

            # Fill in missing calendar dates so columns are continuous
            if pivot.columns.size > 0:
                min_d = pd.to_datetime(min(pivot.columns))
                max_d = pd.to_datetime(max(pivot.columns))
                all_dates = pd.date_range(min_d, max_d, freq='D')
                pivot = pivot.reindex(columns=all_dates)
            else:
                all_dates = []

            title = "24-Hour Noise Pattern (Date × Hour)"
            x_title = "Date"
            y_title = "Hour of Day"
        else:
            # Day x Week heatmap — energy-average (LAeq) per cell, not arithmetic.
            pivot = self.df.pivot_table(
                values=noise_col,
                index='date',
                columns='week',
                aggfunc=_energy_mean_db
            )
            title = "Daily Noise Pattern (Day × Week)"
            x_title = "Week Number"
            y_title = "Date"
        
        # Color scale: Green (compliant) → Yellow (warning) → Red (critical)
        x_labels = [d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d) for d in pivot.columns]
        if resolution == 'hourly':
            y_labels = [f'{h:02d}:00' for h in pivot.index]
            y_for_plot = y_labels
        else:
            y_for_plot = [str(d) for d in pivot.index]

        # Convert numpy array → plain Python list to avoid Plotly's binary (bdata) encoding,
        # which can fail to decode in some Plotly.js CDN builds.
        z_list = []
        for row in pivot.values:
            z_list.append([None if (v != v) else float(v) for v in row])  # NaN → None

        fig = go.Figure(data=go.Heatmap(
            z=z_list,
            x=x_labels,
            y=y_for_plot,
            colorscale=[
                [0,   'rgba(46, 204, 113, 1)'],
                [0.4, 'rgba(241, 196, 15, 1)'],
                [0.7, 'rgba(230, 126, 34, 1)'],
                [1,   'rgba(192, 57, 43, 1)']
            ],
            colorbar=dict(title='Noise (dB)', thickness=18),
            hovertemplate='Hour: %{y}<br>Date: %{x}<br>LAeq: %{z:.1f} dB(A)<extra></extra>',
            xgap=1,
            ygap=1,
        ))

        fig.update_layout(
            title=title,
            xaxis_title=x_title,
            yaxis_title=y_title,
            autosize=True,
            height=600,
            margin=dict(l=70, r=30, t=80, b=80),
        )

        if resolution == 'hourly':
            # y_for_plot is a list of strings ('00:00', '01:00', …).
            # Using the same strings as tickvals ensures each tick snaps to the
            # centre of its heatmap cell — no half-hour offset.
            fig.update_yaxes(
                tickmode='array',
                tickvals=y_labels,
                ticktext=y_labels,
                automargin=True,
                tickfont=dict(size=10),
                autorange='reversed',
            )
        fig.update_xaxes(tickangle=-45, automargin=True)
        
        return disclose_weather_screen(fig, self.df)

    def generate_diurnal_box_whisker(self, noise_col):
        """Create a diurnal box-and-whisker chart showing hourly LEQ volatility."""
        if not self.time_col or noise_col not in self.df.columns:
            return None

        df = self.df[[self.time_col, noise_col]].copy().dropna()
        if df.empty:
            return None

        df['hour'] = pd.to_datetime(df[self.time_col], errors='coerce').dt.hour
        df = df.dropna(subset=['hour'])
        if df.empty:
            return None

        hour_labels = [f'{h:02d}:00' for h in range(24)]
        fig = go.Figure()

        # Precompute per-hour box statistics server-side so the payload stays tiny.
        # Shipping every raw reading (millions on long datasets) freezes the browser;
        # Plotly renders an identical box from q1/median/q3/fences arrays instead.
        bx, b_q1, b_med, b_q3, b_lf, b_uf, b_mean = [], [], [], [], [], [], []
        out_x, out_y = [], []
        OUTLIER_CAP = 40  # per hour, for display only

        for hour in range(24):
            hv = pd.to_numeric(df.loc[df['hour'] == hour, noise_col], errors='coerce').dropna().to_numpy()
            if hv.size == 0:
                continue
            q1, med, q3 = (float(np.percentile(hv, p)) for p in (25, 50, 75))
            iqr = q3 - q1
            lo_w, hi_w = q1 - 1.5 * iqr, q3 + 1.5 * iqr
            within = hv[(hv >= lo_w) & (hv <= hi_w)]
            lf = float(within.min()) if within.size else float(hv.min())
            uf = float(within.max()) if within.size else float(hv.max())

            bx.append(f'{hour:02d}:00')
            b_q1.append(q1); b_med.append(med); b_q3.append(q3)
            # Box mean marker = LAeq (energy average), the meaningful central level for noise.
            b_lf.append(lf); b_uf.append(uf); b_mean.append(_energy_mean_db(hv))

            outliers = hv[(hv < lo_w) | (hv > hi_w)]
            if outliers.size:
                # Cap displayed outliers; show the most extreme ones.
                extreme = outliers[np.argsort(-np.abs(outliers - med))][:OUTLIER_CAP]
                out_x.extend([f'{hour:02d}:00'] * len(extreme))
                out_y.extend(float(v) for v in extreme)

        if bx:
            fig.add_trace(go.Box(
                x=bx, lowerfence=b_lf, q1=b_q1, median=b_med, q3=b_q3, upperfence=b_uf, mean=b_mean,
                name='Hourly LEQ',
                marker=dict(color='#3D5A80'),
                line=dict(color='#293241', width=1.5),
                fillcolor='rgba(61, 90, 128, 0.35)',
                hovertemplate='Hour %{x}<br>Median: %{median:.1f} dB(A)<extra></extra>',
                showlegend=False,
            ))
        if out_y:
            fig.add_trace(go.Scatter(
                x=out_x, y=out_y, mode='markers', name='Outliers',
                marker=dict(color='rgba(41,50,65,0.55)', size=4, symbol='circle-open'),
                hovertemplate='Hour %{x}<br>Outlier: %{y:.1f} dB(A)<extra></extra>',
                showlegend=False,
            ))

        fig.update_layout(
            title='Diurnal Box-and-Whisker (Hourly LEQ Volatility)',
            xaxis=dict(
                title='Hour of Day',
                categoryorder='array',
                categoryarray=hour_labels,
                tickmode='array',
                tickvals=hour_labels,
                ticktext=hour_labels,
                automargin=True,
            ),
            yaxis=dict(
                title='LEQ dB(A)',
                gridcolor='rgba(0,0,0,0.08)',
                zeroline=False,
            ),
            autosize=True,
            height=520,
            margin=dict(l=60, r=30, t=70, b=90),
            paper_bgcolor='white',
            plot_bgcolor='white',
        )
        return disclose_weather_screen(fig, self.df)
    
    def generate_violin_distribution(self, noise_col):
        """
        Create violin plot showing full distribution shape plus statistical summary
        
        Args:
            noise_col: Noise column to analyze
        """
        if noise_col not in self.df.columns:
            return None
        
        data = self.df[noise_col].dropna()
        
        fig = make_subplots(
            rows=1, cols=2,
            subplot_titles=("Noise Distribution", "Statistical Summary"),
            specs=[[{"type": "box"}, {"type": "indicator"}]]
        )
        
        # Violin plot
        fig.add_trace(
            go.Violin(
                y=data,
                name=noise_col,
                box_visible=True,
                meanline_visible=True,
                jitter=0.05,
                scalegroup="yes",
                side='negative',
                line_color='blue',
                fillcolor='rgba(102, 126, 234, 0.5)',
                hovertemplate='Level: %{y:.1f} dB<extra></extra>'
            ),
            row=1, col=1
        )
        
        # Statistics — "Mean" shown as LAeq (energy average), the correct central level.
        _laeq = _energy_mean_db(data)
        stats_text = f"""
        <b>Statistical Summary</b><br>
        Mean (LAeq): {_laeq:.1f} dB<br>
        Median: {data.median():.1f} dB<br>
        Std Dev: {data.std():.1f} dB<br>
        Min: {data.min():.1f} dB<br>
        Max: {data.max():.1f} dB<br>
        Q1: {data.quantile(0.25):.1f} dB<br>
        Q3: {data.quantile(0.75):.1f} dB<br>
        IQR: {data.quantile(0.75) - data.quantile(0.25):.1f} dB
        """
        
        fig.add_trace(
            go.Indicator(
                mode="number+gauge",
                value=_laeq,
                title={"text": "Mean Level (LAeq)"},
                domain={'x': [0, 1], 'y': [0, 1]},
                gauge={
                    'axis': {'range': [data.min()-5, data.max()+5]},
                    'bar': {'color': "darkblue"},
                    'steps': [
                        {'range': [data.min()-5, 55], 'color': 'rgba(46, 204, 113, 0.5)'},
                        {'range': [55, 65], 'color': 'rgba(241, 196, 15, 0.5)'},
                        {'range': [65, data.max()+5], 'color': 'rgba(192, 57, 43, 0.5)'}
                    ]
                }
            ),
            row=1, col=2
        )
        
        fig.update_xaxes(title_text="Distribution", row=1, col=1)
        fig.update_yaxes(title_text="Noise Level (dB)", row=1, col=1)
        
        fig.update_layout(
            title_text=f"Distribution Analysis: {noise_col}",
            height=500,
            showlegend=False,
            template='plotly_white'
        )
        
        return disclose_weather_screen(fig, self.df)
    
    def generate_cumulative_distribution(self, noise_col):
        """
        Create CDF (Cumulative Distribution Function) showing compliance fraction
        
        Args:
            noise_col: Noise column to analyze
        """
        if noise_col not in self.df.columns:
            return None
        
        data = self.df[noise_col].dropna().sort_values()
        cumsum = np.arange(1, len(data) + 1) / len(data) * 100
        
        standards = {
            '55 dB reference': 55,
            '45 dB reference': 45,
            '65 dB reference': 65,
            '75 dB reference': 75
        }
        
        fig = go.Figure()
        
        # CDF line
        fig.add_trace(go.Scatter(
            x=data,
            y=cumsum,
            name=f"{noise_col} CDF",
            mode='lines',
            line=dict(color='blue', width=3),
            fill='tozeroy',
            fillcolor='rgba(102, 126, 234, 0.2)',
            hovertemplate='Level: %{x:.1f} dB<br>% of samples ≤ this: %{y:.1f}%<extra></extra>'
        ))
        
        # Add standard lines
        colors = {'55 dB reference': 'green', '45 dB reference': 'orange',
                  '65 dB reference': 'red', '75 dB reference': 'darkred'}
        
        for std_name, limit in standards.items():
            compliance_pct = (data <= limit).sum() / len(data) * 100
            fig.add_vline(x=limit, line_dash="dash", line_color=colors[std_name],
                         annotation_text=f"{std_name}: {limit}dB({compliance_pct:.0f}%)",
                         annotation_position="top")
        
        fig.update_xaxes(title_text="Noise Level (dB)")
        fig.update_yaxes(title_text="Percentage of recorded samples at or below level (%)")
        
        fig.update_layout(
            title=f"Cumulative Distribution Function (CDF): {noise_col}",
            height=500,
            template='plotly_white',
            hovermode='x unified'
        )
        
        return disclose_weather_screen(fig, self.df)
    
    def generate_compliance_dashboard(self, noise_col=None, analysis=None):
        """Use the same metric-matched reference rows as the main dashboard."""
        from analysis.compliance_matrix import matrix_from_analysis
        from analysis.noise_analyzer import NoiseAnalyzer
        rows = matrix_from_analysis(NoiseAnalyzer(self.df).comprehensive_analysis())
        labels = [r['standard'] + ' — ' + r['metric'] for r in rows]
        fig = go.Figure()
        fig.add_bar(x=labels, y=[r['measured_db'] for r in rows], name='Measured index')
        fig.add_bar(x=labels, y=[r['limit_db'] for r in rows], name='Reference')
        fig.update_layout(title='Indicative monitoring-period reference comparisons',
                          yaxis_title='dB(A)', barmode='group', height=600)
        if not rows:
            fig.add_annotation(text='No applicable metrics available', showarrow=False)
        return disclose_weather_screen(fig, self.df)

    def generate_anomaly_detection(self, noise_col, threshold_std=2.5):
        """
        Create time series with detected anomalies highlighted
        
        Args:
            noise_col: Noise column to analyze
            threshold_std: Number of standard deviations for anomaly detection
        """
        if not self.time_col or noise_col not in self.df.columns:
            return None
        
        data = self.df[[self.time_col, noise_col]].copy()
        data = data.dropna()
        
        # Calculate rolling mean and std dev
        window = max(24, len(data) // 10)  # Rolling window
        data['rolling_mean'] = data[noise_col].rolling(window=window, center=True).mean()
        data['rolling_std'] = data[noise_col].rolling(window=window, center=True).std()
        
        # Detect anomalies
        data['anomaly'] = np.abs(data[noise_col] - data['rolling_mean']) > (threshold_std * data['rolling_std'])
        data['anomaly_type'] = 'anomaly'
        
        fig = go.Figure()
        
        # Normal data
        normal = data[~data['anomaly']]
        fig.add_trace(go.Scatter(
            x=normal[self.time_col],
            y=normal[noise_col],
            name='Normal',
            mode='markers',
            marker=dict(color='blue', size=5),
            hovertemplate='Time: %{x}<br>Level: %{y:.1f} dB<extra></extra>'
        ))
        
        # Anomalies
        anomalies = data[data['anomaly']]
        fig.add_trace(go.Scatter(
            x=anomalies[self.time_col],
            y=anomalies[noise_col],
            name='Anomaly',
            mode='markers',
            marker=dict(color='red', size=10, symbol='star'),
            hovertemplate='<b>ANOMALY</b><br>Time: %{x}<br>Level: %{y:.1f} dB<extra></extra>'
        ))
        
        # Add rolling mean and confidence band
        fig.add_trace(go.Scatter(
            x=data[self.time_col],
            y=data['rolling_mean'],
            name='Trend',
            line=dict(color='green', width=3),
            hovertemplate='Time: %{x}<br>Trend: %{y:.1f} dB<extra></extra>'
        ))
        
        # Upper and lower bounds
        upper = data['rolling_mean'] + (threshold_std * data['rolling_std'])
        lower = data['rolling_mean'] - (threshold_std * data['rolling_std'])
        
        # Build the closed band from plain lists.
        #
        # ``reversed(series)`` iterates a pandas Series by LABEL, not position.
        # After the dropna() above the index has gaps, so reversing raised
        # KeyError on the first missing label and the endpoint returned HTTP 500
        # for any file containing a gap. Convert to lists first, then reverse.
        x_fwd = data[self.time_col].tolist()
        fig.add_trace(go.Scatter(
            x=x_fwd + x_fwd[::-1],
            y=upper.tolist() + lower.tolist()[::-1],
            fill='toself',
            fillcolor='rgba(0, 255, 0, 0.2)',
            line=dict(color='rgba(0,0,0,0)'),
            hoverinfo='skip',
            name='Normal Range'
        ))
        
        fig.update_layout(
            title=f"Anomaly Detection: {noise_col}",
            xaxis_title="Time",
            yaxis_title="Noise Level (dB)",
            height=500,
            template='plotly_white',
            hovermode='x unified'
        )
        
        return disclose_weather_screen(fig, self.df)


# Usage example (for reference)
if __name__ == "__main__":
    # df = pd.read_csv('data.csv')
    # engine = EnvironmentalVisualizationEngine(df, ['Noise_Level_dB'])
    # 
    # fig1 = engine.generate_exceedance_analysis('Noise_Level_dB')
    # fig1.show()
    # 
    # fig2 = engine.generate_enhanced_temporal_heatmap('Noise_Level_dB')
    # fig2.show()
    pass
