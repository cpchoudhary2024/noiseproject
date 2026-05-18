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
from analysis.gap_detector import detect_gaps, gap_report_to_dict, merge_dataframes
from analysis.compliance_matrix import evaluate_compliance
from analysis.acoustics import energetic_mean_db, compute_ldn_lden
import io
import logging
import threading

import retention
import firebase_storage

# Setup logging for cache debugging
logging.basicConfig(level=logging.INFO, format='[%(asctime)s] %(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


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

# ── Supabase Storage ─────────────────────────────────────────────────────────
# Initialise in a background thread so slow Supabase connections never block
# server startup (which would cause gunicorn health-check failures on Render).
threading.Thread(target=firebase_storage.init, daemon=True).start()

def _fb_upload(local_path: str):
    """Mirror a local file to Supabase in a background thread (non-blocking)."""
    def _task():
        firebase_storage.upload(local_path, UPLOAD_FOLDER, ARTIFACTS_DIR)
    threading.Thread(target=_task, daemon=True).start()

def _fb_download_url(local_path: str) -> str | None:
    """Return a Firebase signed URL for direct browser download, or None."""
    return firebase_storage.get_download_url(local_path, UPLOAD_FOLDER, ARTIFACTS_DIR)

def _ensure_local_file(local_path: str) -> bool:
    """Restore a file from Firebase if it is missing on the local filesystem.

    Render's ephemeral /tmp is wiped on every restart / sleep cycle.
    This function transparently re-downloads any previously uploaded file
    so the rest of the application code never has to know about it.
    Returns True if the file is (or was made) available locally.
    """
    if os.path.exists(local_path):
        return True
    logger.info('[Restore] %s not found locally — attempting Firebase restore.', local_path)
    return firebase_storage.download(local_path, UPLOAD_FOLDER, ARTIFACTS_DIR)

# Simple in-memory cache to avoid re-reading and re-analyzing the same file.
_DATA_CACHE = {}

# Bump this when parser/analysis behavior changes in a way that should invalidate
# cached results (e.g., WLG decode/scaling fixes).
_CACHE_VERSION = "2026-04-29-merge-rename-back-v3"


def _cache_mtime(filepath: str) -> float | None:
    try:
        return os.path.getmtime(filepath)
    except OSError:
        return None


def _get_cache_entry(filepath: str):
    abs_path = os.path.abspath(filepath)
    entry = _DATA_CACHE.get(abs_path)
    if not entry:
        logger.info(f"[CACHE] MISS: {abs_path} - not in cache")
        return None

    if entry.get('cache_version') != _CACHE_VERSION:
        logger.info(f"[CACHE] MISS: {abs_path} - cache version changed")
        return None

    current_mtime = _cache_mtime(abs_path)
    stored_mtime = entry.get('mtime')
    if stored_mtime != current_mtime:
        logger.info(f"[CACHE] MISS: {abs_path} - file modified (stored: {stored_mtime}, current: {current_mtime})")
        return None
    logger.info(f"[CACHE] HIT: {abs_path} - cache valid")
    return entry


def _store_cache_entry(filepath: str, df=None, analysis=None, standards=None, daily_summary=None, hourly_summary=None):
    abs_path = os.path.abspath(filepath)
    mtime = _cache_mtime(abs_path)
    _DATA_CACHE[abs_path] = {
        'cache_version': _CACHE_VERSION,
        'mtime': mtime,
        'df': df,
        'analysis': analysis,
        'standards': standards,
        'daily_summary': daily_summary,
        'hourly_summary': hourly_summary,
    }
    logger.info(
        f"[CACHE] STORE: {abs_path} - mtime={mtime}, has_df={df is not None}, "
        f"has_analysis={analysis is not None}, has_standards={standards is not None}, "
        f"has_daily_summary={daily_summary is not None}, has_hourly_summary={hourly_summary is not None}"
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

    time_candidates = [
        c for c in df.columns
        if any(t in c.lower() for t in ['timestamp', 'datetime', 'time', 'date'])
    ]
    if not time_candidates:
        return df

    time_col = time_candidates[0]
    ts = pd.to_datetime(df[time_col], errors='coerce', dayfirst=True)
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
    """Resolve a client-provided filepath (absolute path or basename).

    Backwards compatible:
    - If basename: check uploads/raw first, then uploads root.
    - If absolute/relative path provided: normalize to absolute.

    Also transparently restores the file from Firebase Storage when the
    local ephemeral filesystem has been wiped (Render sleep/restart).
    """
    if os.path.basename(filepath) == filepath:
        raw_candidate = os.path.abspath(os.path.join(app.config['RAW_UPLOAD_FOLDER'], filepath))
        if os.path.exists(raw_candidate) or _ensure_local_file(raw_candidate):
            return raw_candidate
        root_candidate = os.path.abspath(os.path.join(app.config['UPLOAD_FOLDER'], filepath))
        _ensure_local_file(root_candidate)
        return root_candidate
    resolved = os.path.abspath(filepath)
    _ensure_local_file(resolved)
    return resolved


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

def read_excel_file(filepath):
    """Read Excel file with content-based engine detection (.xls vs .xlsx).

    Uses calamine (Rust-based) for .xlsx — same data types as openpyxl but
    4-6x faster because it does not parse styles/formatting/formulas.
    Falls back to xlrd for legacy .xls files.
    """
    head = _read_file_head(filepath, size=16)
    if head.startswith(OLE_XLS_SIGNATURE):
        return pd.read_excel(filepath, engine='xlrd')
    if head.startswith(ZIP_SIGNATURE):
        return pd.read_excel(filepath, engine='calamine')
    raise ValueError(
        "Unsupported or corrupt Excel file. The file does not look like a real .xls or .xlsx. "
        "If you renamed the file extension, please re-save it as a true .xlsx or .csv."
    )


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

    # Prefer an existing timestamp/datetime column.
    if any(any(k in c.lower() for k in ['timestamp', 'datetime']) for c in df.columns):
        return df

    time_col = next((c for c in df.columns if 'time' in c.lower()), None)
    if time_col is None:
        time_col = df.columns[0]

    s = df[time_col].astype(str).str.strip()

    # Try parsing as a full datetime first (e.g. '12-03-2026 10:33').
    parsed = pd.to_datetime(s, errors='coerce', dayfirst=True, cache=True)
    if float(parsed.notna().mean()) >= 0.9 and parsed.dt.normalize().nunique(dropna=True) >= 2:
        df = df.copy()
        df['Timestamp'] = parsed
        return df

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
        if base_dt is None and filepath:
            start_date, _end_date = _parse_date_range_from_filename(filepath)
            if start_date is not None:
                # Anchor the first sample at 00:MM:SS on the start date.
                first_within = float(within.dropna().iloc[0]) if within.notna().any() else 0.0
                mm = int(first_within // 60)
                ss = first_within - mm * 60
                base_dt = start_date.replace(hour=0, minute=mm % 60, second=int(ss), microsecond=int(round((ss % 1) * 1_000_000)))

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

    # pandas 2+ supports encoding_errors
    df = pd.read_csv(
        filepath,
        skiprows=skiprows,
        encoding='utf-8-sig',
        encoding_errors='replace',
        low_memory=False,
    )

    df = _maybe_add_absolute_timestamp(df, start_dt, filepath=filepath)
    return df

@app.route('/')
def index():
    return render_template('index.html')

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

        # Mirror to Firebase so the file survives server restarts.
        _fb_upload(filepath)

        # Apply retention after a successful upload to keep disk usage bounded.
        _run_retention_cleanup(keep_paths=(filepath,))

        # Compute date range for the uploaded file
        start_date = end_date = None
        try:
            time_cols = [c for c in df.columns if any(t in c.lower() for t in ['timestamp', 'datetime', 'time', 'date'])]
            if time_cols:
                ts = pd.to_datetime(df[time_cols[0]], errors='coerce', dayfirst=True).dropna()
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
            analysis_results = cache_entry['analysis']
            standards_results = cache_entry['standards']
        else:
            logger.info(f"[ANALYZE] Computing FRESH analysis for {filepath}")
            analyzer = NoiseAnalyzer(df)
            analysis_results = analyzer.comprehensive_analysis()

            standards_analyzer = StandardsAnalyzer(df)
            standards_results = standards_analyzer.analyze()

            if not filters:
                _store_cache_entry(filepath, df=df, analysis=analysis_results, standards=standards_results)
        
        # ── Forensic gap analysis ──────────────────────────────────────────────
        gap_data: dict = {}
        try:
            time_candidates = [c for c in df.columns if any(t in c.lower() for t in ['timestamp', 'datetime', 'time', 'date'])]
            if time_candidates:
                gap_rpt = detect_gaps(df, time_candidates[0])
                gap_data = gap_report_to_dict(gap_rpt)
        except Exception as gap_err:
            logger.warning(f"[ANALYZE] Gap detection skipped: {gap_err}")

        # ── Compliance matrix check ────────────────────────────────────────────
        compliance_matrix: list[dict] = []
        try:
            env = analysis_results.get('environmental_metrics', {})
            first_col = next(iter(env), None) if env else None
            env_first = env.get(first_col, {}) if first_col else {}
            stats     = analysis_results.get('statistics', {})
            stat_first = stats.get(first_col or next(iter(stats), ''), {}) or {}

            # Day/Night LAeq from env_metrics if available, else estimate from overall
            laeq_day   = env_first.get('LAeq_day')
            laeq_night = env_first.get('LAeq_night') or env_first.get('Lnight')

            compliance_matrix = evaluate_compliance(
                lden        = env_first.get('Lden'),
                lnight      = env_first.get('Lnight'),
                laeq        = float(stat_first.get('laeq_db') or stat_first.get('mean') or 0) or None,
                laeq_day    = laeq_day,
                laeq_night  = laeq_night,
                lamax       = float(stat_first.get('max') or 0) or None,
            )
        except Exception as cm_err:
            logger.warning(f"[ANALYZE] Compliance matrix skipped: {cm_err}")

        # Plain-English summary — computed from stats, no AI
        plain_english_summary = ''
        try:
            env_pe = analysis_results.get('environmental_metrics', {})
            first_col_pe = next(iter(env_pe), None) if env_pe else None
            env_first_pe = env_pe.get(first_col_pe, {}) if first_col_pe else {}
            stats_pe = analysis_results.get('statistics', {})
            stat_first_pe = stats_pe.get(first_col_pe or next(iter(stats_pe), ''), {}) or {}
            pct_pe = analysis_results.get('percentiles', {})
            pct_first_pe = pct_pe.get(first_col_pe or next(iter(pct_pe), ''), {}) if pct_pe else {}

            ts_candidates_pe = [c for c in df.columns if any(t in c.lower() for t in ['timestamp', 'datetime', 'time', 'date'])]
            ts_pe = pd.to_datetime(df[ts_candidates_pe[0]], errors='coerce') if ts_candidates_pe else pd.Series(dtype='datetime64[ns]')
            ts_valid_pe = ts_pe.dropna()
            n_days_pe = int(round((ts_valid_pe.max() - ts_valid_pe.min()).total_seconds() / 86400)) if not ts_valid_pe.empty else 0
            start_pe = ts_valid_pe.min().strftime('%d %b %Y') if not ts_valid_pe.empty else ''
            end_pe   = ts_valid_pe.max().strftime('%d %b %Y') if not ts_valid_pe.empty else ''
            expected_s_pe = max(0.0, (ts_valid_pe.max() - ts_valid_pe.min()).total_seconds()) if not ts_valid_pe.empty else 0
            completeness_pe = 100.0 * len(df) / max(1, expected_s_pe) if expected_s_pe > 0 else None

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
            )
        except Exception as _pe_err:
            logger.warning(f"[ANALYZE] Plain-English summary failed: {_pe_err}")

        return jsonify({
            'success': True,
            'analysis': analysis_results,
            'standards': standards_results,
            'gap_analysis': gap_data,
            'compliance_matrix': compliance_matrix,
            'plain_english_summary': plain_english_summary,
            'filepath': filepath
        })

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


# ── Multi-file upload endpoint ────────────────────────────────────────────────

def _df_date_range(df: pd.DataFrame) -> tuple[str | None, str | None]:
    """Return (start_iso, end_iso) for the primary time column of a dataframe."""
    time_cols = [c for c in df.columns if any(t in c.lower() for t in ['timestamp', 'datetime', 'time', 'date'])]
    if not time_cols:
        return None, None
    try:
        ts = pd.to_datetime(df[time_cols[0]], errors='coerce', dayfirst=True).dropna()
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

        # Mirror merged file to Firebase for persistence across restarts.
        _fb_upload(merged_path)

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
        # Read data (cached)
        data = request.json
        filepath = (data or {}).get('filepath')
        report_type = (data or {}).get('report_type') or (data or {}).get('type') or 'comprehensive'
        report_format = ((data or {}).get('format') or 'pdf').lower()
        
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
            analyzer = NoiseAnalyzer(df)
            analysis_cached = analyzer.comprehensive_analysis()
            standards_analyzer = StandardsAnalyzer(df)
            standards_cached = standards_analyzer.analyze()
            if not filters:
                _store_cache_entry(filepath, df=df, analysis=analysis_cached, standards=standards_cached)

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

        # Use ReportGeneratorV2 for publication-grade 8-section reports
        # NOTE: All summaries (daily, hourly) are COMPUTED from uploaded data, NOT loaded from external CSVs

        def _make_generator():
            return ReportGeneratorV2(
                df, filepath,
                analysis=analysis_cached, standards=standards_cached,
                daily_summary=daily_cached, hourly_summary=hourly_cached,
                device_id=device_id, source_files=source_files,
                merge_gap_report=merge_gap_report,
                custom_section_heading=custom_section_heading,
                custom_section_body=custom_section_body,
            )

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
        _run_retention_cleanup(keep_paths=(filepath, report_path))

        # Mirror report to Firebase and serve via signed URL when available.
        # Falls back to streaming from local disk if Firebase is not configured.
        _fb_upload(report_path)
        signed_url = _fb_download_url(report_path)
        if signed_url:
            from flask import redirect as flask_redirect
            return flask_redirect(signed_url)
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


def _get_datetime_series(df: pd.DataFrame) -> tuple[pd.DataFrame, str] | tuple[None, None]:
    """Return (prepared_df, time_col) with a reliable datetime series."""
    summarizer = DataSummarizer(df)
    time_col = summarizer.time_col
    if not time_col or time_col not in summarizer.df.columns:
        return None, None
    prepared = summarizer.df.copy()
    prepared[time_col] = pd.to_datetime(prepared[time_col], errors='coerce', dayfirst=True, cache=True)
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
        noise_col = data.get('noise_col', 'Noise_Level_dB')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400
        
        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        
        # Read data
        df = _get_cached_df(filepath)
        
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
        noise_col = data.get('noise_col', 'Noise_Level_dB')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400
        
        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        
        df = _get_cached_df(filepath)
        
        # Generate exceedance chart
        viz = EnvironmentalVisualizationEngine(df, noise_col)
        fig = viz.generate_exceedance_analysis()
        
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
        noise_col = data.get('noise_col', 'Noise_Level_dB')
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

        def _resolve_noise_column(frame, requested: str):
            requested_lower = str(requested).lower()
            for column in frame.columns:
                if str(column).lower() == requested_lower:
                    return column
            for token in ('leq', 'laeq', 'l-max', 'lmax', 'l-min', 'lmin', 'noise', 'level', 'sound'):
                for column in frame.columns:
                    if token in str(column).lower():
                        return column
            numeric_cols = frame.select_dtypes(include=[np.number]).columns.tolist()
            return numeric_cols[0] if numeric_cols else None

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

        def _resolve_noise_column(frame, requested: str):
            requested_lower = str(requested).lower()
            for column in frame.columns:
                if str(column).lower() == requested_lower:
                    return column
            for token in ('leq', 'laeq', 'l-max', 'lmax', 'l-min', 'lmin', 'noise', 'level', 'sound'):
                for column in frame.columns:
                    if token in str(column).lower():
                        return column
            numeric_cols = frame.select_dtypes(include=[np.number]).columns.tolist()
            return numeric_cols[0] if numeric_cols else None

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
        noise_col = data.get('noise_col', 'Noise_Level_dB')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400
        
        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        
        df = _get_cached_df(filepath)
        
        # Generate violin plot
        viz = EnvironmentalVisualizationEngine(df, noise_col)
        fig = viz.generate_violin_distribution()
        
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
        noise_col = data.get('noise_col', 'Noise_Level_dB')
        
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
        viz = EnvironmentalVisualizationEngine(df, noise_col)
        fig = viz.generate_compliance_dashboard(analysis)
        
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
        noise_col = data.get('noise_col', 'Noise_Level_dB')
        threshold = data.get('threshold_std', 2.5)
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400
        
        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        
        df = _get_cached_df(filepath)
        
        # Generate anomaly detection
        viz = EnvironmentalVisualizationEngine(df, noise_col)
        fig = viz.generate_anomaly_detection(threshold_std=threshold)
        
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
        noise_col = data.get('noise_col', 'Noise_Level_dB')
        
        if not filepath:
            return jsonify({'error': 'No filepath provided'}), 400
        
        filepath = _resolve_uploaded_filepath(filepath)
        
        if not os.path.exists(filepath):
            return jsonify({'error': 'File not found'}), 404
        
        df = _get_cached_df(filepath)
        
        # Generate CDF
        viz = EnvironmentalVisualizationEngine(df, noise_col)
        fig = viz.generate_cumulative_distribution()
        
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
        time_candidates = [
            c for c in df.columns
            if any(t in c.lower() for t in ['timestamp', 'datetime', 'time', 'date'])
        ]
        if not time_candidates:
            return jsonify({'error': 'No timestamp column found'}), 400

        ts = pd.to_datetime(df[time_candidates[0]], errors='coerce', dayfirst=True)
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

    ts_candidates = [c for c in df.columns if any(t in c.lower() for t in ["timestamp", "datetime", "date", "time"])]
    ts_col = ts_candidates[0] if ts_candidates else None

    ts = pd.to_datetime(df[ts_col], errors="coerce", dayfirst=True) if ts_col else pd.Series(dtype='datetime64[ns]')
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

    pct_above_day = _safe(float((leq > 53).mean() * 100)) if not leq.empty else None
    pct_above_night = _safe(float((leq > 45).mean() * 100)) if not leq.empty else None

    return {
        'name': os.path.basename(original_name),
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
        'pct_above_day_who': pct_above_day,
        'pct_above_night_who': pct_above_night,
        'diurnal': diurnal,
    }


@app.route('/api/compare', methods=['POST'])
def compare_files():
    """Process 2–6 noise data files and return side-by-side comparison metrics."""
    try:
        files = request.files.getlist('files[]')
        if len(files) < 2:
            return jsonify({'error': 'At least 2 files are required for comparison.'}), 400
        if len(files) > 6:
            return jsonify({'error': 'Maximum 6 files can be compared at once.'}), 400

        datasets = []
        saved_paths = []
        for file in files:
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
                metrics = _compute_comparison_metrics(df, file.filename)
                datasets.append(metrics)
            except Exception as e:
                return jsonify({'error': f'Could not read {file.filename}: {str(e)}'}), 400

        _run_retention_cleanup(keep_paths=tuple(saved_paths))
        return jsonify({'success': True, 'datasets': datasets})

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

        # ---- Title ----
        story.append(Paragraph("Multi-Dataset Environmental Noise Comparison", styles['Title']))
        story.append(Spacer(1, 0.1 * inch))
        story.append(Paragraph(
            f"Comparative Acoustic Analysis  |  Generated: {datetime.now().strftime('%B %d, %Y  %H:%M')}",
            styles['Sub']
        ))
        story.append(Spacer(1, 0.25 * inch))

        # ---- Metrics Table ----
        story.append(Paragraph("Section 1: Key Metrics Comparison", styles['h1']))
        story.append(Spacer(1, 0.1 * inch))

        def _db(v):
            return f"{v:.1f}" if v is not None else "N/A"

        def _pct(v):
            return f"{v:.1f}%" if v is not None else "N/A"

        headers = ["Metric"] + [d['name'][:22] for d in datasets]
        rows = [
            ["Date Range"] + [d.get('date_range', 'N/A') for d in datasets],
            ["Duration"] + [d.get('duration_label', 'N/A') for d in datasets],
            ["Records"] + [str(d.get('n_records', 'N/A')) for d in datasets],
            ["LAeq dB(A)"] + [_db(d.get('laeq')) for d in datasets],
            ["LAmax dB(A)"] + [_db(d.get('lmax')) for d in datasets],
            ["LAmin dB(A)"] + [_db(d.get('lmin')) for d in datasets],
            ["Lden dB(A)"] + [_db(d.get('lden')) for d in datasets],
            ["Lnight dB(A)"] + [_db(d.get('lnight')) for d in datasets],
            ["L10 dB(A)"] + [_db(d.get('l10')) for d in datasets],
            ["L50 dB(A)"] + [_db(d.get('l50')) for d in datasets],
            ["L90 dB(A)"] + [_db(d.get('l90')) for d in datasets],
            ["% time > 53 dB (WHO day)"] + [_pct(d.get('pct_above_day_who')) for d in datasets],
            ["% time > 45 dB (WHO night)"] + [_pct(d.get('pct_above_night_who')) for d in datasets],
        ]

        n_cols = len(headers)
        metric_col_w = 2.0 * inch
        data_col_w = (10.5 * inch - metric_col_w) / max(1, n_cols - 1)
        col_widths = [metric_col_w] + [data_col_w] * (n_cols - 1)

        table_data = [[Paragraph(str(c), styles['BodyText']) for c in row] for row in [headers] + rows]
        t = Table(table_data, colWidths=col_widths)

        ts_style = [
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#3D5A80')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('BACKGROUND', (0, 1), (0, -1), colors.HexColor('#EEF2F7')),
            ('FONTNAME', (0, 1), (0, -1), 'Helvetica-Bold'),
            ('BACKGROUND', (0, 1), (-1, -1), colors.HexColor('#F8FAFC')),
            ('BACKGROUND', (0, 1), (0, -1), colors.HexColor('#EEF2F7')),
            ('GRID', (0, 0), (-1, -1), 0.5, colors.HexColor('#CCCCCC')),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('ALIGN', (1, 1), (-1, -1), 'CENTER'),
            ('FONTSIZE', (0, 0), (-1, -1), 8),
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.HexColor('#F8FAFC'), colors.white]),
        ]
        t.setStyle(TableStyle(ts_style))
        story.append(t)
        story.append(Spacer(1, 0.25 * inch))

        # ---- WHO Compliance Summary ----
        story.append(Paragraph("Section 2: WHO 2018 Compliance Overview", styles['h1']))
        story.append(Spacer(1, 0.08 * inch))
        who_day_limit = 53.0
        who_night_limit = 45.0
        for ds in datasets:
            lden = ds.get('lden')
            lnight = ds.get('lnight')

            # Daytime (Lden) sentence
            if lden is not None:
                day_diff = round(lden - who_day_limit, 1)
                if day_diff > 0:
                    day_sent = (
                        f"Lden is <b>{_db(lden)} dB(A)</b> — "
                        f"<font color='#c0392b'>exceeds the WHO daytime limit of 53 dB(A) by <b>{day_diff} dB</b>,"
                        f" indicating elevated daytime acoustic exposure.</font>"
                    )
                else:
                    day_sent = (
                        f"Lden is <b>{_db(lden)} dB(A)</b> — "
                        f"<font color='#27ae60'>within the WHO daytime limit of 53 dB(A) by <b>{abs(day_diff)} dB</b>,"
                        f" compliant with daytime guidelines.</font>"
                    )
            else:
                day_sent = "Lden could not be computed (insufficient data)."

            # Nighttime (Lnight) sentence
            if lnight is not None:
                night_diff = round(lnight - who_night_limit, 1)
                if night_diff > 0:
                    night_sent = (
                        f"Lnight is <b>{_db(lnight)} dB(A)</b> — "
                        f"<font color='#c0392b'>exceeds the WHO nighttime limit of 45 dB(A) by <b>{night_diff} dB</b>,"
                        f" posing a risk of sleep disturbance and cardiovascular effects.</font>"
                    )
                else:
                    night_sent = (
                        f"Lnight is <b>{_db(lnight)} dB(A)</b> — "
                        f"<font color='#27ae60'>within the WHO nighttime limit of 45 dB(A) by <b>{abs(night_diff)} dB</b>,"
                        f" compliant with nighttime health guidelines.</font>"
                    )
            else:
                night_sent = "Lnight could not be computed (insufficient data)."

            story.append(Paragraph(f"<b>{ds['name']}</b>", styles['h2']))
            story.append(Paragraph(day_sent, styles['BodyText']))
            story.append(Paragraph(night_sent, styles['BodyText']))
            story.append(Spacer(1, 0.10 * inch))

        story.append(PageBreak())

        # ---- Diurnal Profile Chart ----
        story.append(Paragraph("Section 3: Diurnal Comparison (24-Hour Average LAeq per Dataset)", styles['h1']))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph(
            "Each line represents the energy-averaged LAeq (dB(A)) for each clock hour aggregated across all "
            "measurement days. Dashed reference lines show WHO Lnight threshold (45 dB, 23:00–07:00 band) and "
            "WHO Lden daytime threshold (53 dB). A higher diurnal peak indicates more pronounced traffic or "
            "activity-driven noise.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.1 * inch))

        hours = list(range(24))
        colors_list = ['#0066ff', '#e74c3c', '#27ae60', '#f39c12', '#9b59b6', '#1abc9c']
        diurnal_fig = go.Figure()
        for i, ds in enumerate(datasets):
            diurnal = ds.get('diurnal', [None] * 24)
            y = [v if v is not None else None for v in diurnal]
            diurnal_fig.add_trace(go.Scatter(
                x=hours, y=y, mode='lines+markers', name=ds['name'],
                line=dict(color=colors_list[i % len(colors_list)], width=2.5),
                connectgaps=False
            ))
        diurnal_fig.add_hline(y=45, line_dash='dash', line_color='#8e44ad', annotation_text='WHO Lnight 45 dB')
        diurnal_fig.add_hline(y=53, line_dash='dot', line_color='#c0392b', annotation_text='WHO Lden 53 dB')
        diurnal_fig.update_layout(
            title='Diurnal Hourly Average LAeq Comparison',
            xaxis_title='Hour of Day', yaxis_title='LAeq dB(A)',
            xaxis=dict(tickmode='array', tickvals=list(range(0, 24, 2)),
                       ticktext=[f'{h:02d}:00' for h in range(0, 24, 2)]),
            height=400, width=950,
            legend=dict(orientation='h', y=-0.25),
            margin=dict(l=60, r=20, t=50, b=80),
            plot_bgcolor='#f8fafc', paper_bgcolor='white'
        )

        try:
            img_bytes = diurnal_fig.to_image(format='png', scale=2, engine='kaleido')
            import io as _io
            from reportlab.platypus import Image as RLImage
            img_obj = RLImage(_io.BytesIO(img_bytes), width=9.5 * inch, height=4.0 * inch)
            story.append(img_obj)
        except Exception:
            story.append(Paragraph("<i>Diurnal chart could not be rendered.</i>", styles['BodyText']))

        story.append(Spacer(1, 0.25 * inch))

        # ---- LAeq / Lden / Lnight Bar Chart ----
        story.append(Paragraph("Section 4: LAeq, Lden, and Lnight Bar Comparison", styles['h1']))
        story.append(Spacer(1, 0.08 * inch))
        story.append(Paragraph(
            "Grouped bar chart comparing the three primary acoustic load metrics across all datasets. "
            "LAeq is the overall energy average; Lden adds evening (+5 dB) and night (+10 dB) penalties; "
            "Lnight reflects the unpenalised nocturnal average. WHO 2018 limits: Lden ≤ 53 dB, Lnight ≤ 45 dB.",
            styles['BodyText']
        ))
        story.append(Spacer(1, 0.1 * inch))

        names = [ds['name'] for ds in datasets]
        laeq_vals = [ds.get('laeq') for ds in datasets]
        lden_vals = [ds.get('lden') for ds in datasets]
        lnight_vals = [ds.get('lnight') for ds in datasets]

        bar_fig = go.Figure()
        bar_fig.add_trace(go.Bar(name='LAeq', x=names, y=laeq_vals, marker_color='#3498db'))
        bar_fig.add_trace(go.Bar(name='Lden', x=names, y=lden_vals, marker_color='#e74c3c'))
        bar_fig.add_trace(go.Bar(name='Lnight', x=names, y=lnight_vals, marker_color='#9b59b6'))
        bar_fig.add_hline(y=53, line_dash='dot', line_color='#c0392b', annotation_text='WHO Lden limit')
        bar_fig.add_hline(y=45, line_dash='dash', line_color='#8e44ad', annotation_text='WHO Lnight limit')
        bar_fig.update_layout(
            barmode='group', title='LAeq / Lden / Lnight Comparison',
            yaxis_title='dB(A)', height=380, width=950,
            legend=dict(orientation='h', y=-0.25),
            margin=dict(l=60, r=20, t=50, b=80),
            plot_bgcolor='#f8fafc', paper_bgcolor='white'
        )
        try:
            img_bytes = bar_fig.to_image(format='png', scale=2, engine='kaleido')
            img_obj = RLImage(_io.BytesIO(img_bytes), width=9.5 * inch, height=3.8 * inch)
            story.append(img_obj)
        except Exception:
            story.append(Paragraph("<i>Bar chart could not be rendered.</i>", styles['BodyText']))

        story.append(Spacer(1, 0.25 * inch))
        story.append(Paragraph("Section 5: Disclaimer", styles['h1']))
        story.append(Paragraph(
            "This report was generated automatically from user-uploaded acoustic monitoring data. "
            "Results are evaluated against WHO 2018 Environmental Noise Guidelines. "
            "This document is for environmental research purposes only and does not constitute "
            "medical or legal advice. Accuracy depends on proper sensor calibration.",
            styles['BodyText']
        ))

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
        _fb_upload(report_path)
        signed_url = _fb_download_url(report_path)
        if signed_url:
            from flask import redirect as flask_redirect
            return flask_redirect(signed_url)
        return send_file(report_path, as_attachment=True, download_name=report_filename, mimetype='application/pdf')

    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500


@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    # PORT is set by Render/Railway; FLASK_PORT is the local override.
    # Default host to 0.0.0.0 so cloud platforms can reach the server.
    host = os.environ.get('FLASK_HOST', '0.0.0.0')
    port = int(os.environ.get('PORT') or os.environ.get('FLASK_PORT', '5001'))

    # Enforce retention at startup as well (covers files generated in prior runs).
    _run_retention_cleanup()

    app.run(debug=False, host=host, port=port)
