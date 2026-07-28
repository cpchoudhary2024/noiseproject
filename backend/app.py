from flask import Flask, render_template, request, jsonify, send_file
from flask_cors import CORS
import os
import pandas as pd
import numpy as np
from datetime import datetime
import json
import traceback
import re
from werkzeug.utils import secure_filename
from analysis.noise_analyzer import NoiseAnalyzer
from analysis.report_generator import ReportGenerator
from analysis.report_generator_v2 import ReportGeneratorV2
from analysis.docx_generator import WordReportGenerator
from analysis.iso_epa_standards import StandardsAnalyzer
from analysis.data_summarizer import DataSummarizer
from analysis.chart_generator import AdvancedChartGenerator
from analysis.environmental_viz import EnvironmentalVisualizationEngine
from analysis.environmental_metrics import EnvironmentalMetricsCalculator
from analysis.wlg_parser import parse_wlg_file, WLGParser
from analysis.gap_detector import (detect_gaps, gap_report_to_dict, merge_dataframes,
                                   data_completeness_pct, _modal_interval_seconds)
from analysis.compliance_matrix import evaluate_compliance
from analysis.acoustics import energetic_mean_db, compute_ldn_lden, energy_concentration
from analysis.timestamp_utils import (assess_timestamp_integrity, primary_time_column,
                                      parse_timestamps_robust, resolve_time_column)
import io
import logging
import threading
from collections import OrderedDict
import math
import uuid

import retention

# Setup logging for cache debugging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

# ── Server-side progress store ────────────────────────────────────────────────
# Maps job_id -> {pct: int, msg: str}. Frontend polls /api/progress/<job_id>.
_progress_store: dict = {}
_progress_lock = threading.Lock()

def _set_progress(job_id: str, pct: int, msg: str):
    if not job_id:
        return
    with _progress_lock:
        _progress_store[job_id] = {'pct': int(pct), 'msg': msg}
        if len(_progress_store) > 200:
            oldest = list(_progress_store.keys())[:-100]
            for k in oldest:
                del _progress_store[k]


OLE_XLS_SIGNATURE = b"\xD0\xCF\x11\xE0\xA1\xB1\x1A\xE1"  # Excel 97-2003 .xls (OLE CF)
ZIP_SIGNATURE = b"PK\x03\x04"  # .xlsx (ZIP container)


def _read_file_head(filepath, size=4096):
    with open(filepath, 'rb') as f:
        return f.read(size)


def _looks_like_text(data: bytes) -> bool:
    if not data:
        return False
    if b"\x00" in data:
        return False
    sample = data[:2048]
    printable = sum(1 for b in sample if b in b"\t\n\r" or 32 <= b <= 126)
    return printable / max(1, len(sample)) > 0.9

# Get the backend directory
backend_dir = os.path.dirname(os.path.abspath(__file__))
# Get the parent directory (noise-analysis-platform root)
root_dir = os.path.dirname(backend_dir)

# Initialize Flask with correct template and static folders
app = Flask(__name__,
            template_folder=os.path.join(root_dir, 'frontend', 'templates'),
            static_folder=os.path.join(root_dir, 'frontend', 'static'))
CORS(app)

# Configuration
# Use normalized absolute paths to avoid issues like backend/../uploads in responses.
# On Vercel (read-only FS), set UPLOAD_FOLDER / ARTIFACTS_DIR env vars to /tmp/...
_default_upload = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'uploads'))
_default_artifacts = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'artifacts'))
UPLOAD_FOLDER = os.environ.get('UPLOAD_FOLDER') or _default_upload
RAW_UPLOAD_FOLDER = os.path.join(UPLOAD_FOLDER, 'raw')
ARTIFACTS_DIR = os.environ.get('ARTIFACTS_DIR') or _default_artifacts
ARTIFACTS_REPORTS_DIR = os.path.join(ARTIFACTS_DIR, 'reports')
ARTIFACTS_CHARTS_DIR = os.path.join(ARTIFACTS_DIR, 'charts')
ALLOWED_EXTENSIONS = {'csv', 'xlsx', 'xls', 'wlg'}
MAX_UPLOAD_MB = int(os.environ.get('UPLOAD_MAX_MB', '200'))
MAX_FILE_SIZE = MAX_UPLOAD_MB * 1024 * 1024

app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['RAW_UPLOAD_FOLDER'] = RAW_UPLOAD_FOLDER
app.config['ARTIFACTS_DIR'] = ARTIFACTS_DIR
app.config['ARTIFACTS_REPORTS_DIR'] = ARTIFACTS_REPORTS_DIR
app.config['ARTIFACTS_CHARTS_DIR'] = ARTIFACTS_CHARTS_DIR
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE
# Never cache static files — ensures browsers always load the latest JS/CSS
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# In-memory cache to avoid re-reading and re-analyzing the same file.
#
# Entries hold full DataFrames — a single 11-day 1 Hz record is ~950k rows and
# tens of MB. An unbounded dict therefore grows until the process is OOM-killed,
# which for a shared portal means the server dies for everyone as soon as enough
# distinct files have been opened. Bounded by both entry count and an approximate
# memory budget, evicting least-recently-used first, under a lock so concurrent
# requests cannot interleave a read and an eviction.
_DATA_CACHE: "OrderedDict[str, dict]" = OrderedDict()
_CACHE_LOCK = threading.RLock()
_CACHE_MAX_ENTRIES = int(os.environ.get('NOISE_CACHE_MAX_ENTRIES', '6'))
_CACHE_MAX_BYTES = int(os.environ.get('NOISE_CACHE_MAX_MB', '1024')) * 1024 * 1024


def _entry_nbytes(entry: dict) -> int:
    """Approximate resident size of a cache entry, in bytes."""
    df = entry.get('df')
    try:
        return int(df.memory_usage(deep=False).sum()) if df is not None else 0
    except Exception:
        return 0


def _evict_if_needed() -> None:
    """Drop least-recently-used entries until the cache is within budget.

    Caller must hold ``_CACHE_LOCK``.
    """
    while len(_DATA_CACHE) > _CACHE_MAX_ENTRIES:
        path, entry = _DATA_CACHE.popitem(last=False)
        logger.info("[CACHE] EVICT (entry count): %s (~%.1f MB)",
                    path, _entry_nbytes(entry) / 1e6)

    total = sum(_entry_nbytes(e) for e in _DATA_CACHE.values())
    while total > _CACHE_MAX_BYTES and len(_DATA_CACHE) > 1:
        path, entry = _DATA_CACHE.popitem(last=False)
        freed = _entry_nbytes(entry)
        total -= freed
        logger.info("[CACHE] EVICT (memory budget): %s (~%.1f MB freed)", path, freed / 1e6)

# Bump this when parser/analysis behavior changes in a way that should invalidate
# cached results (e.g., WLG decode/scaling fixes).
_CACHE_VERSION = "2026-06-18-ts-yearfirst-forced-v5"


def _cache_mtime(filepath: str) -> float | None:
    try:
        return os.path.getmtime(filepath)
    except OSError:
        return None


def _get_cache_entry(filepath: str):
    abs_path = os.path.abspath(filepath)
    with _CACHE_LOCK:
        entry = _DATA_CACHE.get(abs_path)
        if not entry:
            logger.info(f"[CACHE] MISS: {abs_path} - not in cache")
            return None

        if entry.get('cache_version') != _CACHE_VERSION:
            logger.info(f"[CACHE] MISS: {abs_path} - cache version changed")
            _DATA_CACHE.pop(abs_path, None)
            return None

        current_mtime = _cache_mtime(abs_path)
        stored_mtime = entry.get('mtime')
        if stored_mtime != current_mtime:
            logger.info(f"[CACHE] MISS: {abs_path} - file modified (stored: {stored_mtime}, current: {current_mtime})")
            _DATA_CACHE.pop(abs_path, None)
            return None

        # Mark as most-recently-used so eviction drops genuinely cold entries.
        _DATA_CACHE.move_to_end(abs_path)
        logger.info(f"[CACHE] HIT: {abs_path} - cache valid")
        return entry


def _store_cache_entry(filepath: str, df=None, analysis=None, standards=None, daily_summary=None, hourly_summary=None):
    abs_path = os.path.abspath(filepath)
    mtime = _cache_mtime(abs_path)
    with _CACHE_LOCK:
        # Preserve anything already cached for this file that this call does not
        # supply, so storing an analysis does not silently discard the DataFrame.
        existing = _DATA_CACHE.get(abs_path) or {}
        merged = {
            'cache_version': _CACHE_VERSION,
            'mtime': mtime,
            'df': df if df is not None else existing.get('df'),
            'analysis': analysis if analysis is not None else existing.get('analysis'),
            'standards': standards if standards is not None else existing.get('standards'),
            'daily_summary': daily_summary if daily_summary is not None else existing.get('daily_summary'),
            'hourly_summary': hourly_summary if hourly_summary is not None else existing.get('hourly_summary'),
        }
        # A stale-mtime entry must not keep results computed from older content.
        if existing and existing.get('mtime') != mtime:
            merged.update({k: locals()[k] for k in
                           ('df', 'analysis', 'standards', 'daily_summary', 'hourly_summary')})
        _DATA_CACHE[abs_path] = merged
        _DATA_CACHE.move_to_end(abs_path)
        _evict_if_needed()

    logger.info(
        f"[CACHE] STORE: {abs_path} - mtime={mtime}, has_df={merged['df'] is not None}, "
        f"has_analysis={merged['analysis'] is not None}, has_standards={merged['standards'] is not None}, "
        f"has_daily_summary={merged['daily_summary'] is not None}, "
        f"has_hourly_summary={merged['hourly_summary'] is not None}, "
        f"entries={len(_DATA_CACHE)}"
    )


def _get_cached_df(filepath: str) -> pd.DataFrame:
    entry = _get_cache_entry(filepath)
    if entry and entry.get('df') is not None:
        return entry['df']
    df = read_input_file(filepath)
    _store_cache_entry(filepath, df=df)
    return df


@app.errorhandler(413)
def request_entity_too_large(e):
    return jsonify({
        'error': f'File too large. Max upload size is {MAX_UPLOAD_MB} MB.'
    }), 413

# Create storage folders if they don't exist
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(RAW_UPLOAD_FOLDER, exist_ok=True)
os.makedirs(ARTIFACTS_REPORTS_DIR, exist_ok=True)
os.makedirs(ARTIFACTS_CHARTS_DIR, exist_ok=True)


def _apply_temporal_filters(df: pd.DataFrame, filters: dict | None) -> pd.DataFrame:
    """Apply user-defined temporal exclusions and date bounds to produce clean_df.

    Args:
        filters: dict with keys:
            bound_start  – ISO string: keep rows on or after this datetime
            bound_end    – ISO string: keep rows on or before this datetime (inclusive day)
            exclusions   – list of {start, end} dicts (ISO strings) to DROP from the data

    All analysis, visualisation, and compliance checks must run on the returned
    DataFrame so that results perfectly reflect the filtered data.
    """
    if not filters or df.empty:
        return df

    time_col = resolve_time_column(df)
    if not time_col:
        return df
    ts, _ = parse_timestamps_robust(df[time_col])
    valid_ts = ts.notna()

    keep = pd.Series(True, index=df.index)

    # --- Bounding: restrict to [bound_start, bound_end] ---
    bound_start = filters.get('bound_start')
    bound_end   = filters.get('bound_end')
    if bound_start:
        try:
            start_dt = pd.to_datetime(bound_start)
            keep &= (~valid_ts) | (ts >= start_dt)
        except Exception:
            pass
    if bound_end:
        try:
            # Include the full last day
            end_dt = pd.to_datetime(bound_end) + pd.Timedelta(days=1) - pd.Timedelta(seconds=1)
            keep &= (~valid_ts) | (ts <= end_dt)
        except Exception:
            pass

    # --- Exclusions: drop rows within each bad window ---
    for excl in (filters.get('exclusions') or []):
        excl_start = excl.get('start')
        excl_end   = excl.get('end')
        if excl_start and excl_end:
            try:
                es = pd.to_datetime(excl_start)
                ee = pd.to_datetime(excl_end)
                keep &= (~valid_ts) | ~((ts >= es) & (ts <= ee))
            except Exception:
                pass

    clean_df = df[keep].copy()
    return clean_df if not clean_df.empty else df


def _resolve_uploaded_filepath(filepath: str) -> str:
    """Resolve a client-provided filepath, constrained to server-managed folders.

    Security: the client only ever supplies paths the server itself handed out
    (under uploads/ or artifacts/). A bare basename is looked up inside the
    upload folders; a full path is honoured ONLY if it normalises to a location
    inside an allowed root. Anything else (``/etc/passwd``, ``../../secret``) is
    rejected by falling back to a basename lookup in uploads/raw, which will not
    exist and yields a clean 404 — preventing arbitrary file read / path traversal.
    """
    filepath = str(filepath or '')

    # Roots the client is permitted to reference.
    allowed_roots = [
        os.path.abspath(app.config['RAW_UPLOAD_FOLDER']),
        os.path.abspath(app.config['UPLOAD_FOLDER']),
        os.path.abspath(ARTIFACTS_REPORTS_DIR),
        os.path.abspath(ARTIFACTS_CHARTS_DIR),
    ]

    def _within_allowed(p: str) -> bool:
        # realpath, not abspath: abspath normalises '..' but does not follow
        # symlinks, so a symlink planted inside uploads/ could still point at an
        # arbitrary file outside the allowed roots.
        ap = os.path.realpath(p)
        return any(ap == os.path.realpath(root) or ap.startswith(os.path.realpath(root) + os.sep)
                   for root in allowed_roots)

    # Bare basename → look up inside the upload folders only.
    if os.path.basename(filepath) == filepath:
        raw_candidate = os.path.abspath(os.path.join(app.config['RAW_UPLOAD_FOLDER'], filepath))
        if os.path.exists(raw_candidate):
            return raw_candidate
        return os.path.abspath(os.path.join(app.config['UPLOAD_FOLDER'], filepath))

    # Full/relative path → honour only if it resolves inside an allowed root.
    if _within_allowed(filepath):
        return os.path.abspath(filepath)

    # Reject traversal: treat as a basename inside uploads/raw (non-existent → 404).
    return os.path.abspath(os.path.join(app.config['RAW_UPLOAD_FOLDER'], os.path.basename(filepath)))


