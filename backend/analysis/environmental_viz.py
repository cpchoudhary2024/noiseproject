# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""Plotly figures for the heatmap and box-plot endpoints."""

from analysis.weather_screen import disclose_weather_screen
import pandas as pd
import numpy as np
import plotly.graph_objects as go


def _energy_mean_db(series):
    arr = pd.to_numeric(pd.Series(series).to_numpy().ravel(), errors='coerce')
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        return float('nan')
    return float(10.0 * np.log10(np.mean(np.power(10.0, arr / 10.0))))


class EnvironmentalVisualizationEngine:
    """Builds Plotly figures from the noise columns."""

    def __init__(self, df, noise_columns):
        self.df = df.copy()
        self.noise_columns = noise_columns
        self._identify_time_column()
        self._prepare_temporal_features()

    def _identify_time_column(self):
        from analysis.timestamp_utils import resolve_time_column, parse_timestamps_robust
        self.time_col = resolve_time_column(self.df)

        if self.time_col:
            self.df[self.time_col], _ = parse_timestamps_robust(self.df[self.time_col])

    def _prepare_temporal_features(self):
        if self.time_col:
            self.df['hour'] = self.df[self.time_col].dt.hour
            self.df['day_of_week'] = self.df[self.time_col].dt.dayofweek
            self.df['day_name'] = self.df[self.time_col].dt.day_name()
            self.df['date'] = self.df[self.time_col].dt.date
            self.df['week'] = self.df[self.time_col].dt.isocalendar().week

    def generate_enhanced_temporal_heatmap(self, noise_col, resolution='hourly'):
        """Hour-by-day heatmap."""

        if not self.time_col or noise_col not in self.df.columns:
            return None

        if resolution == 'hourly':
            def laeq_agg(series):
                s = pd.to_numeric(series, errors='coerce').dropna().astype(float)
                if s.empty:
                    return float('nan')
                return float(10.0 * np.log10(np.mean(np.power(10.0, s / 10.0))))

            self.df['date'] = pd.to_datetime(self.df[self.time_col]).dt.floor('D')
            pivot = self.df.pivot_table(
                values=noise_col,
                index='hour',
                columns='date',
                aggfunc=laeq_agg,
            )

            # hours 0..23 in order
            pivot = pivot.reindex(index=list(range(24)))

            # Fill in missing calendar dates so columns are continuous
            if pivot.columns.size > 0:
                min_d = pd.to_datetime(min(pivot.columns))
                max_d = pd.to_datetime(max(pivot.columns))
                all_dates = pd.date_range(min_d, max_d, freq='D')
                pivot = pivot.reindex(columns=all_dates)
            else:
                all_dates = []

            x_title = "Date"
            y_title = "Hour of day (start of hour)"
        else:
            # Day x Week heatmap, energy-average (LAeq) per cell, not arithmetic.
            pivot = self.df.pivot_table(
                values=noise_col,
                index='date',
                columns='week',
                aggfunc=_energy_mean_db
            )
            x_title = "Week Number"
            y_title = "Date"

        x_labels = [d.strftime('%Y-%m-%d') if hasattr(d, 'strftime') else str(d) for d in pivot.columns]
        if resolution == 'hourly':
            y_labels = [f'{h:02d}:00' for h in pivot.index]
            y_for_plot = y_labels
        else:
            y_for_plot = [str(d) for d in pivot.index]

        z_list = []
        for row in pivot.values:
            z_list.append([None if (v != v) else float(v) for v in row])  # NaN → None

        fig = go.Figure(data=go.Heatmap(
            z=z_list,
            x=x_labels,
            y=y_for_plot,
            # Perceptually uniform and colour-blind safe.
            colorscale='Viridis',
            colorbar=dict(title=dict(text='LAeq<br>(dB(A))'), thickness=14),
            hovertemplate='Hour: %{y}<br>Date: %{x}<br>LAeq: %{z:.1f} dB(A)<extra></extra>',
            xgap=1,
            ygap=1,
        ))

        fig.update_layout(
            xaxis_title=x_title,
            yaxis_title=y_title,
            autosize=True,
            height=560,
            margin=dict(l=70, r=30, t=20, b=80),
            font=dict(family='IBM Plex Sans, Arial, sans-serif', size=12, color='#1F2933'),
            paper_bgcolor='white',
            plot_bgcolor='white',
        )

        if resolution == 'hourly':
            # y_for_plot is a list of strings ('00:00', '01:00', …).
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
                marker=dict(color='#0072B2'),
                line=dict(color='#0072B2', width=1.2),
                fillcolor='rgba(0, 114, 178, 0.18)',
                hovertemplate='Hour %{x}<br>Median: %{median:.1f} dB(A)<extra></extra>',
                showlegend=False,
            ))
        if out_y:
            fig.add_trace(go.Scatter(
                x=out_x, y=out_y, mode='markers', name='Outliers',
                marker=dict(color='rgba(31,41,51,0.55)', size=4, symbol='circle-open'),
                hovertemplate='Hour %{x}<br>Outlier: %{y:.1f} dB(A)<extra></extra>',
                showlegend=False,
            ))

        fig.update_layout(
            xaxis=dict(
                title='Hour of day (start of hour)',
                categoryorder='array',
                categoryarray=hour_labels,
                tickmode='array',
                tickvals=hour_labels,
                ticktext=hour_labels,
                automargin=True,
            ),
            yaxis=dict(
                title='Sound level, LEQ (dB(A))',
                gridcolor='#E5E7EB',
                zeroline=False,
            ),
            autosize=True,
            height=460,
            margin=dict(l=64, r=24, t=20, b=64),
            font=dict(family='IBM Plex Sans, Arial, sans-serif', size=12, color='#1F2933'),
            paper_bgcolor='white',
            plot_bgcolor='white',
        )
        return disclose_weather_screen(fig, self.df)


# Usage example (for reference)
if __name__ == "__main__":
    pass
