"""
Enhanced Environmental Visualization Module
Implements Phase 1 quick wins for advanced environmental analysis
"""

import pandas as pd
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from datetime import datetime, timedelta
import plotly.io as pio


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
        """Identify datetime column"""
        datetime_cols = [c for c in self.df.columns 
                        if any(k in c.lower() for k in ['datetime', 'timestamp', 'time'])]
        self.time_col = datetime_cols[0] if datetime_cols else None
        
        if self.time_col:
            self.df[self.time_col] = pd.to_datetime(self.df[self.time_col], errors='coerce')
    
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
                      Default: {'Residential Day': 55, 'Residential Night': 45, 
                               'Commercial': 65, 'Industrial': 75}
        """
        if standards is None:
            standards = {
                'Residential Day': 55,
                'Residential Night': 45,
                'Commercial': 65,
                'Industrial': 75
            }
        
        if not self.time_col or noise_col not in self.df.columns:
            return None
        
        # Create hourly aggregates (use 'h' for newer pandas versions)
        hourly = self.df.groupby(self.df[self.time_col].dt.floor('h')).agg({
            noise_col: ['count', 'mean', 'max']
        }).reset_index()
        hourly.columns = ['time', 'count', 'mean', 'max']
        
        # Calculate exceedances for each standard
        fig = make_subplots(
            specs=[[{"secondary_y": True}]],
            subplot_titles=("Regulatory Exceedances Over Time",)
        )
        
        colors = ['rgba(46, 204, 113, 0.7)', 'rgba(231, 76, 60, 0.7)', 
                  'rgba(231, 76, 60, 0.8)', 'rgba(192, 57, 43, 0.8)']
        
        for (std_name, limit), color in zip(standards.items(), colors):
            exceedances = (hourly['mean'] > limit).astype(int)
            exceedance_count = exceedances.rolling(window=12).sum()  # 12-hour rolling
            
            fig.add_trace(
                go.Scatter(
                    x=hourly['time'],
                    y=exceedance_count,
                    name=f"{std_name} ({limit} dB)",
                    fill='tozeroy',
                    line=dict(color=color.replace('0.7', '1'), width=2),
                    hovertemplate='<b>%{fullData.name}</b><br>Time: %{x}<br>Exceedances: %{y}h<extra></extra>'
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
        fig.update_yaxes(title_text="Hours Exceeding Standard (12-hr window)", secondary_y=False)
        fig.update_yaxes(title_text="Noise Level (dB)", secondary_y=True)
        
        fig.update_layout(
            title="Exceedance Frequency Analysis",
            height=500,
            hovermode='x unified',
            template='plotly_white'
        )
        
        return fig
    
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
            # Day x Week heatmap
            pivot = self.df.pivot_table(
                values=noise_col,
                index='date',
                columns='week',
                aggfunc='mean'
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
        
        return fig

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
            b_lf.append(lf); b_uf.append(uf); b_mean.append(float(hv.mean()))

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
        return fig
    
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
        
        # Statistics
        stats_text = f"""
        <b>Statistical Summary</b><br>
        Mean: {data.mean():.1f} dB<br>
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
                value=data.mean(),
                title={"text": "Mean Level"},
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
        
        return fig
    
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
            'Residential Day': 55,
            'Residential Night': 45,
            'Commercial': 65,
            'Industrial': 75
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
            hovertemplate='Level: %{x:.1f} dB<br>% of time ≤ this: %{y:.1f}%<extra></extra>'
        ))
        
        # Add standard lines
        colors = {'Residential Day': 'green', 'Residential Night': 'orange', 
                  'Commercial': 'red', 'Industrial': 'darkred'}
        
        for std_name, limit in standards.items():
            compliance_pct = (data <= limit).sum() / len(data) * 100
            fig.add_vline(x=limit, line_dash="dash", line_color=colors[std_name],
                         annotation_text=f"{std_name}: {limit}dB({compliance_pct:.0f}%)",
                         annotation_position="top")
        
        fig.update_xaxes(title_text="Noise Level (dB)")
        fig.update_yaxes(title_text="Percentage of Time At or Below Level (%)")
        
        fig.update_layout(
            title=f"Cumulative Distribution Function (CDF): {noise_col}",
            height=500,
            template='plotly_white',
            hovermode='x unified'
        )
        
        return fig
    
    def generate_compliance_dashboard(self, noise_col=None, analysis=None):
        """
        Create compliance status dashboard showing current status vs limits
        
        Args:
            noise_col: Name of noise column (optional, for API compatibility)
            analysis: Analysis results dict with compliance data
        """
        if analysis is None:
            analysis = {}
        
        compliance = analysis.get('compliance', {})
        if not compliance:
            # Fallback: create simple dashboard with current data
            if noise_col and noise_col in self.df.columns:
                current_level = self.df[noise_col].mean()
            else:
                current_level = 67.0
            current_level = float(current_level) if not pd.isna(current_level) else 67.0
        else:
            # Prefer the requested column (if provided) for correctness.
            primary_col = None
            if noise_col and noise_col in compliance:
                primary_col = noise_col
            elif compliance:
                primary_col = list(compliance.keys())[0]

            if primary_col:
                current_level = compliance.get(primary_col, {}).get('current_leq')
                if current_level is None:
                    # Fallback to raw column mean if the key is missing.
                    if primary_col in self.df.columns:
                        current_level = float(self.df[primary_col].dropna().mean())
                    else:
                        current_level = 67.0
            else:
                current_level = 67.0
        
        current_level = float(current_level)
        
        # Standard limits
        standards = {
            'Residential Day': 55,
            'Residential Night': 45,
            'Commercial': 65,
            'Industrial': 75
        }
        
        # Create individual gauges and combine into HTML
        gauges = []
        for std_name, limit in standards.items():
            color = 'green' if current_level <= limit else 'red'
            delta_val = current_level - limit
            
            fig = go.Figure(data=[
                go.Indicator(
                    mode="gauge+number+delta",
                    value=current_level,
                    title={'text': std_name},
                    delta={'reference': limit, 'suffix': " dB", 'font': {'size': 14}},
                    gauge={
                        'axis': {'range': [40, 80], 'tickwidth': 1, 'tickcolor': '#999'},
                        'bar': {'color': color, 'thickness': 0.2},
                        'steps': [
                            {'range': [40, limit], 'color': 'rgba(46, 204, 113, 0.3)'},
                            {'range': [limit, 80], 'color': 'rgba(192, 57, 43, 0.3)'}
                        ],
                        'threshold': {
                            'line': {'color': 'darkred', 'width': 2},
                            'thickness': 0.75,
                            'value': limit
                        }
                    },
                    number={'suffix': " dB", 'font': {'size': 20, 'color': color}}
                )
            ])
            
            fig.update_layout(
                height=300,
                margin=dict(l=50, r=50, t=50, b=50),
                font={'size': 12}
            )
            
            gauges.append(fig)
        
        # Create combined figure using subplots with graph_objects
        from plotly.subplots import make_subplots
        
        # Create 2x2 layout
        fig = make_subplots(
            rows=2, cols=2,
            subplot_titles=list(standards.keys()),
            specs=[[{"type": "indicator"}, {"type": "indicator"}],
                   [{"type": "indicator"}, {"type": "indicator"}]],
            vertical_spacing=0.25,
            horizontal_spacing=0.15
        )
        
        # Add indicators manually (workaround for gauge+subplots)
        positions = [(1, 1), (1, 2), (2, 1), (2, 2)]
        for (std_name, limit), (row, col) in zip(standards.items(), positions):
            color = 'green' if current_level <= limit else 'red'
            
            fig.add_trace(
                go.Indicator(
                    mode="number+delta",
                    value=current_level,
                    title={'text': f"{std_name}<br>({limit} dB limit)"},
                    delta={'reference': limit, 'suffix': " dB"},
                    number={'suffix': " dB", 'font': {'size': 24, 'color': color}},
                    domain={'x': [0, 1], 'y': [0, 1]}
                ),
                row=row, col=col
            )
        
        fig.update_layout(
            title_text="Compliance Status Dashboard",
            height=600,
            showlegend=False
        )
        
        return fig
    
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
        
        fig.add_trace(go.Scatter(
            x=list(data[self.time_col]) + list(reversed(data[self.time_col])),
            y=list(upper) + list(reversed(lower)),
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
        
        return fig


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