def _run_retention_cleanup(*, keep_paths=()):
    """Best-effort cleanup of generated artifacts.

    Keeps the most recent N artifacts (default 15) per category in uploads/.
    Never raises: cleanup must not affect core app functionality.
    """
    try:
        policy = retention.RetentionPolicy(
            keep_raw_uploads=int(os.environ.get('RETENTION_KEEP_RAW', '15')),
            keep_charts_html=int(os.environ.get('RETENTION_KEEP_CHARTS', '15')),
            keep_reports=int(os.environ.get('RETENTION_KEEP_REPORTS', '15')),
            enabled=os.environ.get('RETENTION_ENABLED', '1') not in {'0', 'false', 'False'},
        )
        retention.enforce_retention(
            uploads_dir=UPLOAD_FOLDER,
            root_dir=root_dir,
            raw_uploads_dir=RAW_UPLOAD_FOLDER,
            artifacts_reports_dir=ARTIFACTS_REPORTS_DIR,
            artifacts_charts_dir=ARTIFACTS_CHARTS_DIR,
            policy=policy,
            keep_paths=keep_paths,
        )
    except Exception:
        # Never break requests due to cleanup logic.
        logger.info("[RETENTION] Cleanup skipped due to error")

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def _detect_delimiter(sample_line: str) -> str:
    """Pick the most likely column delimiter from a header/sample line.

    Many logger exports masquerading as ``.xls`` are actually tab-separated text;
    others use ',' or ';'. We choose the candidate with the highest count on the
    header line, defaulting to ',' when none is present.
    """
    candidates = ('\t', ';', ',', '|')
    counts = {d: sample_line.count(d) for d in candidates}
    best = max(counts, key=counts.get)
    return best if counts[best] > 0 else ','


def _expand_tied_timestamps(ts: pd.Series) -> pd.Series:
    """Give every row a distinct, monotonic timestamp when the source stored
    sub-interval samples at a coarser time resolution.

    Some logger/Excel exports truncate the time column to whole minutes, so all
    ~60 one-second samples in a minute share the stamp ``HH:MM:00``. Treating
    those as duplicates and dropping them would silently discard 59/60 of the
    data. Instead we spread each run of identical timestamps across the interval
    up to the next distinct timestamp, reconstructing the native cadence
    (e.g. 1 Hz). **No row is ever added or removed.**

    Behaviour:
      * Already-unique timestamps → returned unchanged (fast no-op).
      * A full block (e.g. 60 ties before the next minute) → evenly spread to the
        native interval (``:00 … :59``), staying contiguous with the next block
        so no spurious gap is introduced.
      * A block followed by a genuine gap → spread only at the native interval so
        the real gap is preserved (never smeared across the gap).
      * NaT values are preserved in place.

    Assumes rows are in recorded (chronological) order, which is true for these
    loggers; the merge step re-sorts globally afterwards.

    Parameters
    ----------
    ts : pd.Series
        Datetime series in recorded order.

    Returns
    -------
    pd.Series
        Datetime series, strictly increasing within each block, ties expanded.
    """
    ts = pd.to_datetime(ts, errors='coerce')
    if len(ts) < 2:
        return ts
    valid = ts.notna()
    if int(valid.sum()) < 2 or int(ts[valid].duplicated().sum()) == 0:
        return ts  # nothing tied → no reconstruction needed

    work = ts.reset_index(drop=True)
    block = work.ne(work.shift()).cumsum()          # new id at each timestamp change

    starts = work.groupby(block).first()
    sizes = work.groupby(block).size().astype('float64')
    widths = (starts.shift(-1) - starts).dt.total_seconds()   # NaN for the final block

    positive = widths[widths > 0]
    if not positive.empty:
        modal = positive.mode()
        modal_width = float(modal.iloc[0]) if not modal.empty else float(positive.median())
    else:
        modal_width = 1.0
    size_mode = sizes.mode()
    modal_size = float(size_mode.iloc[0]) if not size_mode.empty else 1.0
    native_interval = (modal_width / modal_size) if modal_size > 0 else 1.0

    # Fill the open-ended final block's width with the modal block width.
    widths = widths.fillna(modal_width)

    # Lay each block's samples at the native interval (e.g. 1 s) — never invent a
    # finer-than-native cadence. A short block that is contiguous with the next
    # block (a partial first/early minute → recording started mid-minute) is
    # anchored to that block's END (e.g. :32 … :59) so no spurious internal gap
    # appears. The final block, and any block sitting before a genuine gap, are
    # anchored to the START so real gaps are preserved.
    is_last = pd.Series(False, index=sizes.index)
    if len(is_last):
        is_last.iloc[-1] = True
    contiguous = (widths <= modal_width * 1.5) & (~is_last) & (sizes > 1)
    fits = (sizes * native_interval) <= widths

    spacing = pd.Series(native_interval, index=sizes.index, dtype='float64')
    base = pd.Series(0.0, index=sizes.index, dtype='float64')
    end_anchor = contiguous & fits
    base[end_anchor] = widths[end_anchor] - sizes[end_anchor] * native_interval
    # Over-dense block (more samples than fit at native spacing) → compress evenly.
    compress = contiguous & (~fits)
    spacing[compress] = widths[compress] / sizes[compress]

    k = work.groupby(block).cumcount().astype('float64')
    offset_s = block.map(base).astype('float64') + k * block.map(spacing).astype('float64')
    expanded = block.map(starts) + pd.to_timedelta(offset_s, unit='s')
    expanded = expanded.where(work.notna(), other=pd.NaT)
    expanded.index = ts.index
    return expanded


def read_excel_file(filepath):
    """Read Excel file with content-based engine detection (.xls vs .xlsx).

    Also reconstructs a unique, monotonic timestamp when the workbook stored
    sub-minute samples at minute resolution, so downstream code never has to drop
    "duplicate" rows.
    """
    head = _read_file_head(filepath, size=16)
    if head.startswith(OLE_XLS_SIGNATURE):
        df = pd.read_excel(filepath, engine='xlrd')
    elif head.startswith(ZIP_SIGNATURE):
        df = pd.read_excel(filepath, engine='openpyxl')
    else:
        raise ValueError(
            "Unsupported or corrupt Excel file. The file does not look like a real .xls or .xlsx. "
            "If you renamed the file extension, please re-save it as a true .xlsx or .csv."
        )
    df = _flag_spreadsheet_row_limit_truncation(df, filepath)
    return _maybe_add_absolute_timestamp(df, None, filepath=filepath)


# Worksheet row ceilings. A logger export that lands within a couple of rows of
# one of these was almost certainly cut off by the spreadsheet, not by the device.
_XLSX_ROW_LIMIT = 1_048_576   # Excel 2007+ (.xlsx)
_XLS_ROW_LIMIT = 65_536       # Excel 97-2003 (.xls)
_ROW_LIMIT_TOLERANCE = 5      # allow for header + metadata rows


def _flag_spreadsheet_row_limit_truncation(df: pd.DataFrame, filepath: str) -> pd.DataFrame:
    """Detect silent truncation at a spreadsheet row ceiling.

    Excel drops every row past its worksheet limit **without warning**. Several
    files in this project sit at exactly 1,048,573 data rows: days of monitoring
    were lost at export, yet the file opens cleanly and reports a shorter period
    than its own filename claims. Reporting that period as the monitoring window
    would understate coverage in a document intended for public release.

    Records the finding in ``df.attrs['ingest_warnings']`` and logs it. Does not
    raise: the surviving data is still valid, it is the *extent* that is wrong.
    """
    n = len(df)
    warnings_found: list[str] = []
    for limit, label in ((_XLSX_ROW_LIMIT, 'xlsx'), (_XLS_ROW_LIMIT, 'xls')):
        if limit - _ROW_LIMIT_TOLERANCE - 1 <= n <= limit:
            warnings_found.append(
                f"This file contains {n:,} rows, which is exactly the {label.upper()} worksheet "
                f"limit of {limit:,}. Excel discards every row beyond that limit without warning, "
                f"so this export is truncated: the recording continued past the last timestamp "
                f"shown. All results below describe the period actually present in the file, which "
                f"is shorter than the deployment. Where the original logger data is no longer "
                f"available the shortfall cannot be recovered and should be stated as a coverage "
                f"limitation; where it is, re-export as CSV, which has no row limit."
            )
            logger.warning(
                "[INGEST] Row-limit truncation suspected in %s: %d rows (%s limit %d)",
                os.path.basename(filepath), n, label, limit
            )
            break

    if warnings_found:
        existing = list(df.attrs.get('ingest_warnings', []))
        df.attrs['ingest_warnings'] = existing + warnings_found
        df.attrs['truncated_at_row_limit'] = True
    return df


def read_input_file(filepath):
    """Read CSV, Excel, or WLG by sniffing the actual file format.

    This avoids failures when users upload files with the wrong extension (e.g. a CSV renamed to .xls).
    Supports:
    - CSV files
    - Excel files (.xls, .xlsx)
    - WLG files (Larson Davis sound meter data)
    """
    head = _read_file_head(filepath)
    
    # Check for Excel formats first
    if head.startswith(OLE_XLS_SIGNATURE) or head.startswith(ZIP_SIGNATURE):
        return read_excel_file(filepath)
    
    # Check for text-based formats (CSV)
    if _looks_like_text(head):
        return read_csv_file(filepath)
    
    # Check for WLG binary format
    if WLGParser.is_wlg_file(filepath):
        logger.info(f"Detected WLG file format: {filepath}")
        return parse_wlg_file(filepath)
    
    raise ValueError(
        "Unsupported format, or corrupt file. "
        "Please upload a valid .csv, .xls, .xlsx, or .wlg (Larson Davis) file."
    )


def _sniff_text_lines(filepath, max_bytes=64 * 1024, max_lines=50):
    data = _read_file_head(filepath, size=max_bytes)
    # utf-8-sig strips a BOM if present
    text = data.decode('utf-8-sig', errors='replace')
    lines = text.splitlines()
    return lines[:max_lines]


def _detect_csv_header_row(lines):
    """Return skiprows index for the header row.

    Some logger exports include metadata lines (e.g. 'trial ...') and blanks before the real header.
    We look for a row that contains 'time' and at least one noise metric hint.
    """
    for idx, line in enumerate(lines):
        l = (line or '').strip().lower()
        if not l:
            continue
        if 'time' in l and (('leq' in l) or ('db' in l) or ('dba' in l) or ('l-max' in l) or ('l-min' in l)):
            return idx
        # Fallback: a wide-ish header row with multiple commas and metric hints.
        if l.count(',') >= 2 and (('leq' in l) or ('db' in l)):
            return idx
    return 0


def _parse_trial_start_datetime(lines):
    """Parse start datetime from a metadata line like: 'trial 2_2026_03_01__11h24m44s'."""
    trial_re = re.compile(r'(\d{4})_(\d{2})_(\d{2})__([0-9]{1,2})h([0-9]{2})m([0-9]{2})s')
    for line in lines:
        if not line:
            continue
        m = trial_re.search(line)
        if not m:
            continue
        y, mo, d, hh, mm, ss = map(int, m.groups())
        try:
            return datetime(y, mo, d, hh, mm, ss)
        except ValueError:
            return None
    return None


def _parse_date_range_from_filename(filename: str) -> tuple[datetime | None, datetime | None]:
    """Best-effort parsing of a start/end date from a filename.

    Examples seen in this project:
    - '1_MARCH-_4MARCH_11.22-20.15.csv'
    - '21_feb-_12_march.csv'
    - '3.12-3.16.csv' (month.day-month.day)

    Returns (start_date, end_date) as naive datetimes at midnight.
    """
    name = os.path.splitext(os.path.basename(filename))[0]
    name = re.sub(r'^\d{8}_\d{6}_', '', name)  # strip upload prefix
    lower = name.lower()

    month_map = {
        'jan': 1, 'january': 1,
        'feb': 2, 'february': 2,
        'mar': 3, 'march': 3,
        'apr': 4, 'april': 4,
        'may': 5,
        'jun': 6, 'june': 6,
        'jul': 7, 'july': 7,
        'aug': 8, 'august': 8,
        'sep': 9, 'sept': 9, 'september': 9,
        'oct': 10, 'october': 10,
        'nov': 11, 'november': 11,
        'dec': 12, 'december': 12,
    }

    def _year_guess() -> int:
        # Heuristic: most sample datasets here are 2026.
        # Prefer current year, but keep deterministic.
        return datetime.now().year

    # Pattern: 21_feb-_12_march
    m = re.search(r'(?P<d1>\d{1,2})\s*[_\-\. ]*(?P<m1>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s*[_\- ]*[_\-]?\s*(?P<d2>\d{1,2})\s*[_\-\. ]*(?P<m2>jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:t|tember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)', lower)
    if m:
        y = _year_guess()
        d1 = int(m.group('d1'))
        d2 = int(m.group('d2'))
        mo1 = month_map.get(m.group('m1'), None)
        mo2 = month_map.get(m.group('m2'), None)
        if mo1 and mo2:
            try:
                return datetime(y, mo1, d1), datetime(y, mo2, d2)
            except ValueError:
                return None, None

    # Pattern: 3.12-3.16 (month.day-month.day)
    m = re.search(r'(?P<m1>\d{1,2})\.(?P<d1>\d{1,2})\s*[-_ ]\s*(?P<m2>\d{1,2})\.(?P<d2>\d{1,2})', lower)
    if m:
        y = _year_guess()
        try:
            return (
                datetime(y, int(m.group('m1')), int(m.group('d1'))),
                datetime(y, int(m.group('m2')), int(m.group('d2'))),
            )
        except ValueError:
            return None, None

    return None, None


