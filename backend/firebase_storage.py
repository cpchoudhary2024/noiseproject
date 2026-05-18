"""
Supabase Cloud Storage integration for the Noise Analysis Platform.

Provides transparent file persistence across server restarts on Render's
ephemeral filesystem.  When SUPABASE_URL + SUPABASE_KEY are set, every
uploaded file and generated report is mirrored to Supabase Storage so it
survives Render sleep cycles and re-deploys.

If the env vars are absent the module is a no-op and the app runs in
local-only mode exactly as before.

Supabase free tier: 1 GB storage, no credit card required.
Bucket name used: noise-analysis-platform
"""

import os
import logging

logger = logging.getLogger(__name__)

_client   = None          # supabase.Client, set by init()
_bucket   = 'noise-analysis-platform'
_prefix   = 'files'       # top-level folder inside the bucket


# ─────────────────────────────────────────────────────────────────────────────
# Initialisation
# ─────────────────────────────────────────────────────────────────────────────

def init() -> bool:
    """Initialise the Supabase client from environment variables.

    Required env vars:
        SUPABASE_URL  – project URL, e.g. https://xxxx.supabase.co
        SUPABASE_KEY  – service_role secret key (not the anon key)

    Returns True on success, False if not configured or on error.
    """
    global _client

    url = os.environ.get('SUPABASE_URL', '').strip()
    key = os.environ.get('SUPABASE_KEY', '').strip()

    if not url or not key:
        logger.info('[Storage] Supabase not configured — running in local-only mode.')
        return False

    try:
        from supabase import create_client
        _client = create_client(url, key)
        # Ensure the bucket exists (creates it if it doesn't)
        _ensure_bucket()
        logger.info('[Storage] Supabase Storage ready — bucket: %s', _bucket)
        return True
    except Exception as exc:
        logger.error('[Storage] Supabase init failed: %s', exc)
        _client = None
        return False


def _ensure_bucket():
    """Create the storage bucket if it does not already exist."""
    try:
        buckets = [b.name for b in _client.storage.list_buckets()]
        if _bucket not in buckets:
            _client.storage.create_bucket(_bucket, options={'public': False})
            logger.info('[Storage] Created bucket: %s', _bucket)
    except Exception as exc:
        logger.warning('[Storage] Could not verify/create bucket: %s', exc)


def is_available() -> bool:
    return _client is not None


# ─────────────────────────────────────────────────────────────────────────────
# Path mapping
# ─────────────────────────────────────────────────────────────────────────────

def _remote_path(local_path: str, upload_folder: str, artifacts_dir: str) -> str | None:
    """Map a local absolute path to a Supabase object path.

    /tmp/uploads/raw/file.xlsx      →  files/uploads/raw/file.xlsx
    /tmp/artifacts/reports/r.pdf    →  files/artifacts/reports/r.pdf
    """
    for base, folder in ((upload_folder, 'uploads'), (artifacts_dir, 'artifacts')):
        base = os.path.normpath(base)
        norm = os.path.normpath(local_path)
        if norm.startswith(base):
            relative = norm[len(base):].lstrip(os.sep).replace(os.sep, '/')
            return f'{_prefix}/{folder}/{relative}'
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Core operations
# ─────────────────────────────────────────────────────────────────────────────

def upload(local_path: str, upload_folder: str, artifacts_dir: str) -> bool:
    """Upload a local file to Supabase Storage. Returns True on success."""
    if not is_available():
        return False
    remote = _remote_path(local_path, upload_folder, artifacts_dir)
    if not remote:
        logger.warning('[Storage] Cannot map remote path for: %s', local_path)
        return False
    try:
        with open(local_path, 'rb') as f:
            data = f.read()
        # upsert=True overwrites if the same filename was uploaded before
        _client.storage.from_(_bucket).upload(
            path=remote,
            file=data,
            file_options={'upsert': 'true'},
        )
        logger.info('[Storage] Uploaded  %s  →  %s', os.path.basename(local_path), remote)
        return True
    except Exception as exc:
        logger.error('[Storage] Upload failed for %s: %s', local_path, exc)
        return False


def download(local_path: str, upload_folder: str, artifacts_dir: str) -> bool:
    """Download a file from Supabase to restore a missing local file."""
    if not is_available():
        return False
    remote = _remote_path(local_path, upload_folder, artifacts_dir)
    if not remote:
        return False
    try:
        data = _client.storage.from_(_bucket).download(remote)
        if not data:
            return False
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, 'wb') as f:
            f.write(data)
        logger.info('[Storage] Restored  %s  ←  %s', os.path.basename(local_path), remote)
        return True
    except Exception as exc:
        logger.error('[Storage] Download failed for %s: %s', local_path, exc)
        return False


def get_download_url(local_path: str, upload_folder: str, artifacts_dir: str,
                     expiry_seconds: int = 43200) -> str | None:
    """Return a time-limited signed URL for direct browser download (default 12 h).

    Returns None if Supabase is unavailable — caller falls back to send_file().
    """
    if not is_available():
        return None
    remote = _remote_path(local_path, upload_folder, artifacts_dir)
    if not remote:
        return None
    try:
        result = _client.storage.from_(_bucket).create_signed_url(remote, expiry_seconds)
        url = result.get('signedURL') or result.get('signed_url')
        return url
    except Exception as exc:
        logger.warning('[Storage] Signed URL failed (%s) — will stream locally.', exc)
        return None


def delete(local_path: str, upload_folder: str, artifacts_dir: str) -> bool:
    """Delete a file from Supabase Storage (called by retention cleanup)."""
    if not is_available():
        return False
    remote = _remote_path(local_path, upload_folder, artifacts_dir)
    if not remote:
        return False
    try:
        _client.storage.from_(_bucket).remove([remote])
        logger.info('[Storage] Deleted  %s', remote)
        return True
    except Exception as exc:
        logger.error('[Storage] Delete failed for %s: %s', local_path, exc)
        return False
