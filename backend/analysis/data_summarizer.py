# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
import logging
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class DataSummarizer:
    """Finds the noise and time columns and parses timestamps for the summary tables."""

    def __init__(self, df):
        self.df = df.copy()
        self.noise_columns = self._identify_noise_columns()
        self.time_col = self._identify_time_column()
        self._prepare_data()

    def _pick_first(self, cols):
        return cols[0] if cols else None

    def _ensure_datetime_column(self):
        """Return a usable datetime column."""
        if self.df.empty:
            return self.time_col

        # Identify likely date-only and time-only columns.
        date_cols = [c for c in self.df.columns if ('date' in c.lower()) and ('time' not in c.lower())]
        time_cols = [c for c in self.df.columns if ('time' in c.lower()) and ('date' not in c.lower())]

        # Prefer explicit datetime/timestamp columns if present.
        datetime_cols = [c for c in self.df.columns if any(k in c.lower() for k in ['datetime', 'timestamp'])]
        if datetime_cols:
            return datetime_cols[0]

        # If the identified time_col looks like time-only or date-only, try to combine.
        primary = self.time_col
        if primary and primary in self.df.columns:
            # Parse only a sample for heuristics to avoid expensive full-column parsing.
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
                # Avoid collisions.
                suffix = 2
                while f'{combined_name}_{suffix}' in self.df.columns:
                    suffix += 1
                combined_name = f'{combined_name}_{suffix}'

            # build "YYYY-MM-DD HH:MM:SS" strings from whatever columns exist
            date_part = pd.to_datetime(self.df[date_col], errors='coerce', cache=True).dt.strftime('%Y-%m-%d')

            time_raw = self.df[time_col]
            time_parsed = pd.to_datetime(time_raw, errors='coerce', cache=True)
            time_part = time_parsed.dt.strftime('%H:%M:%S')

            # Fallback: if parsing failed, try interpreting numeric hours.
            if time_part.isna().all():
                numeric = pd.to_numeric(time_raw, errors='coerce')
                if numeric.notna().any():
                    hours = numeric.round().astype('Int64')
                    time_part = hours.map(lambda h: f'{int(h):02d}:00:00' if pd.notna(h) else None)

            combined_str = (date_part.fillna('') + ' ' + time_part.fillna('')).str.strip()
            self.df[combined_name] = pd.to_datetime(combined_str, errors='coerce')

            # Only switch if we actually got a meaningful datetime.
            if self.df[combined_name].notna().sum() > 0:
                return combined_name

        return self.time_col

    def _identify_noise_columns(self):
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

    def _identify_time_column(self):
        from analysis.timestamp_utils import resolve_time_column
        return resolve_time_column(self.df)

    def _prepare_data(self):
        # Convert noise columns to numeric
        for col in self.noise_columns:
            self.df[col] = pd.to_numeric(self.df[col], errors='coerce')

        # make the time column a real datetime (combine Date and Time if needed)
        self.time_col = self._ensure_datetime_column()
        if self.time_col and self.time_col in self.df.columns:
            from analysis.timestamp_utils import parse_timestamps_robust
            self.df[self.time_col], _ = parse_timestamps_robust(self.df[self.time_col])