def _parse_start_time_from_filename(filename: str) -> tuple[int | None, int | None]:
    """Extract a start clock time from a filename, e.g. '... 11.22-20.15' -> (11, 22).

    Several exports encode the recording window in the filename as
    ``HH.MM-HH.MM``. Using it beats anchoring at midnight, which fabricates a
    time of day and shifts every sample into the wrong day/night window.

    Only accepts values that are valid clock times AND where the second value
    reads as a later time than the first, so a ``month.day-month.day`` range is
    not mistaken for a time range.

    Returns
    -------
    tuple[int | None, int | None]
        ``(hour, minute)`` of the start time, or ``(None, None)``.
    """
    name = os.path.splitext(os.path.basename(filename or ''))[0]
    name = re.sub(r'^\d{8}_\d{6}_', '', name)

    for m in re.finditer(r'(?<!\d)(\d{1,2})\.(\d{2})\s*[-–]\s*(\d{1,2})\.(\d{2})(?!\d)', name):
        h1, m1, h2, m2 = (int(g) for g in m.groups())
        if not (0 <= h1 <= 23 and 0 <= m1 <= 59 and 0 <= h2 <= 23 and 0 <= m2 <= 59):
            continue
        # A same-day recording window runs forwards. Reject date-like pairs
        # (e.g. '03.12-03.15', where both halves are equal-hour month.day).
        if (h2, m2) <= (h1, m1):
            continue
        return h1, m1
    return None, None


