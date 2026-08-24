# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
import logging
import pandas as pd
import numpy as np
import re
from datetime import datetime, timedelta
import io
from openpyxl import Workbook
from openpyxl.styles import PatternFill, Font, Alignment, Border, Side
from openpyxl.utils import get_column_letter

logger = logging.getLogger(__name__)


class DataSummarizer:
    """Generate daily, hourly, and weekly summaries with intelligent missing value filling"""
    
    def __init__(self, df):
        self.df = df.copy()
        self.noise_columns = self._identify_noise_columns()
        self.time_col = self._identify_time_column()
        self._prepare_data()

    def _pick_first(self, cols):
        return cols[0] if cols else None

    def _ensure_datetime_column(self):
        """Ensure we have a usable datetime column.

        Common real-world files have separate Date and Time columns. If we detect that,
        we create a combined datetime column and point self.time_col to it.
        """
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

            # Build strings like "YYYY-MM-DD HH:MM:SS" as robustly as possible.
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
        """Prefer an Leq/L_EQ-like column for summaries when available."""
        if not self.noise_columns:
            return None

        def score(col: str) -> tuple[int, int]:
            c = (col or '').lower()
            # Prefer Leq
            if 'leq' in c or 'l_eq' in c or 'l-eq' in c:
                return (0, len(c))
            # Then generic 'eq'
            if re.search(r'\beq\b', c):
                return (1, len(c))
            # Avoid Lmax/min when possible
            if 'max' in c or 'peak' in c or 'l-max' in c or 'lmax' in c:
                return (3, len(c))
            if 'min' in c or 'background' in c or 'l-min' in c or 'lmin' in c:
                return (3, len(c))
            return (2, len(c))

        # Stable sort by score then original order.
        scored = sorted(((score(c), i, c) for i, c in enumerate(self.noise_columns)), key=lambda x: (x[0], x[1]))
        return scored[0][2]
    
    def _identify_time_column(self):
        """Identify the time/date column via the shared authoritative resolver."""
        from analysis.timestamp_utils import resolve_time_column
        return resolve_time_column(self.df)
    
    def _prepare_data(self):
        """Prepare and clean data"""
        # Convert noise columns to numeric
        for col in self.noise_columns:
            self.df[col] = pd.to_numeric(self.df[col], errors='coerce')

        # Ensure time column is a real datetime (combine Date+Time if needed).
        self.time_col = self._ensure_datetime_column()
        if self.time_col and self.time_col in self.df.columns:
            # Robust parse — a naive to_datetime here silently produced a THIRD
            # timeline (different from the analyzer's and the report's) whenever
            # the source used day-first or year-first slash dates.
            from analysis.timestamp_utils import parse_timestamps_robust
            self.df[self.time_col], _ = parse_timestamps_robust(self.df[self.time_col])
    
    def fill_missing_values(self, method='none'):
        """Return the frame WITHOUT fabricating measurements (default).

        This previously interpolated across gaps of unlimited length and then
        mean-filled whatever remained, so an outage of any duration was replaced
        by invented dB values that were indistinguishable from real measurements
        in every exported summary. In a report intended for public or evidentiary
        use, a measurement that was never taken must not appear as though it was.

        Missing samples are now left as NaN and excluded from each statistic, so
        an average is computed over the data that actually exists and the sample
        count reported alongside it reflects real coverage.

        Parameters
        ----------
        method : str
            ``'none'`` (default) returns the data untouched. The interpolating
            methods are retained only for explicit, caller-driven use — never for
            report or export paths — and every filled value is recorded in
            ``df.attrs['imputed_counts']`` so it can be disclosed.

        Returns
        -------
        pd.DataFrame
            Frame with gaps preserved as NaN unless imputation was requested.
        """
        df = self.df.copy()
        if method in (None, 'none'):
            return df

        imputed: dict[str, int] = {}
        for col in self.noise_columns:
            missing = int(df[col].isna().sum())
            if missing == 0:
                continue
            imputed[col] = missing

            if method == 'interpolate':
                df[col] = df[col].interpolate(method='linear', limit_direction='both')
            elif method == 'forward_fill':
                df[col] = df[col].ffill().bfill()
            elif method == 'mean':
                df[col] = df[col].fillna(df[col].mean())
            elif method == 'combined':
                df[col] = df[col].interpolate(method='linear', limit_direction='both')
                df[col] = df[col].ffill().bfill()

        if imputed:
            df.attrs['imputed_counts'] = imputed
            logger.warning(
                "fill_missing_values(method=%r) synthesised values for: %s. "
                "These are NOT measurements and must be disclosed wherever used.",
                method, imputed,
            )
        return df
    
    def generate_hourly_summary(self):
        """Generate hourly summary with statistics"""
        df = self.fill_missing_values(method='none')
        
        if not self.time_col:
            return None
        
        df_time = df.set_index(self.time_col)
        hourly_stats = {}

        def laeq(series: pd.Series) -> float:
            s = pd.to_numeric(series, errors='coerce').dropna().astype(float)
            if s.empty:
                return float('nan')
            return float(10.0 * np.log10(np.mean(np.power(10.0, s / 10.0))))
        
        for col in self.noise_columns:
            hourly_resample = df_time[col].resample('h').agg([
                laeq, 'min', 'max', 'std', 'count'
            ]).round(2)

            hourly_resample.columns = ['LAeq (dB)', 'Min (dB)', 'Max (dB)', 'Std Dev', 'Count']
            hourly_stats[col] = hourly_resample
        
        return hourly_stats
    
    def generate_daily_summary(self):
        """Generate daily summary per day.

        Output columns match the UI requirement:
        Date, Average, Emin Leq, Max Leq, Std Dev
        """
        df = self.fill_missing_values(method='none')
        
        if not self.time_col:
            return None
        
        df_time = df.set_index(self.time_col)
        daily_stats = {}

        def laeq(series: pd.Series) -> float:
            s = pd.to_numeric(series, errors='coerce').dropna().astype(float)
            if s.empty:
                return float('nan')
            return float(10.0 * np.log10(np.mean(np.power(10.0, s / 10.0))))
        
        for col in self.noise_columns:
            daily_resample = df_time[col].resample('D').agg([laeq, 'min', 'max', 'std']).round(2)
            daily_resample.columns = ['Average', 'Emin Leq', 'Max Leq', 'Std Dev']
            daily_stats[col] = daily_resample
        
        return daily_stats
    
    def generate_weekly_summary(self):
        """Generate weekly comparison summary"""
        df = self.fill_missing_values(method='none')
        
        if not self.time_col:
            return None
        
        df_time = df.set_index(self.time_col)
        weekly_stats = {}

        def laeq(series: pd.Series) -> float:
            s = pd.to_numeric(series, errors='coerce').dropna().astype(float)
            if s.empty:
                return float('nan')
            return float(10.0 * np.log10(np.mean(np.power(10.0, s / 10.0))))
        
        for col in self.noise_columns:
            weekly_resample = df_time[col].resample('W').agg([
                laeq, 'min', 'max', 'std', 'count'
            ]).round(2)

            weekly_resample.columns = ['LAeq (dB)', 'Min (dB)', 'Max (dB)', 'Std Dev', 'Count']
            
            # Add day of week analysis
            weekly_df = pd.DataFrame(weekly_resample)
            weekly_df['Week_Start'] = weekly_df.index.strftime('%Y-%m-%d')
            weekly_df['Week_End'] = (weekly_df.index + timedelta(days=6)).strftime('%Y-%m-%d')
            
            weekly_stats[col] = weekly_df
        
        return weekly_stats
    
    def generate_hourly_excel(self):
        """Generate hourly summary as Excel with formatting"""
        hourly_data = self.generate_hourly_summary()
        
        if not hourly_data:
            return None
        
        output = io.BytesIO()
        
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            row_offset = 0
            
            for col_name, hourly_df in hourly_data.items():
                # Add title
                sheet = writer.book.create_sheet(col_name[:30][:26])  # Excel sheet name limit
                sheet.append([f'Hourly Summary - {col_name}'])
                sheet.append([f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'])
                sheet.append([])
                
                # Write DataFrame
                hourly_df_copy = hourly_df.reset_index()
                for r_idx, row in enumerate(hourly_df_copy.itertuples(index=False), 1):
                    for c_idx, value in enumerate(row, 1):
                        cell = sheet.cell(row=r_idx + 3, column=c_idx, value=value)
                        self._apply_cell_formatting(cell, value, r_idx, col_name)
                
                # Add headers
                for c_idx, col in enumerate(['Timestamp'] + list(hourly_df.columns), 1):
                    cell = sheet.cell(row=4, column=c_idx, value=col)
                    self._apply_header_formatting(cell)
                
                # Adjust column widths
                for col_idx in range(1, len(hourly_df.columns) + 2):
                    sheet.column_dimensions[get_column_letter(col_idx)].width = 15
        
        output.seek(0)
        return output
    
    def generate_daily_excel(self):
        """Generate daily summary as Excel with formatting and highlighting"""
        daily_data = self.generate_daily_summary()
        
        if not daily_data:
            return None
        
        output = io.BytesIO()
        
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            for col_name, daily_df in daily_data.items():
                # Add title sheet
                sheet = writer.book.create_sheet(col_name[:30][:26])
                sheet.append([f'Daily Summary - {col_name}'])
                sheet.append([f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'])
                sheet.append([])
                
                # Write headers
                headers = ['Date'] + list(daily_df.columns)
                for c_idx, header in enumerate(headers, 1):
                    cell = sheet.cell(row=4, column=c_idx, value=header)
                    self._apply_header_formatting(cell)
                
                # Write data with highlighting
                daily_df_copy = daily_df.reset_index()
                for r_idx, row in enumerate(daily_df_copy.itertuples(index=False), 1):
                    for c_idx, value in enumerate(row, 1):
                        cell = sheet.cell(row=r_idx + 4, column=c_idx, value=value)
                        self._apply_daily_cell_formatting(cell, value, headers[c_idx - 1])
                
                # Adjust column widths
                for col_idx in range(1, len(headers) + 1):
                    sheet.column_dimensions[get_column_letter(col_idx)].width = 15
        
        output.seek(0)
        return output
    
    def generate_weekly_excel(self):
        """Generate weekly comparison as Excel"""
        weekly_data = self.generate_weekly_summary()
        
        if not weekly_data:
            return None
        
        output = io.BytesIO()
        
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            for col_name, weekly_df in weekly_data.items():
                sheet = writer.book.create_sheet(col_name[:30][:26])
                sheet.append([f'Weekly Summary - {col_name}'])
                sheet.append([f'Generated: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'])
                sheet.append([])
                
                headers = ['Week Start', 'Week End', 'Mean (dB)', 'Min (dB)', 'Max (dB)', 'Std Dev', 'Count']
                for c_idx, header in enumerate(headers, 1):
                    cell = sheet.cell(row=4, column=c_idx, value=header)
                    self._apply_header_formatting(cell)
                
                weekly_df_copy = weekly_df.reset_index(drop=True)
                for r_idx, row in enumerate(weekly_df_copy.itertuples(index=False), 1):
                    for c_idx, value in enumerate(row, 1):
                        cell = sheet.cell(row=r_idx + 4, column=c_idx, value=value)
                        self._apply_cell_formatting(cell, value, r_idx, col_name)
                
                for col_idx in range(1, len(headers) + 1):
                    sheet.column_dimensions[get_column_letter(col_idx)].width = 15
        
        output.seek(0)
        return output
    
    def _apply_header_formatting(self, cell):
        """Apply header formatting"""
        cell.font = Font(bold=True, color="FFFFFF", size=11)
        cell.fill = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        cell.border = border
    
    def _apply_cell_formatting(self, cell, value, row_idx, col_name):
        """Apply cell formatting based on value"""
        # Basic alignment and border
        cell.alignment = Alignment(horizontal="center", vertical="center")
        border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        cell.border = border
        
        # Highlight high values
        if isinstance(value, (int, float)) and not np.isnan(value):
            if value > 80:
                cell.fill = PatternFill(start_color="C00000", end_color="C00000", fill_type="solid")
                cell.font = Font(color="FFFFFF", bold=True)
            elif value > 70:
                cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
            elif value > 60:
                cell.fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
    
    def _apply_daily_cell_formatting(self, cell, value, header):
        """Apply cell formatting for daily summary"""
        cell.alignment = Alignment(horizontal="center", vertical="center")
        border = Border(
            left=Side(style='thin'),
            right=Side(style='thin'),
            top=Side(style='thin'),
            bottom=Side(style='thin')
        )
        cell.border = border
        
        # Highlight based on column type
        if 'Compliance' in header:
            if value == 'FAIL':
                cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                cell.font = Font(color="9C0006", bold=True)
            elif value == 'PASS':
                cell.fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")
                cell.font = Font(color="006100", bold=True)
        elif 'Peak' in header:
            if isinstance(value, (int, float)) and value > 10:
                cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
        elif 'Mean' in header or 'Max' in header:
            if isinstance(value, (int, float)) and not np.isnan(value):
                if value > 80:
                    cell.fill = PatternFill(start_color="C00000", end_color="C00000", fill_type="solid")
                    cell.font = Font(color="FFFFFF", bold=True)
                elif value > 70:
                    cell.fill = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
                elif value > 60:
                    cell.fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")