def _maybe_add_absolute_timestamp(
    df: pd.DataFrame,
    start_dt: datetime | None,
    filepath: str | None = None,
) -> pd.DataFrame:
    """Synthesize an absolute Timestamp column when files lack one.

    Supports common logger exports where the "Time" column is a *minute:second* clock
    within an hour (MM:SS(.ms)), often paired with a 'trial ...YYYY_MM_DD__HHhMMmSSs'
    metadata line that provides the absolute start datetime.

    If start_dt is missing, we optionally fall back to date hints in the filename.
    """
    if df.empty:
        return df

    # Choose the primary time column: prefer an explicit timestamp/datetime,
    # else any column mentioning 'time', else the first column.
    time_col = next((c for c in df.columns
                     if any(k in c.lower() for k in ['timestamp', 'datetime'])), None)
    if time_col is None:
        time_col = next((c for c in df.columns if 'time' in c.lower()), None)
    if time_col is None:
        time_col = df.columns[0]

    col = df[time_col]

    # Absolute datetimes (Excel datetime cells, or ISO/date strings):
    # normalise, expand any minute-collapsed ties into a unique timeline, and
    # write back IN PLACE so every downstream time-column selector is consistent.
    if pd.api.types.is_datetime64_any_dtype(col):
        parsed = pd.to_datetime(col, errors='coerce')
    else:
        s = col.astype(str).str.strip()
        # Robust parse — auto-detects year-first ('2026/05/12') vs day-first and
        # falls back to explicit formats, so slash dates with day > 12 are not
        # silently turned into NaT by a wrong dayfirst guess.
        parsed, _ = parse_timestamps_robust(col)
        # Only treat as absolute when the raw text actually looks like calendar
        # dates (a 4-digit year or a d/d separator) — not a MM:SS / HH:MM:SS
        # elapsed clock that pandas happened to coerce.
        looks_absolute = (
            float(parsed.notna().mean()) >= 0.9
            and float(s.str.contains(r'\d{4}|\d{1,2}[/-]\d{1,2}', regex=True, na=False).mean()) >= 0.5
        )
        if not looks_absolute:
            parsed = None

    if parsed is not None and float(parsed.notna().mean()) >= 0.9:
        expanded = _expand_tied_timestamps(parsed)
        df = df.copy()
        df[time_col] = expanded
        df['Timestamp'] = expanded
        return df

    s = df[time_col].astype(str).str.strip()

    # Try MM:SS(.sss)
    mmss = s.str.extract(r'^(?P<m>\d+):(?P<s>\d+(?:\.\d+)?)$')
    mmss_match = float(mmss.notna().all(axis=1).mean())
    if mmss_match >= 0.8:
        minutes = pd.to_numeric(mmss['m'], errors='coerce')
        seconds = pd.to_numeric(mmss['s'], errors='coerce')

        # If minutes exceed 59, treat as elapsed minutes since start.
        if minutes.notna().any() and float(minutes.max()) > 59:
            if start_dt is None:
                return df
            offset_seconds = minutes * 60 + seconds
            df = df.copy()
            df['Timestamp'] = start_dt + pd.to_timedelta(offset_seconds, unit='s')
            return df

        within = (minutes * 60 + seconds).astype('Float64')

        # Choose a base datetime.
        base_dt = start_dt
        tod_known = start_dt is not None
        if base_dt is None and filepath:
            start_date, _end_date = _parse_date_range_from_filename(filepath)
            if start_date is not None:
                # The filename gives a DATE but no time of day. Anchoring at
                # hour 0 invents a clock: one file here truly began at 11:24 and
                # was placed at 00:24, shifting every sample 11 hours and moving
                # daytime measurements into the night window — which silently
                # corrupts Lnight, Ldn/Lden and the whole diurnal profile.
                #
                # Prefer a start time parsed from the filename when present;
                # otherwise anchor the date but mark the time of day as
                # unreliable so hour-dependent metrics can be suppressed rather
                # than fabricated.
                fname_hour, fname_minute = _parse_start_time_from_filename(filepath)
                first_within = float(within.dropna().iloc[0]) if within.notna().any() else 0.0
                mm = int(first_within // 60)
                ss = first_within - mm * 60
                if fname_hour is not None:
                    base_dt = start_date.replace(
                        hour=fname_hour, minute=fname_minute if fname_minute is not None else mm % 60,
                        second=int(ss), microsecond=int(round((ss % 1) * 1_000_000)))
                    tod_known = True
                else:
                    base_dt = start_date.replace(
                        hour=0, minute=mm % 60, second=int(ss),
                        microsecond=int(round((ss % 1) * 1_000_000)))
                    tod_known = False

        if base_dt is None:
            return df

        # Build a monotonic timeline by counting hour wraps in the within-hour clock.
        base_within = (base_dt.minute * 60) + base_dt.second + (base_dt.microsecond / 1_000_000)
        # Forward-fill occasional parse failures to keep wrap detection stable.
        within = within.ffill()
        wraps = (within.diff() < 0).fillna(False).cumsum()
        elapsed = wraps * 3600 + (within - base_within)
        # If the first row doesn't align with base_dt (common when base_dt was guessed), shift to start at 0.
        if elapsed.notna().any():
            elapsed = elapsed - float(elapsed.iloc[0])

        df = df.copy()
        df['Timestamp'] = base_dt + pd.to_timedelta(elapsed, unit='s')
        if not tod_known:
            df.attrs['time_of_day_reliable'] = False
            df.attrs['ingest_warnings'] = list(df.attrs.get('ingest_warnings', [])) + [
                "This file carried no clock time — only minutes and seconds. The calendar date "
                "was recovered, but the time of day is UNKNOWN and has been anchored at 00:00. "
                "Hour-dependent results (Lnight, Ldn, Lden, day/night split, diurnal profile, "
                "heatmap) are therefore not reliable for this file. Re-export it with the full "
                "'YYYY-MM-DD HH:MM:SS' timestamp column."
            ]
            logger.warning("[INGEST] Time of day unknown for %s — anchored at 00:00.",
                           os.path.basename(filepath or ''))
        return df

    # Try HH:MM:SS(.sss)
    hms = s.str.extract(r'^(?P<h>\d+):(?P<m>\d{1,2}):(?P<s>\d+(?:\.\d+)?)$')
    hms_match = float(hms.notna().all(axis=1).mean())
    if hms_match >= 0.8:
        if start_dt is None:
            return df
        hours = pd.to_numeric(hms['h'], errors='coerce')
        minutes = pd.to_numeric(hms['m'], errors='coerce')
        seconds = pd.to_numeric(hms['s'], errors='coerce')
        offset_seconds = hours * 3600 + minutes * 60 + seconds
        df = df.copy()
        df['Timestamp'] = start_dt + pd.to_timedelta(offset_seconds, unit='s')
        return df

    return df


def read_csv_file(filepath: str) -> pd.DataFrame:
    """Read CSV robustly.

    Handles:
    - UTF-8 BOM
    - Metadata rows before the real header
    - Trial start datetime + elapsed time column -> synthesizes an absolute Timestamp column
    """
    lines = _sniff_text_lines(filepath)
    skiprows = _detect_csv_header_row(lines)
    start_dt = _parse_trial_start_datetime(lines)
    header_line = lines[skiprows] if skiprows < len(lines) else (lines[0] if lines else '')
    sep = _detect_delimiter(header_line)

    # pandas 2+ supports encoding_errors
    df = pd.read_csv(
        filepath,
        skiprows=skiprows,
        sep=sep,
        encoding='utf-8-sig',
        encoding_errors='replace',
        low_memory=False,
    )

    # Lines that end with the delimiter (common in logger TSV exports) create a
    # trailing all-empty column — drop it so it isn't mistaken for a data field.
    empty_unnamed = [c for c in df.columns
                     if str(c).startswith('Unnamed') and df[c].isna().all()]
    if empty_unnamed:
        df = df.drop(columns=empty_unnamed)

    df = _maybe_add_absolute_timestamp(df, start_dt, filepath=filepath)
    return df

@app.route('/')
def index():
    return render_template('index.html')


# ── Fast-upload helpers ───────────────────────────────────────────────────────

def _quick_preview_read(filepath, nrows=10):
    """Read only the first *nrows* rows — never loads the full file into memory."""
    head = _read_file_head(filepath)
    if head.startswith(OLE_XLS_SIGNATURE) or head.startswith(ZIP_SIGNATURE):
        return pd.read_excel(filepath, nrows=nrows, engine='openpyxl')
    if _looks_like_text(head):
        lines = _sniff_text_lines(filepath)
        skiprows = _detect_csv_header_row(lines) or 0
        header_line = lines[skiprows] if skiprows < len(lines) else (lines[0] if lines else '')
        return pd.read_csv(
            filepath, nrows=nrows, sep=_detect_delimiter(header_line),
            skiprows=range(1, skiprows + 1) if skiprows else None,
            on_bad_lines='skip',
        )
    if WLGParser.is_wlg_file(filepath):
        return parse_wlg_file(filepath).head(nrows)
    raise ValueError("Unsupported file format")


def _fast_row_count(filepath):
    """Return row count without loading all data. Uses openpyxl metadata for
    XLSX and newline counting for CSV. Returns None on failure."""
    head = _read_file_head(filepath)
    try:
        if head.startswith(OLE_XLS_SIGNATURE) or head.startswith(ZIP_SIGNATURE):
            from openpyxl import load_workbook
            wb = load_workbook(filepath, read_only=True)
            ws = wb.active
            max_row = ws.max_row
            wb.close()
            if max_row and max_row > 1:
                return max_row - 1  # subtract header row
        elif _looks_like_text(head):
            count = 0
            with open(filepath, 'rb') as f:
                for chunk in iter(lambda: f.read(65536), b''):
                    count += chunk.count(b'\n')
            return max(0, count - 1)
    except Exception as e:
        logger.warning(f"[FAST-COUNT] {e}")
    return None


def _sanitize_for_json(records):
    """Replace NaN / Infinity / numpy scalars so jsonify never chokes."""
    cleaned = []
    for row in records:
        clean_row = {}
        for k, v in row.items():
            if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
                clean_row[k] = None
            elif hasattr(v, 'item'):  # numpy scalar → Python native
                try:
                    native = v.item()
                    clean_row[k] = None if (isinstance(native, float) and (math.isnan(native) or math.isinf(native))) else native
                except Exception:
                    clean_row[k] = str(v)
            else:
                clean_row[k] = v
        cleaned.append(clean_row)
    return cleaned


@app.route('/api/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({'error': 'No file provided'}), 400
        
        file = request.files['file']
        
        if file.filename == '':
            return jsonify({'error': 'No file selected'}), 400
        
        if not allowed_file(file.filename):
            return jsonify({'error': 'Only CSV, Excel, and WLG (Larson Davis) files are allowed'}), 400
        
        # Save file
        filename = secure_filename(datetime.now().strftime("%Y%m%d_%H%M%S_") + file.filename)
        filepath = os.path.abspath(os.path.join(app.config['RAW_UPLOAD_FOLDER'], filename))
        file.save(filepath)
        
        # Read and validate data
        try:
            df = read_input_file(filepath)
        except Exception as e:
            os.remove(filepath)
            return jsonify({'error': f'Error reading file: {str(e)}'}), 400
        
        if df.empty:
            os.remove(filepath)
            return jsonify({'error': 'File is empty'}), 400

        _store_cache_entry(filepath, df=df)

        # Apply retention after a successful upload to keep disk usage bounded.
        _run_retention_cleanup(keep_paths=(filepath,))

        # Compute date range for the uploaded file
        start_date = end_date = None
        try:
            time_cols = [c for c in [resolve_time_column(df)] if c]
            if time_cols:
                ts, _ = parse_timestamps_robust(df[time_cols[0]]); ts = ts.dropna()
                if not ts.empty:
                    start_date = ts.min().isoformat()
                    end_date = ts.max().isoformat()
        except Exception:
            pass

        return jsonify({
            'success': True,
            'filename': filename,
            'filepath': filepath,
            'rows': len(df),
            'columns': df.columns.tolist(),
            'preview': df.head(10).to_dict(orient='records'),
            'start_date': start_date,
            'end_date': end_date,
        })
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/analyze', methods=['POST'])
def analyze_data():
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        job_id   = str((data or {}).get('job_id', '') or '')
        environment = str((data or {}).get('environment', 'outdoor') or 'outdoor').strip().lower()

        _set_progress(job_id, 5, 'Resolving file…')

        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)

        if not os.path.exists(filepath):
            return jsonify({
                'error': 'File not found',
                'filepath': filepath,
                'hint': 'Re-upload the file and try again. If the server was restarted, the previous filepath may no longer exist.'
            }), 404

        # Read data (cached)
        _set_progress(job_id, 15, 'Loading data…')
        df = _get_cached_df(filepath)

        # Apply temporal filtration if the user defined exclusions / bounds.
        # Filters bypass the analysis cache so the results match the clean_df exactly.
        filters = (data or {}).get('filters')
        if filters:
            df = _apply_temporal_filters(df, filters)
            logger.info(f"[ANALYZE] Applied temporal filters: {len(df)} rows retained")

        # Check if analysis is already cached (only when no filters applied)
        cache_entry = _get_cache_entry(filepath)
        if (not filters) and cache_entry and cache_entry.get('analysis') and cache_entry.get('standards'):
            logger.info(f"[ANALYZE] Using CACHED analysis for {filepath}")
            _set_progress(job_id, 50, 'Loaded from cache…')
            analysis_results = cache_entry['analysis']
            standards_results = cache_entry['standards']
        else:
            logger.info(f"[ANALYZE] Computing FRESH analysis for {filepath}")
            _set_progress(job_id, 30, 'Computing statistics…')
            analyzer = NoiseAnalyzer(df)
            analysis_results = analyzer.comprehensive_analysis()

            _set_progress(job_id, 55, 'Checking standards…')
            standards_analyzer = StandardsAnalyzer(df)
            standards_results = standards_analyzer.analyze()

            if not filters:
                _store_cache_entry(filepath, df=df, analysis=analysis_results, standards=standards_results)

        # ── Timestamp integrity guard ──────────────────────────────────────────
        # Detect corrupted / dateless timestamps so the client can warn the user
        # and suppress time-dependent metrics instead of showing fabricated values.
        timestamp_integrity: dict = {"status": "ok", "time_metrics_valid": True}
        try:
            _tcol = resolve_time_column(df)
            if _tcol is not None:
                timestamp_integrity = assess_timestamp_integrity(df[_tcol]).to_dict()
        except Exception as ts_err:
            logger.warning(f"[ANALYZE] Timestamp integrity check skipped: {ts_err}")

        # Ingestion-level data-integrity warnings (e.g. spreadsheet row-limit
        # truncation). These describe what is MISSING from the file, which the
        # timestamp check cannot see — the surviving rows parse perfectly.
        ingest_warnings: list[str] = list(df.attrs.get('ingest_warnings', []) or [])

        # ── Suppress time-dependent metrics when the timeline is not real ──────
        # When timestamps are unusable, pandas still yields *a* timeline (it
        # parses a bare '24:44.0' as a time on the processing date), so every
        # hour-dependent metric computes successfully and looks authoritative
        # while describing a day that never happened.
        #
        # These values must be removed on the SERVER. Relying on the client to
        # hide them leaves the fabricated numbers in the API response, in any
        # exported payload, and in the narrative text itself.
        time_metrics_suppressed = not bool(timestamp_integrity.get('time_metrics_valid', True))
        if time_metrics_suppressed:
            _TIME_DEPENDENT = (
                'Lden', 'Ldn', 'Lnight', 'LAeq_day', 'LAeq_night', 'LAeq_evening',
                'LAeq_day_ldn', 'LAeq_night_ldn', 'LAeq_day_lden',
                'LAeq_night_lden', 'LAeq_evening_lden',
            )
            for _col, _vals in (analysis_results.get('environmental_metrics') or {}).items():
                for _k in _TIME_DEPENDENT:
                    _vals.pop(_k, None)
            # Strip the same values from the per-column compliance block.
            for _col, _vals in (analysis_results.get('compliance') or {}).items():
                if isinstance(_vals, dict):
                    for _k in list(_vals):
                        if any(t in str(_k) for t in ('Lden', 'Lnight', 'LAmax night')):
                            _vals.pop(_k, None)
                    _vals['current_Lden'] = 'N/A'
                    _vals['current_Lnight'] = 'N/A'
            analysis_results['data_summary']['measurement_period'] = {
                'start': 'Unknown (timestamps unreadable)',
                'end': 'Unknown (timestamps unreadable)',
            }
            ingest_warnings.append(
                timestamp_integrity.get('message')
                or 'Timestamps are unreliable; time-dependent metrics have been suppressed.'
            )
            logger.warning('[ANALYZE] Timestamps unusable (%s) — time-dependent metrics suppressed.',
                           timestamp_integrity.get('method'))

        # ── Forensic gap analysis ──────────────────────────────────────────────
        _set_progress(job_id, 65, 'Detecting data gaps…')
        gap_data: dict = {}
        try:
            time_candidates = [c for c in [resolve_time_column(df)] if c]
            if time_candidates:
                gap_rpt = detect_gaps(df, time_candidates[0])
                gap_data = gap_report_to_dict(gap_rpt)
        except Exception as gap_err:
            logger.warning(f"[ANALYZE] Gap detection skipped: {gap_err}")

        # ── Compliance matrix check ────────────────────────────────────────────
        _set_progress(job_id, 75, 'Evaluating compliance…')
        compliance_matrix: list[dict] = []
        try:
            env = analysis_results.get('environmental_metrics', {})
            first_col = next(iter(env), None) if env else None
            env_first = env.get(first_col, {}) if first_col else {}
            stats     = analysis_results.get('statistics', {})
            stat_first = stats.get(first_col or next(iter(stats), ''), {}) or {}

            # COMAR rows are defined on 07:00-22:00 and 22:00-07:00. Use the Ldn
            # windows explicitly rather than the generic aliases, so the metric
            # always matches the averaging period the legal limit specifies.
            laeq_day   = env_first.get('LAeq_day_ldn', env_first.get('LAeq_day'))
            laeq_night = env_first.get('LAeq_night_ldn', env_first.get('LAeq_night'))

            # LAmax must come from the L-Max stream. Taking max() of the LEQ
            # column understates the true peak, since LEQ is already averaged
            # over each logging interval.
            lamax_val = None
            _lmax_col = next(
                (c for c in stats
                 if 'lmax' in ''.join(ch for ch in str(c).lower() if ch.isalnum())),
                None
            )
            if _lmax_col:
                lamax_val = float(stats.get(_lmax_col, {}).get('max') or 0) or None

            compliance_matrix = evaluate_compliance(
                lden        = env_first.get('Lden'),
                lnight      = env_first.get('Lnight'),
                laeq        = float(stat_first.get('laeq_db') or stat_first.get('mean') or 0) or None,
                laeq_day    = laeq_day,
                laeq_night  = laeq_night,
                lamax       = lamax_val,
                environment = environment,
            )
        except Exception as cm_err:
            logger.warning(f"[ANALYZE] Compliance matrix skipped: {cm_err}")

        # ── At-a-glance key findings (participant-friendly headline numbers) ────
        key_findings: dict = {}
        try:
            stats_all = analysis_results.get('statistics', {})
            cols = list(stats_all.keys())
            prim = next((c for c in cols if 'leq' in ''.join(ch for ch in c.lower() if ch.isalnum())),
                        cols[0] if cols else None)
            if prim is not None:
                leq_series = pd.to_numeric(df[prim], errors='coerce').dropna()
                if not leq_series.empty:
                    key_findings['avg_laeq'] = energetic_mean_db(leq_series)
                    key_findings['peak'] = float(leq_series.max())
                    # Share of individual logged samples at or below 53 dB(A).
                    # This is NOT a WHO compliance figure: 53 dB(A) is an Lden
                    # limit — a duration-weighted, penalty-adjusted long-term
                    # average — and cannot be evaluated against instantaneous
                    # samples. Named and labelled as a plain distribution
                    # statistic so it cannot be read as a compliance rate.
                    key_findings['pct_samples_at_or_below_53db'] = round(
                        100.0 * float((leq_series <= 53.0).mean()), 1)
                    key_findings['pct_samples_note'] = (
                        'Share of individual logged samples at or below 53 dB(A). '
                        'Not a WHO compliance rate — WHO limits apply to Lden/Lnight, '
                        'not to individual samples.'
                    )
                    # Lden/Lnight are the metrics the WHO guidelines are defined
                    # on, so the guideline headline card must read from these.
                    # Suppressed automatically when the timeline is not real,
                    # because the block above strips them from environmental_metrics.
                    _env_kf = (analysis_results.get('environmental_metrics') or {})
                    _env_first_kf = _env_kf.get(next(iter(_env_kf), ''), {}) if _env_kf else {}
                    if _env_first_kf.get('Lden') is not None:
                        key_findings['lden'] = _env_first_kf['Lden']
                    if _env_first_kf.get('Lnight') is not None:
                        key_findings['lnight'] = _env_first_kf['Lnight']
                    _tcol_kf = resolve_time_column(df)
                    # Calendar dates touched by the record. A recording that runs
                    # 20:00-08:00 touches 2 dates but covers 0.5 days, so this is
                    # explicitly a date count, not a duration.
                    key_findings['n_calendar_dates'] = int(len(pd.to_datetime(
                        df[_tcol_kf or prim],
                        errors='coerce').dropna().dt.normalize().unique())) if cols else 0
                    # Loudest / quietest hour by energy average
                    tcands = [c for c in [_tcol_kf] if c]
                    if tcands:
                        tser, _ = parse_timestamps_robust(df[tcands[0]])
                        tmp = pd.DataFrame({'h': tser.dt.hour, 'v': pd.to_numeric(df[prim], errors='coerce')}).dropna()
                        if not tmp.empty:
                            hourly = tmp.groupby('h')['v'].apply(lambda s: energetic_mean_db(s)).dropna()
                            if not hourly.empty:
                                key_findings['loudest_hour'] = int(hourly.idxmax())
                                key_findings['loudest_hour_db'] = round(float(hourly.max()), 1)
                                key_findings['quietest_hour'] = int(hourly.idxmin())
                                key_findings['quietest_hour_db'] = round(float(hourly.min()), 1)
        except Exception as kf_err:
            logger.warning(f"[ANALYZE] Key findings skipped: {kf_err}")

        # Plain-English summary — computed from stats, no AI
        _set_progress(job_id, 88, 'Building health summary…')
        plain_english_summary = ''
        try:
            env_pe = analysis_results.get('environmental_metrics', {})
            first_col_pe = next(iter(env_pe), None) if env_pe else None
            env_first_pe = env_pe.get(first_col_pe, {}) if first_col_pe else {}
            stats_pe = analysis_results.get('statistics', {})
            stat_first_pe = stats_pe.get(first_col_pe or next(iter(stats_pe), ''), {}) or {}
            pct_pe = analysis_results.get('percentiles', {})
            pct_first_pe = pct_pe.get(first_col_pe or next(iter(pct_pe), ''), {}) if pct_pe else {}

            ts_candidates_pe = [c for c in [resolve_time_column(df)] if c]
            ts_pe = parse_timestamps_robust(df[ts_candidates_pe[0]])[0] if ts_candidates_pe else pd.Series(dtype='datetime64[ns]')
            # A fabricated timeline must not produce dates, day counts, or
            # completeness figures in the prose.
            ts_valid_pe = ts_pe.dropna() if not time_metrics_suppressed else pd.Series(dtype='datetime64[ns]')
            # Elapsed monitoring duration in whole days. Reported alongside
            # key_findings['n_calendar_dates'], which counts calendar dates
            # touched — the two legitimately differ and must not be conflated.
            n_days_pe = int(round((ts_valid_pe.max() - ts_valid_pe.min()).total_seconds() / 86400)) if not ts_valid_pe.empty else 0
            start_pe = ts_valid_pe.min().strftime('%d %b %Y') if not ts_valid_pe.empty else ''
            end_pe   = ts_valid_pe.max().strftime('%d %b %Y') if not ts_valid_pe.empty else ''
            completeness_pe = data_completeness_pct(ts_valid_pe, actual_count=len(df))

            # True LAmax from the L-Max column, and the measured logging interval,
            # so the narrative names the stream each peak came from.
            _lmax_col_pe = next(
                (c for c in df.columns
                 if 'lmax' in ''.join(ch for ch in str(c).lower() if ch.isalnum())), None)
            _lamax_pe = None
            if _lmax_col_pe:
                _s = pd.to_numeric(df[_lmax_col_pe], errors='coerce').dropna()
                _lamax_pe = float(_s.max()) if not _s.empty else None
            _interval_pe = _modal_interval_seconds(ts_valid_pe) if not ts_valid_pe.empty else None

            # Flag when a handful of samples carry the average (see acoustics.energy_concentration).
            _dominance_pe = None
            try:
                _prim_pe = _resolve_noise_column(df)
                if _prim_pe:
                    _dominance_pe = energy_concentration(pd.to_numeric(df[_prim_pe], errors='coerce').dropna())
            except Exception:
                pass

            # Real gap inventory, so the narrative can state interruptions
            # instead of asserting an unbroken record.
            n_gaps_pe = int((gap_data or {}).get('gap_count') or 0)
            gap_hours_pe = float((gap_data or {}).get('missing_seconds') or 0.0) / 3600.0
            if time_metrics_suppressed:
                n_gaps_pe, gap_hours_pe = 0, 0.0

            plain_english_summary = ReportGeneratorV2.generate_plain_english_summary(
                laeq        = env_first_pe.get('LAeq_24h') or float(stat_first_pe.get('laeq_db') or stat_first_pe.get('mean') or 0) or None,
                lden        = env_first_pe.get('Lden'),
                lnight      = env_first_pe.get('Lnight'),
                laeq_day    = env_first_pe.get('LAeq_day'),
                laeq_night  = env_first_pe.get('LAeq_night'),
                laeq_min    = float(stat_first_pe.get('min') or 0) or None,
                laeq_max    = float(stat_first_pe.get('max') or 0) or None,
                l10         = float(pct_first_pe.get('L10') or 0) or None,
                l90         = float(pct_first_pe.get('L90') or 0) or None,
                start_date  = start_pe,
                end_date    = end_pe,
                duration_label = '',
                data_completeness_pct = completeness_pe,
                n_days      = n_days_pe,
                n_gaps      = n_gaps_pe,
                total_gap_hours = gap_hours_pe,
                environment = environment,
                truncation_warning = bool(df.attrs.get('truncated_at_row_limit')),
                timestamps_unusable = time_metrics_suppressed,
                # LAmax must come from the L-Max stream, not max(LEQ).
                lamax = _lamax_pe,
                logging_interval_s = _interval_pe,
                energy_dominance = _dominance_pe,
            )
        except Exception as _pe_err:
            logger.warning(f"[ANALYZE] Plain-English summary failed: {_pe_err}")

        _set_progress(job_id, 100, 'Complete')
        return jsonify({
            'success': True,
            'analysis': analysis_results,
            'standards': standards_results,
            'gap_analysis': gap_data,
            'compliance_matrix': compliance_matrix,
            'plain_english_summary': plain_english_summary,
            'timestamp_integrity': timestamp_integrity,
            'ingest_warnings': ingest_warnings,
            'key_findings': key_findings,
            'filepath': filepath
        })

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


# ── Multi-file upload endpoint ────────────────────────────────────────────────

def _df_date_range(df: pd.DataFrame) -> tuple[str | None, str | None]:
    """Return (start_iso, end_iso) for the primary time column of a dataframe."""
    time_cols = [c for c in [resolve_time_column(df)] if c]
    if not time_cols:
        return None, None
    try:
        ts, _ = parse_timestamps_robust(df[time_cols[0]]); ts = ts.dropna()
        if ts.empty:
            return None, None
        return ts.min().isoformat(), ts.max().isoformat()
    except Exception:
        return None, None


@app.route('/api/upload-multi', methods=['POST'])
def upload_multi():
    """
    Accept 1-N files, merge them into a single master dataframe,
    save the merged file, run gap detection, and return the result.

    Optionally accepts 'existing_filepath' as a form field — if provided,
    the existing merged CSV is prepended to the batch so users can keep adding
    files without re-uploading everything.
    """
    try:
        files = request.files.getlist('files')
        if not files and not request.form.get('existing_filepath'):
            return jsonify({'error': 'No files provided'}), 400

        dfs: list[pd.DataFrame] = []
        filenames: list[str] = []
        raw_dfs: list[pd.DataFrame] = []  # keep un-merged copies for per-file stats

        # ── Prepend existing merged file if provided ──────────────────────────
        existing_filepath_raw = request.form.get('existing_filepath', '').strip()
        if existing_filepath_raw:
            existing_fp = _resolve_uploaded_filepath(existing_filepath_raw)
            if os.path.exists(existing_fp):
                try:
                    ex_df = read_input_file(existing_fp)
                    if not ex_df.empty:
                        dfs.append(ex_df)
                        raw_dfs.append(ex_df)
                        filenames.append(os.path.basename(existing_fp))
                except Exception as ex_err:
                    logger.warning(f"[UPLOAD-MULTI] Could not re-read existing file: {ex_err}")

        for f in (files or []):
            if not f or f.filename == '':
                continue
            if not allowed_file(f.filename):
                return jsonify({'error': f'Unsupported file type: {f.filename}. Only CSV, Excel, WLG are allowed.'}), 400

            fname = secure_filename(datetime.now().strftime("%Y%m%d_%H%M%S_") + f.filename)
            fpath = os.path.abspath(os.path.join(app.config['RAW_UPLOAD_FOLDER'], fname))
            f.save(fpath)

            try:
                single_df = read_input_file(fpath)
                if not single_df.empty:
                    dfs.append(single_df)
                    raw_dfs.append(single_df)
                    filenames.append(f.filename)
            except Exception as read_err:
                return jsonify({'error': f'Error reading {f.filename}: {read_err}'}), 400

        if not dfs:
            return jsonify({'error': 'All uploaded files were empty or unreadable.'}), 400

        # Merge & sort
        merged_df, time_col = merge_dataframes(dfs)

        if merged_df.empty:
            return jsonify({'error': 'Merged dataset is empty.'}), 400

        # Save merged file
        merged_name = f"MERGED_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        merged_path = os.path.abspath(os.path.join(app.config['RAW_UPLOAD_FOLDER'], merged_name))
        merged_df.to_csv(merged_path, index=False)

        # Cache
        _store_cache_entry(merged_path, df=merged_df)
        _run_retention_cleanup(keep_paths=(merged_path,))

        # Gap analysis
        gap_data: dict = {}
        if time_col and time_col in merged_df.columns:
            gap_rpt  = detect_gaps(merged_df, time_col)
            gap_data = gap_report_to_dict(gap_rpt)

        # Per-file detail rows
        file_details = []
        for df_i, name_i in zip(raw_dfs, filenames):
            start_i, end_i = _df_date_range(df_i)
            file_details.append({
                'name': name_i,
                'rows': len(df_i),
                'start': start_i,
                'end': end_i,
            })

        return jsonify({
            'success': True,
            'filepath': merged_path,
            'merged_filename': merged_name,
            'source_files': filenames,
            'file_count': len(dfs),
            'rows': len(merged_df),
            'columns': merged_df.columns.tolist(),
            'preview': merged_df.head(10).to_dict(orient='records'),
            'gap_analysis': gap_data,
            'file_details': file_details,
        })

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


# ── Compliance matrix endpoint ────────────────────────────────────────────────

@app.route('/api/compliance-check', methods=['POST'])
def compliance_check():
    """
    Run the compliance matrix against an already-uploaded file
    and return a structured pass/fail table.
    """
    try:
        data = request.json or {}
        filepath = data.get('filepath')
        environment = str(data.get('environment', 'outdoor') or 'outdoor').strip().lower()
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404

        df = _get_cached_df(filepath)
        analyzer = NoiseAnalyzer(df)
        analysis = analyzer.comprehensive_analysis()

        env    = analysis.get('environmental_metrics', {})
        stats  = analysis.get('statistics', {})
        first  = next(iter(env), None) if env else None
        env_f  = env.get(first, {}) if first else {}
        stat_f = stats.get(first or next(iter(stats), ''), {}) or {}

        matrix = evaluate_compliance(
            lden       = env_f.get('Lden'),
            lnight     = env_f.get('Lnight'),
            laeq       = float(stat_f.get('laeq_db') or stat_f.get('mean') or 0) or None,
            laeq_day   = env_f.get('LAeq_day'),
            laeq_night = env_f.get('LAeq_night') or env_f.get('Lnight'),
            lamax      = float(stat_f.get('max') or 0) or None,
            environment = environment,
        )

        return jsonify({'success': True, 'compliance_matrix': matrix})

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/health-assessment', methods=['POST'])
def get_health_assessment():
    """Get comprehensive health-based noise impact assessment using WHO thresholds."""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')

        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found', 'filepath': filepath}), 404
        
        # Read data (cached)
        df = _get_cached_df(filepath)
        
        # Get health-based assessment
        standards_analyzer = StandardsAnalyzer(df)
        health_assessment = standards_analyzer.get_health_based_assessment()
        
        return jsonify({
            'success': True,
            'health_assessment': health_assessment,
            'filepath': filepath
        })
    
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/occupational-assessment', methods=['POST'])
def get_occupational_assessment():
    """Get occupational noise exposure assessment against NIOSH and OSHA standards."""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')

        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found', 'filepath': filepath}), 404
        
        # Read data (cached)
        df = _get_cached_df(filepath)
        
        # Get occupational assessment
        standards_analyzer = StandardsAnalyzer(df)
        occupational_assessment = standards_analyzer.get_occupational_assessment()
        
        return jsonify({
            'success': True,
            'occupational_assessment': occupational_assessment,
            'filepath': filepath
        })
    
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/standards-reference', methods=['GET'])
def get_standards_reference():
    """Get comprehensive standards reference data (WHO, NIOSH, OSHA, EU, etc.)."""
    try:
        from analysis.standards_reference import who_2018_environmental_noise_guideline_levels, occupational_noise_standards
        from analysis.noise_education import decibel_scale_reference, frequency_weighting_guide, health_effects_by_level, annoyance_by_noise_source
        
        reference = {
            'success': True,
            'who_2018': who_2018_environmental_noise_guideline_levels(),
            'occupational_standards': occupational_noise_standards(),
            'decibel_scale': decibel_scale_reference(),
            'frequency_weighting': frequency_weighting_guide(),
            'health_effects': health_effects_by_level(),
            'annoyance_data': annoyance_by_noise_source(),
        }
        
        return jsonify(reference)
    
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/generate-report', methods=['POST'])
def generate_report():
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        report_type   = (data or {}).get('report_type') or (data or {}).get('type') or 'comprehensive'
        report_format = ((data or {}).get('format') or 'pdf').lower()
        job_id        = str((data or {}).get('job_id', '') or '')

        _set_progress(job_id, 5, 'Resolving file…')

        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)

        if not os.path.exists(filepath):
            return jsonify({
                'error': 'File not found',
                'filepath': filepath,
                'hint': 'Re-upload the file and try again. If the server was restarted, the previous filepath may no longer exist.'
            }), 404

        # Read data (cached) then apply any user-defined temporal filters
        _set_progress(job_id, 15, 'Loading data…')
        df = _get_cached_df(filepath)
        filters = (data or {}).get('filters')
        if filters:
            df = _apply_temporal_filters(df, filters)
            logger.info(f"[REPORT] Applied temporal filters: {len(df)} rows retained")

        entry = _get_cache_entry(filepath) or {}
        analysis_cached = entry.get('analysis')
        standards_cached = entry.get('standards')
        daily_cached = entry.get('daily_summary')
        hourly_cached = entry.get('hourly_summary')

        if filters or analysis_cached is None or standards_cached is None:
            _set_progress(job_id, 30, 'Computing statistics…')
            analyzer = NoiseAnalyzer(df)
            analysis_cached = analyzer.comprehensive_analysis()
            _set_progress(job_id, 45, 'Checking standards…')
            standards_analyzer = StandardsAnalyzer(df)
            standards_cached = standards_analyzer.analyze()
            if not filters:
                _store_cache_entry(filepath, df=df, analysis=analysis_cached, standards=standards_cached)
        else:
            _set_progress(job_id, 45, 'Using cached analysis…')

        report_generator = None
        if daily_cached is None or hourly_cached is None:
            report_generator = ReportGeneratorV2(
                df,
                filepath,
                analysis=analysis_cached,
                standards=standards_cached,
            )
            daily_cached = report_generator.daily_summary
            hourly_cached = report_generator.hourly_summary
            if not filters and (daily_cached is not None or hourly_cached is not None):
                _store_cache_entry(
                    filepath,
                    df=df,
                    analysis=analysis_cached,
                    standards=standards_cached,
                    daily_summary=daily_cached,
                    hourly_summary=hourly_cached,
                )

        # Device ID and merge provenance from the client
        device_id              = str((data or {}).get('device_id', '') or '').strip()
        source_files           = list((data or {}).get('source_files', []) or [])
        merge_gap_report       = (data or {}).get('merge_gap_report') or None
        custom_section_heading = str((data or {}).get('custom_section_heading', '') or '').strip()
        custom_section_body    = str((data or {}).get('custom_section_body', '') or '').strip()
        environment            = str((data or {}).get('environment', 'outdoor') or 'outdoor').strip().lower()

        def _make_generator():
            return ReportGeneratorV2(
                df, filepath,
                analysis=analysis_cached, standards=standards_cached,
                daily_summary=daily_cached, hourly_summary=hourly_cached,
                device_id=device_id, source_files=source_files,
                merge_gap_report=merge_gap_report,
                custom_section_heading=custom_section_heading,
                custom_section_body=custom_section_body,
                environment=environment,
            )

        _set_progress(job_id, 60, 'Generating document…')
        if report_format == 'docx':
            # Create Word report
            word_gen = WordReportGenerator(
                analysis_cached, standards_cached, daily_cached, hourly_cached,
                device_id=device_id, source_files=source_files,
                merge_gap_report=merge_gap_report,
                filepath=filepath,
            )
            doc = word_gen.generate()

            timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
            filename = f'noise_analysis_comprehensive_{timestamp}.docx'
            report_path = os.path.join(app.config['ARTIFACTS_REPORTS_DIR'], filename)
            doc.save(report_path)

            mimetype = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document'
        elif report_format in {'html', 'htm'}:
            generator = _make_generator()
            report_path = generator.generate_html_report(report_type, output_dir=app.config['ARTIFACTS_REPORTS_DIR']) if hasattr(generator, 'generate_html_report') else None
            if report_path is None:
                generator_old = ReportGenerator(df, filepath, analysis=analysis_cached, standards=standards_cached)
                report_path = generator_old.generate_html_report(report_type, output_dir=app.config['ARTIFACTS_REPORTS_DIR'])
            mimetype = 'text/html'
        else:  # Default to PDF
            generator = _make_generator()
            report_path = generator.generate_pdf_report(report_type, output_dir=app.config['ARTIFACTS_REPORTS_DIR'])
            mimetype = 'application/pdf'

        # Apply retention after generating a report to keep only the newest artifacts.
        _set_progress(job_id, 95, 'Saving report…')
        _run_retention_cleanup(keep_paths=(filepath, report_path))

        _set_progress(job_id, 100, 'Complete')
        return send_file(report_path, as_attachment=True, download_name=os.path.basename(report_path), mimetype=mimetype)
    
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/get-computed-summaries', methods=['POST'])
def get_computed_summaries():
    """
    Get daily and hourly summaries COMPUTED from uploaded dataset.
    Returns JSON with 'daily_summary' and 'hourly_summary' arrays.
    This ensures data accuracy by computing from the actual dataset, NOT external CSVs.
    """
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)

        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found', 'filepath': filepath}), 404
        
        filters = (data or {}).get('filters')
        cache_entry = _get_cache_entry(filepath) or {}
        if not filters and cache_entry.get('daily_summary') is not None and cache_entry.get('hourly_summary') is not None:
            daily_data = cache_entry['daily_summary']
            hourly_data = cache_entry['hourly_summary']

            daily_records = daily_data.to_dict('records') if hasattr(daily_data, 'to_dict') else daily_data or []
            hourly_records = hourly_data.to_dict('records') if hasattr(hourly_data, 'to_dict') else hourly_data or []

            for row in daily_records:
                if 'Date' in row and hasattr(row['Date'], 'isoformat'):
                    row['Date'] = row['Date'].isoformat()

            return jsonify({
                'status': 'success',
                'success': True,
                'daily_summary': daily_records,
                'hourly_summary': hourly_records,
                'daily_count': len(daily_records),
                'hourly_count': len(hourly_records),
            })

        # Read data and apply any temporal filters before computing summaries
        df = read_input_file(filepath)
        if filters:
            df = _apply_temporal_filters(df, filters)

        from analysis.report_generator_v2 import ReportGeneratorV2
        rg = ReportGeneratorV2(df, filepath)
        
        # Get computed summaries
        daily_data = []
        if rg.daily_summary is not None:
            daily_data = rg.daily_summary.to_dict('records')
            # Convert dates to strings for JSON serialization
            for row in daily_data:
                if 'Date' in row and hasattr(row['Date'], 'isoformat'):
                    row['Date'] = row['Date'].isoformat()
        
        hourly_data = []
        if rg.hourly_summary is not None:
            hourly_data = rg.hourly_summary.to_dict('records')
        
        return jsonify({
            'status': 'success',
            'success': True,
            'daily_summary': daily_data,
            'hourly_summary': hourly_data,
            'daily_count': len(daily_data),
            'hourly_count': len(hourly_data),
        })
    
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/export-data', methods=['POST'])
def export_data():
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)

        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found', 'filepath': filepath}), 404
        
        # Read data
        df = read_input_file(filepath)
        
        # Run analysis
        analyzer = NoiseAnalyzer(df)
        analysis_results = analyzer.comprehensive_analysis()
        
        # Create Excel with multiple sheets
        output = io.BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, sheet_name='Raw Data', index=False)
            
            # Statistics sheet
            stats_df = pd.DataFrame(analysis_results['statistics'])
            stats_df.to_excel(writer, sheet_name='Statistics', index=True)
            
            # Compliance sheet
            compliance_df = pd.DataFrame(analysis_results.get('compliance', []))
            if not compliance_df.empty:
                compliance_df.to_excel(writer, sheet_name='Compliance', index=False)
        
        output.seek(0)
        return send_file(output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        as_attachment=True, download_name='noise_analysis_export.xlsx')
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/export-daily-summary', methods=['POST'])
def export_daily_summary():
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)

        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found', 'filepath': filepath}), 404
        
        # Read data
        df = read_input_file(filepath)
        
        # Generate daily summary (Excel)
        summarizer = DataSummarizer(df)
        excel_output = summarizer.generate_daily_excel()

        if excel_output is None:
            return jsonify({'error': 'Daily summary requires time-series data'}), 400

        return send_file(
            excel_output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='daily_summary.xlsx',
        )
    
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/export-hourly-summary', methods=['POST'])
def export_hourly_summary():
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)

        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found', 'filepath': filepath}), 404
        
        # Read data
        df = read_input_file(filepath)
        
        # Generate hourly summary (Excel)
        summarizer = DataSummarizer(df)
        excel_output = summarizer.generate_hourly_excel()

        if excel_output is None:
            return jsonify({'error': 'Hourly summary requires time-series data'}), 400

        return send_file(
            excel_output,
            mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
            as_attachment=True,
            download_name='hourly_summary.xlsx',
        )

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


def _pick_primary_leq_column(df: pd.DataFrame) -> str | None:
    candidates = []
    for c in df.columns:
        cl = (c or '').lower()
        if 'leq' in cl or 'l_eq' in cl or 'l-eq' in cl:
            candidates.append(c)
    if candidates:
        return candidates[0]
    # Fallback: choose the first numeric column that doesn't look like time.
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    for c in numeric_cols:
        cl = (c or '').lower()
        if any(k in cl for k in ['time', 'date', 'timestamp', 'datetime']):
            continue
        return c
    return None


def _safe_compare_label(filename: str, idx: int) -> str:
    """Positional label for a comparison dataset when the user supplies none.

    Comparison results and the downloadable comparison PDF are shared onward, so
    the filename must not become the location name. A file called
    "PARTICIPANT home.xlsx" previously became the label and appeared ten times
    in the generated report, including in the headline verdict.

    A filename that is already a study code (CONV001, Home A, SITE-12) is kept,
    since that is exactly what a user would want shown.
    """
    from analysis.report_generator_v2 import is_safe_label
    stem = os.path.splitext(os.path.basename(filename or ''))[0]
    stem = re.sub(r'^\d{8}_\d{6}_', '', stem).strip()
    return stem if is_safe_label(stem) else f"Location {idx + 1}"


def _resolve_noise_column(frame: pd.DataFrame, requested: str | None = None) -> str | None:
    """Resolve the noise measurement column to analyse.

    Endpoints previously defaulted to a hardcoded ``'Noise_Level_dB'`` that does
    not exist in any real logger export, and several duplicated this logic
    inline. Preference order: an exact match on the requested name, then an
    LEQ-like column (the correct default for environmental metrics), then any
    other level column, then the first numeric column.

    Parameters
    ----------
    frame : pd.DataFrame
        Data to inspect.
    requested : str, optional
        Column name asked for by the client.

    Returns
    -------
    str | None
        Column name, or None when the frame has no usable numeric column.
    """
    if requested:
        requested_lower = str(requested).lower()
        for column in frame.columns:
            if str(column).lower() == requested_lower:
                return column

    def _norm(c) -> str:
        return ''.join(ch for ch in str(c).lower() if ch.isalnum())

    # Prefer LEQ over L-Max/L-Min: environmental metrics are defined on LEQ.
    for column in frame.columns:
        n = _norm(column)
        if ('leq' in n or 'laeq' in n) and 'max' not in n and 'min' not in n:
            if pd.to_numeric(frame[column], errors='coerce').notna().any():
                return column

    for token in ('lmax', 'lmin', 'noise', 'level', 'sound', 'db'):
        for column in frame.columns:
            if token in _norm(column):
                if pd.to_numeric(frame[column], errors='coerce').notna().any():
                    return column

    numeric_cols = frame.select_dtypes(include=[np.number]).columns.tolist()
    numeric_cols = [c for c in numeric_cols
                    if not any(t in _norm(c) for t in ('time', 'date', 'hour', 'index'))]
    return numeric_cols[0] if numeric_cols else None


def _get_datetime_series(df: pd.DataFrame) -> tuple[pd.DataFrame, str] | tuple[None, None]:
    """Return (prepared_df, time_col) with a reliable datetime series."""
    summarizer = DataSummarizer(df)
    time_col = summarizer.time_col
    if not time_col or time_col not in summarizer.df.columns:
        return None, None
    prepared = summarizer.df.copy()
    prepared[time_col], _ = parse_timestamps_robust(prepared[time_col])
    return prepared, time_col


@app.route('/api/export-daily-summary-csv', methods=['POST'])
def export_daily_summary_csv():
    """Export daily summary CSV matching the reference schema."""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')

        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)

        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found', 'filepath': filepath}), 404

        df = read_input_file(filepath)
        prepared, time_col = _get_datetime_series(df)
        if prepared is None:
            return jsonify({'error': 'Daily summary requires time-series data (a usable date/time column)'}), 400

        leq_col = _pick_primary_leq_column(prepared)
        if leq_col is None:
            return jsonify({'error': 'No noise measurement column found for daily summary'}), 400

        tmp = prepared[[time_col, leq_col]].dropna()
        tmp = tmp[tmp[time_col].notna()]
        if tmp.empty:
            return jsonify({'error': 'No valid time-series rows after parsing date/time'}), 400

        tmp = tmp.copy()
        tmp['Date'] = tmp[time_col].dt.strftime('%Y-%m-%d')

        from analysis.acoustics import energetic_mean_db as _emdb

        def _laeq_agg(s):
            return _emdb(pd.to_numeric(s, errors='coerce').dropna())

        out = tmp.groupby('Date')[leq_col].agg(
            Average_L_EQ_dB=_laeq_agg,
            Min_L_EQ_dB='min',
            Max_L_EQ_dB='max',
            Std_Dev='std',
        ).reset_index()

        csv_bytes = out.to_csv(index=False).encode('utf-8')
        return send_file(
            io.BytesIO(csv_bytes),
            mimetype='text/csv',
            as_attachment=True,
            download_name='DAILY_SUMMARY.csv',
        )

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/export-hourly-summary-csv', methods=['POST'])
def export_hourly_summary_csv():
    """Export hour-of-day summary CSV matching the reference schema."""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')

        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)

        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found', 'filepath': filepath}), 404

        df = read_input_file(filepath)
        prepared, time_col = _get_datetime_series(df)
        if prepared is None:
            return jsonify({'error': 'Hourly summary requires time-series data (a usable date/time column)'}), 400

        leq_col = _pick_primary_leq_column(prepared)
        if leq_col is None:
            return jsonify({'error': 'No noise measurement column found for hourly summary'}), 400

        tmp = prepared[[time_col, leq_col]].dropna()
        tmp = tmp[tmp[time_col].notna()]
        if tmp.empty:
            return jsonify({'error': 'No valid time-series rows after parsing date/time'}), 400

        tmp = tmp.copy()
        tmp['Hour'] = tmp[time_col].dt.hour

        from analysis.acoustics import energetic_mean_db as _emdb

        def _laeq_agg(s):
            return _emdb(pd.to_numeric(s, errors='coerce').dropna())

        out = tmp.groupby('Hour')[leq_col].agg(
            Average_L_EQ_dB=_laeq_agg,
            Min_L_EQ_dB='min',
            Max_L_EQ_dB='max',
            Std_Dev='std',
        ).reset_index()
        out['Hour'] = out['Hour'].astype(int)
        out = out.sort_values('Hour')

        csv_bytes = out.to_csv(index=False).encode('utf-8')
        return send_file(
            io.BytesIO(csv_bytes),
            mimetype='text/csv',
            as_attachment=True,
            download_name='HOURLY_SUMMARY.csv',
        )

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/export-weekly-summary', methods=['POST'])
def export_weekly_summary():
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)

        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found', 'filepath': filepath}), 404
        
        # Read data
        df = read_input_file(filepath)
        
        # Generate weekly summary
        summarizer = DataSummarizer(df)
        excel_output = summarizer.generate_weekly_excel()
        
        if excel_output is None:
            return jsonify({'error': 'Weekly summary requires time-series data'}), 400
        
        return send_file(excel_output, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                        as_attachment=True, download_name='weekly_summary.xlsx')
    
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/api/generate-advanced-charts', methods=['POST'])
def generate_advanced_charts():
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)

        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found', 'filepath': filepath}), 404
        
        # Read data
        df = read_input_file(filepath)
        
        # Generate advanced charts
        chart_gen = AdvancedChartGenerator(df)
        html_content = chart_gen.generate_all_charts_html()
        
        # Save to file
        charts_filepath = os.path.join(app.config['ARTIFACTS_CHARTS_DIR'],
                          f"charts_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html")
        with open(charts_filepath, 'w') as f:
            f.write(html_content)

        # Apply retention after generating charts.
        _run_retention_cleanup(keep_paths=(filepath, charts_filepath))
        
        return send_file(charts_filepath, mimetype='text/html', 
                        as_attachment=True, download_name='advanced_charts.html')
    
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


# ============================================================================
# NEW ENDPOINTS: Environmental Visualization & Enhanced Metrics
# ============================================================================

@app.route('/api/environmental-metrics', methods=['POST'])
def get_environmental_metrics():
    """Calculate comprehensive environmental analysis metrics"""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        noise_col = data.get('noise_col')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400
        
        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        
        # Read data
        df = _get_cached_df(filepath)
        
        noise_col = _resolve_noise_column(df, noise_col)
        if not noise_col:
            return jsonify({'error': 'No usable noise column found in this dataset'}), 400

        # Calculate environmental metrics
        calc = EnvironmentalMetricsCalculator(df, [noise_col])
        metrics = calc.calculate_all_metrics(noise_col)
        
        return jsonify({
            'success': True,
            'metrics': metrics
        })
    
    except Exception as e:
        logger.error(f"[METRICS] Error: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/exceedance-analysis', methods=['POST'])
def get_exceedance_analysis():
    """Generate exceedance analysis visualization"""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        noise_col = data.get('noise_col')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400
        
        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        
        df = _get_cached_df(filepath)
        
        noise_col = _resolve_noise_column(df, noise_col)
        if not noise_col:
            return jsonify({'error': 'No usable noise column found in this dataset'}), 400

        # Generate exceedance chart
        viz = EnvironmentalVisualizationEngine(df, noise_col)
        fig = viz.generate_exceedance_analysis(noise_col)
        
        # Convert to JSON for browser
        chart_json = fig.to_json()
        
        return jsonify({
            'success': True,
            'chart': json.loads(chart_json)
        })
    
    except Exception as e:
        logger.error(f"[EXCEEDANCE] Error: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/temporal-heatmap', methods=['POST'])
def get_temporal_heatmap():
    """Generate enhanced temporal heatmap visualization"""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        noise_col = data.get('noise_col')
        resolution = data.get('resolution', 'hourly')  # 'hourly' or 'daily'
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400
        
        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        
        df = _get_cached_df(filepath)
        filters = (data or {}).get('filters')
        if filters:
            df = _apply_temporal_filters(df, filters)

        noise_col = _resolve_noise_column(df, noise_col)
        if not noise_col:
            return jsonify({'error': 'No usable noise column found'}), 400

        viz = EnvironmentalVisualizationEngine(df, noise_col)
        fig = viz.generate_enhanced_temporal_heatmap(noise_col=noise_col, resolution=resolution)
        
        chart_json = fig.to_json()
        
        return jsonify({
            'success': True,
            'chart': json.loads(chart_json)
        })
    
    except Exception as e:
        logger.error(f"[TEMPORAL_HEATMAP] Error: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/diurnal-boxplot', methods=['POST'])
def get_diurnal_boxplot():
    """Generate hourly box-and-whisker visualization for LEQ volatility."""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        noise_col = (data or {}).get('noise_col', 'Noise_Level_dB')

        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)

        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404

        df = _get_cached_df(filepath)
        filters = (data or {}).get('filters')
        if filters:
            df = _apply_temporal_filters(df, filters)

        noise_col = _resolve_noise_column(df, noise_col)
        if not noise_col:
            return jsonify({'error': 'No usable noise column found'}), 400

        viz = EnvironmentalVisualizationEngine(df, noise_col)
        fig = viz.generate_diurnal_box_whisker(noise_col)

        if fig is None:
            return jsonify({'error': 'Unable to generate box-and-whisker chart'}), 400

        chart_json = fig.to_json()

        return jsonify({
            'success': True,
            'chart': json.loads(chart_json)
        })

    except Exception as e:
        logger.error(f"[DIURNAL_BOXPLOT] Error: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/distribution-analysis', methods=['POST'])
def get_distribution_analysis():
    """Generate violin plot distribution visualization"""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        noise_col = data.get('noise_col')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400
        
        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        
        df = _get_cached_df(filepath)
        
        # Generate violin plot
        noise_col = _resolve_noise_column(df, noise_col)
        if not noise_col:
            return jsonify({'error': 'No usable noise column found in this dataset'}), 400

        viz = EnvironmentalVisualizationEngine(df, noise_col)
        fig = viz.generate_violin_distribution(noise_col)
        
        chart_json = fig.to_json()
        
        return jsonify({
            'success': True,
            'chart': json.loads(chart_json)
        })
    
    except Exception as e:
        logger.error(f"[DISTRIBUTION] Error: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/compliance-dashboard', methods=['POST'])
def get_compliance_dashboard():
    """Generate compliance dashboard with gauge charts"""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        noise_col = data.get('noise_col')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400
        
        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        
        df = _get_cached_df(filepath)
        
        # Run analysis
        cache_entry = _get_cache_entry(filepath)
        if cache_entry and cache_entry.get('analysis'):
            analysis = cache_entry['analysis']
        else:
            analyzer = NoiseAnalyzer(df)
            analysis = analyzer.comprehensive_analysis()
        
        # Generate compliance dashboard
        noise_col = _resolve_noise_column(df, noise_col)
        if not noise_col:
            return jsonify({'error': 'No usable noise column found in this dataset'}), 400

        viz = EnvironmentalVisualizationEngine(df, noise_col)
        fig = viz.generate_compliance_dashboard(noise_col=noise_col, analysis=analysis)
        
        chart_json = fig.to_json()
        
        return jsonify({
            'success': True,
            'chart': json.loads(chart_json)
        })
    
    except Exception as e:
        logger.error(f"[COMPLIANCE_DASHBOARD] Error: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/anomaly-detection', methods=['POST'])
def get_anomaly_detection():
    """Generate anomaly detection visualization"""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        noise_col = data.get('noise_col')
        threshold = data.get('threshold_std', 2.5)
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400
        
        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        
        df = _get_cached_df(filepath)
        
        # Generate anomaly detection
        noise_col = _resolve_noise_column(df, noise_col)
        if not noise_col:
            return jsonify({'error': 'No usable noise column found in this dataset'}), 400

        viz = EnvironmentalVisualizationEngine(df, noise_col)
        fig = viz.generate_anomaly_detection(noise_col, threshold_std=threshold)
        
        chart_json = fig.to_json()
        
        return jsonify({
            'success': True,
            'chart': json.loads(chart_json)
        })
    
    except Exception as e:
        logger.error(f"[ANOMALY] Error: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/cumulative-distribution', methods=['POST'])
def get_cumulative_distribution():
    """Generate cumulative distribution function visualization"""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        noise_col = data.get('noise_col')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400
        
        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        
        df = _get_cached_df(filepath)
        
        # Generate CDF
        noise_col = _resolve_noise_column(df, noise_col)
        if not noise_col:
            return jsonify({'error': 'No usable noise column found in this dataset'}), 400

        viz = EnvironmentalVisualizationEngine(df, noise_col)
        fig = viz.generate_cumulative_distribution(noise_col)
        
        chart_json = fig.to_json()
        
        return jsonify({
            'success': True,
            'chart': json.loads(chart_json)
        })
    
    except Exception as e:
        logger.error(f"[CDF] Error: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/get-data-date-range', methods=['POST'])
def get_data_date_range():
    """Return the min/max timestamp of the uploaded dataset for the filtration UI."""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found', 'filepath': filepath}), 404

        df = _get_cached_df(filepath)
        _tc = resolve_time_column(df)
        if not _tc:
            return jsonify({'error': 'No timestamp column found'}), 400

        ts, _ = parse_timestamps_robust(df[_tc])
        ts = ts.dropna()
        if ts.empty:
            return jsonify({'error': 'No valid timestamps in file'}), 400

        return jsonify({
            'success': True,
            'start': ts.min().isoformat(),
            'end':   ts.max().isoformat(),
            'total_rows': len(df),
        })

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/validate-filters', methods=['POST'])
def validate_filters():
    """Return row count after applying temporal filters — used for UI preview."""
    try:
        data = request.json
        filepath = (data or {}).get('filepath')
        filters  = (data or {}).get('filters')

        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400

        filepath = _resolve_uploaded_filepath(filepath)
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found', 'filepath': filepath}), 404

        df = _get_cached_df(filepath)
        total_rows = len(df)
        clean_df = _apply_temporal_filters(df, filters)
        retained_rows = len(clean_df)
        dropped_rows  = total_rows - retained_rows

        return jsonify({
            'success': True,
            'total_rows':    total_rows,
            'retained_rows': retained_rows,
            'dropped_rows':  dropped_rows,
            'retained_pct':  round(100.0 * retained_rows / max(1, total_rows), 1),
        })

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


def _compute_comparison_metrics(df: pd.DataFrame, original_name: str) -> dict:
    """Extract key acoustic metrics from a single dataset for cross-file comparison."""
    from analysis.acoustics import energetic_mean_db, exceedance_levels_db, compute_ldn_lden
    from analysis.noise_analyzer import NoiseAnalyzer

    analyzer = NoiseAnalyzer(df)
    noise_cols = list(analyzer.noise_columns)

    def _norm(name):
        return "".join(ch for ch in (name or "").lower() if ch.isalnum())

    lmax_col = lmin_col = None
    for c in noise_cols:
        n = _norm(c)
        if lmax_col is None and ("lmax" in n or n.endswith("maxdba")):
            lmax_col = c
        if lmin_col is None and ("lmin" in n or n.endswith("mindba")):
            lmin_col = c

    leq_candidates = [
        c for c in noise_cols if c != lmax_col and c != lmin_col
        and ("laeq" in _norm(c) or "leq" in _norm(c))
    ]
    if leq_candidates:
        leq_col = leq_candidates[0]
    else:
        non_peak = [c for c in noise_cols if c != lmax_col and c != lmin_col]
        leq_col = non_peak[0] if non_peak else (noise_cols[0] if noise_cols else None)

    ts_candidates = [c for c in [resolve_time_column(df)] if c]
    ts_col = ts_candidates[0] if ts_candidates else None

    ts = parse_timestamps_robust(df[ts_col])[0] if ts_col else pd.Series(dtype='datetime64[ns]')
    leq = pd.to_numeric(df[leq_col], errors="coerce").dropna() if leq_col else pd.Series(dtype=float)
    lmax_s = pd.to_numeric(df[lmax_col], errors="coerce").dropna() if lmax_col else pd.Series(dtype=float)
    lmin_s = pd.to_numeric(df[lmin_col], errors="coerce").dropna() if lmin_col else pd.Series(dtype=float)

    def _safe(v):
        if v is None:
            return None
        try:
            vf = float(v)
            return round(vf, 2) if np.isfinite(vf) else None
        except Exception:
            return None

    laeq = energetic_mean_db(leq) if not leq.empty else None
    la_max = float(lmax_s.max()) if not lmax_s.empty else (float(leq.max()) if not leq.empty else None)
    la_min = float(lmin_s.min()) if not lmin_s.empty else (float(leq.min()) if not leq.empty else None)

    exc = exceedance_levels_db(leq.to_numpy()) if not leq.empty else {}

    env_metrics = None
    ts_valid = ts.dropna()
    if not ts_valid.empty and not leq.empty:
        aligned = pd.DataFrame({'ts': ts_valid}).join(
            pd.DataFrame({'leq': leq}), how='inner'
        ).dropna()
        if not aligned.empty:
            env_metrics = compute_ldn_lden(aligned['ts'], aligned['leq'])

    lden = env_metrics.get('Lden') if env_metrics else None
    lnight = env_metrics.get('Lnight') if env_metrics else None

    date_range = "N/A"
    n_weeks = None
    duration_label = "N/A"
    if not ts_valid.empty:
        span_secs = (ts_valid.max() - ts_valid.min()).total_seconds()
        span_days = span_secs / 86400
        n_weeks = span_days / 7
        date_range = f"{ts_valid.min().strftime('%b %d, %Y')} – {ts_valid.max().strftime('%b %d, %Y')}"
        # Format as "X days, Yh Zmin"
        total_s = int(round(span_secs))
        d = total_s // 86400
        h = (total_s % 86400) // 3600
        m = (total_s % 3600) // 60
        parts = [f"{d} day{'s' if d != 1 else ''}"] if d > 0 else []
        if h > 0:
            parts.append(f"{h} hr{'s' if h != 1 else ''}")
        if m > 0 and d < 30:
            parts.append(f"{m} min")
        duration_label = ", ".join(parts) if parts else "< 1 min"

    # Diurnal profile: energy-averaged LAeq per clock hour (0–23)
    diurnal = [None] * 24
    if not ts_valid.empty and not leq.empty:
        df_tmp = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
        df_tmp['hour'] = df_tmp['ts'].dt.hour
        for h, grp in df_tmp.groupby('hour'):
            diurnal[int(h)] = _safe(energetic_mean_db(grp['leq']))

    # Share of individual logged samples above each level.
    #
    # These are DISTRIBUTION statistics, not compliance rates, and the field names
    # say so. Previously named pct_above_day_who / pct_above_night_who, they
    # compared individual samples against the WHO Lden (53 dB) and Lnight (45 dB)
    # guideline values — which are duration-weighted, penalty-adjusted averages
    # that cannot be evaluated sample by sample. The night figure was also taken
    # over the whole record rather than over night hours, so it was wrong twice.
    # The compliance verdict comes from Lden/Lnight, computed above.
    pct_above_53 = _safe(float((leq > 53).mean() * 100)) if not leq.empty else None
    pct_night_above_45 = None
    if not ts_valid.empty and not leq.empty:
        _t = pd.DataFrame({'ts': ts, 'leq': leq}).dropna()
        if not _t.empty:
            _h = _t['ts'].dt.hour
            _night = _t.loc[(_h >= 22) | (_h < 7), 'leq']
            if not _night.empty:
                pct_night_above_45 = _safe(float((_night > 45).mean() * 100))

    # Distribution box stats (for the box-and-whisker comparison): quartiles +
    # Tukey 1.5*IQR fences, computed on the raw level readings.
    box = None
    if not leq.empty:
        arr = leq.to_numpy()
        q1, med, q3 = (float(np.percentile(arr, p)) for p in (25, 50, 75))
        iqr = q3 - q1
        within = arr[(arr >= q1 - 1.5 * iqr) & (arr <= q3 + 1.5 * iqr)]
        box = {
            'q1': round(q1, 2), 'median': round(med, 2), 'q3': round(q3, 2),
            'lo': round(float(within.min()) if within.size else float(arr.min()), 2),
            'hi': round(float(within.max()) if within.size else float(arr.max()), 2),
        }

    return {
        'name': original_name,
        'n_records': int(len(df)),
        'date_range': date_range,
        'n_weeks': _safe(n_weeks),
        'duration_label': duration_label,
        'laeq': _safe(laeq),
        'lmax': _safe(la_max),
        'lmin': _safe(la_min),
        'lden': _safe(lden),
        'lnight': _safe(lnight),
        'l10': _safe(exc.get('L10')),
        'l50': _safe(exc.get('L50')),
        'l90': _safe(exc.get('L90')),
        'pct_samples_above_53db': pct_above_53,
        'pct_night_samples_above_45db': pct_night_above_45,
        'pct_samples_note': ('Share of individual logged samples above the stated level. '
                             'Not a compliance rate: WHO guidelines apply to Lden and Lnight, '
                             'not to individual samples.'),
        'diurnal': diurnal,
        'box': box,
    }


def _comparison_summary(datasets: list[dict]) -> dict:
    """Build a plain-language ranking + verdict for a set of compared datasets.

    Ranks by Lden (the day-evening-night level the WHO 53 dB guideline applies to),
    falling back to LAeq when Lden is unavailable. Also expresses the loudest-vs-
    quietest gap as perceived loudness (2x per +10 dB) and sound energy (10x per
    +10 dB), and counts how many sites exceed the WHO guidelines.
    """
    WHO_DEN, WHO_NIGHT = 53.0, 45.0

    def _rank_value(d):
        v = d.get('lden')
        if v is None:
            v = d.get('laeq')
        return v

    ranked = [d for d in datasets if _rank_value(d) is not None]
    ranked.sort(key=_rank_value)  # quietest first

    summary = {
        'who_den_limit': WHO_DEN,
        'who_night_limit': WHO_NIGHT,
        'order': [d['name'] for d in ranked],
        'quietest': None,
        'loudest': None,
        'gap_db': None,
        'loudness_factor': None,
        'energy_factor': None,
        'n_exceed_day': sum(1 for d in datasets if (d.get('lden') is not None and d['lden'] > WHO_DEN)),
        'n_exceed_night': sum(1 for d in datasets if (d.get('lnight') is not None and d['lnight'] > WHO_NIGHT)),
        'n_total': len(datasets),
        'verdict': '',
    }

    if len(ranked) >= 2:
        q, l = ranked[0], ranked[-1]
        qv, lv = _rank_value(q), _rank_value(l)
        gap = round(lv - qv, 1)
        summary['quietest'] = {'name': q['name'], 'level': round(qv, 1)}
        summary['loudest'] = {'name': l['name'], 'level': round(lv, 1)}
        summary['gap_db'] = gap
        summary['loudness_factor'] = round(2 ** (gap / 10.0), 1)
        summary['energy_factor'] = round(10 ** (gap / 10.0), 1)

        parts = [
            f"{q['name']} is the quietest location at {round(qv,1)} dB, and "
            f"{l['name']} is the loudest at {round(lv,1)} dB."
        ]
        if gap >= 3:
            parts.append(
                f"That {gap} dB difference means {l['name']} sounds about "
                f"{summary['loudness_factor']}x as loud and carries roughly "
                f"{summary['energy_factor']}x the sound energy."
            )
        nd = summary['n_exceed_day']
        if nd == 0:
            parts.append(f"All {summary['n_total']} locations are within the WHO 2018 "
                         f"day-evening-night guideline (Lden 53 dB).")
        else:
            parts.append(f"{nd} of {summary['n_total']} locations exceed the WHO 2018 "
                         f"day-evening-night guideline (Lden 53 dB).")
        nn = summary['n_exceed_night']
        if nn > 0:
            # 45 dB Lnight is a different metric over a different window, not a
            # "stricter" version of the 53 dB Lden guideline.
            parts.append(f"{nn} exceed the separate night-time sleep guideline "
                         f"(Lnight 45 dB, 23:00-07:00).")
        summary['verdict'] = ' '.join(parts)

    return summary


@app.route('/api/compare', methods=['POST'])
def compare_files():
    """Process 2–6 noise data files and return side-by-side comparison metrics."""
    try:
        files = request.files.getlist('files[]')
        if len(files) < 2:
            return jsonify({'error': 'At least 2 files are required for comparison.'}), 400
        if len(files) > 6:
            return jsonify({'error': 'Maximum 6 files can be compared at once.'}), 400

        # Optional custom labels (one per file, same order) — used as location names.
        try:
            labels = json.loads(request.form.get('labels', '[]'))
            if not isinstance(labels, list):
                labels = []
        except Exception:
            labels = []

        datasets = []
        saved_paths = []
        for idx, file in enumerate(files):
            if not file or file.filename == '':
                continue
            if not allowed_file(file.filename):
                return jsonify({'error': f'Unsupported file type: {file.filename}. Use CSV, XLSX, or WLG.'}), 400
            filename = secure_filename(datetime.now().strftime("%Y%m%d_%H%M%S_") + file.filename)
            filepath = os.path.abspath(os.path.join(app.config['RAW_UPLOAD_FOLDER'], filename))
            file.save(filepath)
            saved_paths.append(filepath)
            try:
                df = read_input_file(filepath)
                if df.empty:
                    return jsonify({'error': f'File is empty: {file.filename}'}), 400
                # A user-supplied label is used as given. Otherwise fall back to a
                # positional label, never the filename: comparison output is shared
                # with residents and regulators, and source filenames routinely
                # carry a participant's name or address.
                _given = (str(labels[idx]).strip() if idx < len(labels) and labels[idx] else '')
                label = _given or _safe_compare_label(file.filename, idx)
                metrics = _compute_comparison_metrics(df, label)
                datasets.append(metrics)
            except Exception as e:
                return jsonify({'error': f'Could not read {file.filename}: {str(e)}'}), 400

        _run_retention_cleanup(keep_paths=tuple(saved_paths))
        return jsonify({'success': True, 'datasets': datasets,
                        'comparison_summary': _comparison_summary(datasets)})

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/compare-report', methods=['POST'])
def compare_report():
    """Generate a PDF comparison report from pre-computed metrics JSON."""
    try:
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import inch
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER
        import plotly.graph_objects as go

        data = request.json or {}
        datasets = data.get('datasets', [])
        if len(datasets) < 2:
            return jsonify({'error': 'At least 2 datasets required.'}), 400

        report_filename = f"noise_comparison_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
        report_path = os.path.join(ARTIFACTS_REPORTS_DIR, report_filename)
        os.makedirs(ARTIFACTS_REPORTS_DIR, exist_ok=True)

        doc = SimpleDocTemplate(report_path, pagesize=(11 * inch, 8.5 * inch),
                                topMargin=0.5 * inch, bottomMargin=0.6 * inch,
                                leftMargin=0.75 * inch, rightMargin=0.75 * inch)
        styles = getSampleStyleSheet()
        styles['Title'].fontSize = 22
        styles['Title'].textColor = colors.HexColor('#3D5A80')
        styles['Title'].alignment = TA_CENTER
        styles['h1'].textColor = colors.HexColor('#3D5A80')
        styles['h1'].fontSize = 15
        styles['h1'].spaceBefore = 12
        styles['h2'].textColor = colors.HexColor('#293241')
        styles['h2'].fontSize = 11
        styles['BodyText'].fontSize = 9
        styles['BodyText'].leading = 12
        styles.add(ParagraphStyle('Sub', parent=styles['Normal'], fontSize=10, alignment=TA_CENTER,
                                  textColor=colors.HexColor('#666666')))
        styles.add(ParagraphStyle('Credit', parent=styles['Normal'], fontSize=7, alignment=TA_CENTER,
                                  textColor=colors.HexColor('#888888')))

        story = []
        summary = _comparison_summary(datasets)
        n = len(datasets)

        import io as _io
        from reportlab.platypus import Image as RLImage

        def _db(v):
            return f"{v:.1f}" if v is not None else "N/A"

        def _add_fig(fig, h_in):
            if fig is None:
                story.append(Paragraph("<i>Chart unavailable — insufficient data.</i>", styles['BodyText']))
                return
            try:
                img = fig.to_image(format='png', scale=2)
                story.append(RLImage(_io.BytesIO(img), width=9.5 * inch, height=h_in * inch))
            except Exception:
                story.append(Paragraph("<i>Chart could not be rendered.</i>", styles['BodyText']))

        # ---- Title ----
        story.append(Paragraph("Noise Comparison Report", styles['Title']))
        story.append(Spacer(1, 0.06 * inch))
        story.append(Paragraph(
            f"Comparing {n} monitoring locations  |  Generated {datetime.now().strftime('%B %d, %Y')}",
            styles['Sub']))
        story.append(Spacer(1, 0.22 * inch))

        # ---- Plain-language verdict ----
        if summary.get('verdict'):
            verdict_style = ParagraphStyle(
                'Verdict', parent=styles['BodyText'], fontSize=11, leading=16,
                backColor=colors.HexColor('#EFF6FF'), borderColor=colors.HexColor('#3D5A80'),
                borderWidth=1, borderPadding=(10, 12, 10, 12))
            story.append(Paragraph("What the comparison shows", styles['h1']))
            story.append(Spacer(1, 0.06 * inch))
            story.append(Paragraph(summary['verdict'], verdict_style))
            story.append(Spacer(1, 0.2 * inch))


        # ---- Side-by-side summary table (slim, plain) ----
        story.append(Paragraph("Side-by-side summary", styles['h1']))
        story.append(Spacer(1, 0.08 * inch))

        def _verdict_cell(v, limit):
            if v is None:
                return "N/A"
            if v <= limit:
                return f"<font color='#15803d'>Within ({_db(v)})</font>"
            return f"<font color='#b91c1c'>Above +{round(v - limit, 1)} ({_db(v)})</font>"

        headers = ["Location"] + [d['name'][:20] for d in datasets]
        rows = [
            ["Monitoring period"] + [d.get('date_range', 'N/A') for d in datasets],
            ["Duration"] + [d.get('duration_label', 'N/A') for d in datasets],
            ["Average level (LAeq)"] + [f"{_db(d.get('laeq'))} dB" for d in datasets],
            ["Day-night (Lden) vs 53"] + [_verdict_cell(d.get('lden'), 53.0) for d in datasets],
            ["Night (Lnight) vs 45"] + [_verdict_cell(d.get('lnight'), 45.0) for d in datasets],
            ["Loudest moment (LAmax)"] + [f"{_db(d.get('lmax'))} dB" for d in datasets],
            ["Quiet background (L90)"] + [f"{_db(d.get('l90'))} dB" for d in datasets],
        ]
        n_cols = len(headers)
        metric_col_w = 1.9 * inch
        data_col_w = (10.5 * inch - metric_col_w) / max(1, n_cols - 1)
        col_widths = [metric_col_w] + [data_col_w] * (n_cols - 1)
        table_data = [[Paragraph(str(c), styles['BodyText']) for c in row] for row in [headers] + rows]
        t = Table(table_data, colWidths=col_widths)
        t.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1e3a5f')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('TOPPADDING', (0, 0), (-1, 0), 8),
            ('BACKGROUND', (0, 1), (0, -1), colors.HexColor('#EEF2F7')),
            ('FONTNAME', (0, 1), (0, -1), 'Helvetica-Bold'),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#D1D5DB')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#F8FAFC'), colors.white]),
            ('TOPPADDING', (0, 1), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 5),
        ]))
        story.append(t)
        story.append(Spacer(1, 0.22 * inch))

        # ---- Daily pattern (diurnal) ----
        story.append(Paragraph("Daily pattern — when is each location loudest?", styles['h1']))
        story.append(Paragraph(
            "Average noise for each hour of the day, combined across all monitored days. Use it to see "
            "when each location is quietest and loudest. Dotted lines mark the WHO day (53 dB) and "
            "night (45 dB) guidelines.", styles['BodyText']))
        story.append(Spacer(1, 0.1 * inch))
        hours = list(range(24))
        colors_list = ['#1e3a5f', '#e74c3c', '#16a34a', '#f39c12', '#9b59b6', '#0ea5e9']
        diurnal_fig = go.Figure()
        for i, ds in enumerate(datasets):
            y = [v if v is not None else None for v in ds.get('diurnal', [None] * 24)]
            diurnal_fig.add_trace(go.Scatter(
                x=hours, y=y, mode='lines+markers', name=ds['name'][:24],
                line=dict(color=colors_list[i % len(colors_list)], width=2.5), connectgaps=False))
        diurnal_fig.add_hline(y=45, line_dash='dash', line_color='#d97706')
        diurnal_fig.add_hline(y=53, line_dash='dot', line_color='#c0392b')
        diurnal_fig.update_layout(
            xaxis_title='Hour of day', yaxis_title='Average noise dB(A)',
            xaxis=dict(tickmode='array', tickvals=list(range(0, 24, 2)),
                       ticktext=[f'{h:02d}:00' for h in range(0, 24, 2)], gridcolor='rgba(0,0,0,0.06)'),
            yaxis=dict(gridcolor='rgba(0,0,0,0.06)'),
            height=400, width=950, legend=dict(orientation='h', y=-0.25),
            margin=dict(l=60, r=20, t=30, b=80), plot_bgcolor='white', paper_bgcolor='white')
        _add_fig(diurnal_fig, 4.0)
        story.append(Spacer(1, 0.2 * inch))

        # ---- Methodology & disclaimer ----
        story.append(Paragraph("How to read this report", styles['h1']))
        story.append(Paragraph(
            "Noise is measured in A-weighted decibels (dB), matched to human hearing, and averaged using "
            "energy averaging (LAeq) — the standard method. <b>Lden</b> is the day-evening-night level "
            "(evening and night count for more, reflecting greater impact); <b>Lnight</b> is the night-only "
            "level. Every +10 dB sounds about twice as loud and carries ten times the sound energy. "
            "Guideline values are from the WHO Environmental Noise Guidelines (2018).",
            styles['BodyText']))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph(
            "<b>Note on fairness of comparison:</b> locations measured over different periods or durations "
            "are not perfectly comparable — check the monitoring period row above. WHO guidelines are "
            "intended as long-term annual averages, so shorter measurements are indicative only. This "
            "report is for environmental research and community information, not medical or legal advice, "
            "and accuracy depends on proper sensor calibration.",
            styles['BodyText']))

        def _footer(canvas, doc):
            canvas.saveState()
            canvas.setFont('Helvetica', 8)
            canvas.drawCentredString(
                doc.width / 2 + doc.leftMargin, 0.30 * inch,
                f"Page {doc.page} | Noise Comparison Report | {datetime.now().strftime('%Y-%m-%d')}"
            )
            canvas.setFont('Helvetica', 7)
            canvas.setFillColorRGB(0.5, 0.5, 0.5)
            canvas.drawCentredString(
                doc.width / 2 + doc.leftMargin, 0.13 * inch,
                "Developed by Chandra Prakash Choudhary | Graduate Student, Johns Hopkins University"
            )
            canvas.setStrokeColorRGB(0.239, 0.353, 0.502)
            canvas.setLineWidth(1.5)
            canvas.line(doc.leftMargin, doc.height + doc.topMargin + 0.2 * inch,
                        doc.width + doc.leftMargin, doc.height + doc.topMargin + 0.2 * inch)
            canvas.restoreState()

        doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
        _run_retention_cleanup(keep_paths=(report_path,))
        return send_file(report_path, as_attachment=True, download_name=report_filename, mimetype='application/pdf')

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/api/progress/<job_id>', methods=['GET'])
def get_progress(job_id):
    with _progress_lock:
        p = _progress_store.get(job_id, {'pct': 0, 'msg': 'Starting…'})
    return jsonify(p)


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    # Default to IPv4 localhost for maximum compatibility.
    # Some environments/browsers try 127.0.0.1 first and won't fall back to ::1.
    host = os.environ.get('FLASK_HOST', '127.0.0.1')
    port = int(os.environ.get('FLASK_PORT', '5001'))

    # Enforce retention at startup as well (covers files generated in prior runs).
    _run_retention_cleanup()

    app.run(debug=False, host=host, port=port)
