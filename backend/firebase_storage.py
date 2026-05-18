"""
Firebase Cloud Storage integration for the Noise Analysis Platform.

Provides transparent file persistence across server restarts on Render's
ephemeral filesystem. When configured (FIREBASE_CREDENTIALS + FIREBASE_BUCKET
env vars), every uploaded file and generated report is mirrored to Firebase
Storage so it survives sleep cycles and re-deploys.

If Firebase is not configured the module is a no-op and the app works
exactly as before (local filesystem only).
"""

import os
import json
import logging

logger = logging.getLogger(__name__)

_bucket = None          # google.cloud.storage.Bucket, set by init()
_base_prefix = 'noise-analysis-platform'   # top-level folder in the bucket


# ─────────────────────────────────────────────────────────────────────────────
# Initialisation
# ─────────────────────────────────────────────────────────────────────────────

def init():
    """Initialise Firebase Admin SDK from environment variables.

    Required env vars:
        FIREBASE_CREDENTIALS  – full JSON string of a Firebase service-account key
        FIREBASE_BUCKET       – Storage bucket name, e.g. my-project.appspot.com

    Returns True on success, False if not configured or on error.
    """
    global _bucket

    cred_json = os.environ.get('FIREBASE_CREDENTIALS', '').strip()
    bucket_name = os.environ.get('FIREBASE_BUCKET', '').strip()

    if not cred_json or not bucket_name:
        logger.info('[Firebase] Not configured — running in local-only mode.')
        return False

    try:
        import firebase_admin
        from firebase_admin import credentials, storage as fb_storage

        if not firebase_admin._apps:
            cred_data = json.loads(cred_json)
            cred = credentials.Certificate(cred_data)
            firebase_admin.initialize_app(cred, {'storageBucket': bucket_name})

        _bucket = fb_storage.bucket()
        logger.info('[Firebase] Storage initialised — bucket: %s', bucket_name)
        return True

    except Exception as exc:
        logger.error('[Firebase] Init failed: %s', exc)
        _bucket = None
        return False


def is_available() -> bool:
    """Return True if Firebase Storage is ready to use."""
    return _bucket is not None


# ─────────────────────────────────────────────────────────────────────────────
# Core operations
# ─────────────────────────────────────────────────────────────────────────────

def _remote_path(local_path: str, upload_folder: str, artifacts_dir: str) -> str | None:
    """Derive a Firebase object path from a local absolute path.

    Maps:
        /tmp/uploads/raw/file.xlsx  →  noise-analysis-platform/uploads/raw/file.xlsx
        /tmp/artifacts/reports/x.pdf →  noise-analysis-platform/artifacts/reports/x.pdf
    """
    for base in (upload_folder, artifacts_dir):
        base = os.path.normpath(base)
        norm = os.path.normpath(local_path)
        if norm.startswith(base):
            relative = norm[len(base):].lstrip(os.sep)
            folder = 'uploads' if base == os.path.normpath(upload_folder) else 'artifacts'
            return f'{_base_prefix}/{folder}/{relative}'
    return None


def upload(local_path: str, upload_folder: str, artifacts_dir: str) -> bool:
    """Upload a local file to Firebase Storage.

    Args:
        local_path:     Absolute path on the local filesystem.
        upload_folder:  Value of UPLOAD_FOLDER from app config.
        artifacts_dir:  Value of ARTIFACTS_DIR from app config.

    Returns True on success.
    """
    if not is_available():
        return False
    remote = _remote_path(local_path, upload_folder, artifacts_dir)
    if not remote:
        logger.warning('[Firebase] Cannot derive remote path for: %s', local_path)
        return False
    try:
        blob = _bucket.blob(remote)
        blob.upload_from_filename(local_path)
        logger.info('[Firebase] Uploaded  %s  →  %s', os.path.basename(local_path), remote)
        return True
    except Exception as exc:
        logger.error('[Firebase] Upload failed for %s: %s', local_path, exc)
        return False


def download(local_path: str, upload_folder: str, artifacts_dir: str) -> bool:
    """Download a file from Firebase Storage to restore a missing local file.

    Returns True if the file was successfully restored.
    """
    if not is_available():
        return False
    remote = _remote_path(local_path, upload_folder, artifacts_dir)
    if not remote:
        return False
    try:
        blob = _bucket.blob(remote)
        if not blob.exists():
            return False
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        blob.download_to_filename(local_path)
        logger.info('[Firebase] Restored  %s  ←  %s', os.path.basename(local_path), remote)
        return True
    except Exception as exc:
        logger.error('[Firebase] Download failed for %s: %s', local_path, exc)
        return False


def get_download_url(local_path: str, upload_folder: str, artifacts_dir: str,
                     expiry_hours: int = 12) -> str | None:
    """Return a time-limited signed URL for direct browser download.

    Falls back to None if Firebase is unavailable (caller uses send_file).
    """
    if not is_available():
        return None
    remote = _remote_path(local_path, upload_folder, artifacts_dir)
    if not remote:
        return None
    try:
        from datetime import timedelta
        import google.auth.transport.requests
        import google.oauth2.service_account

        blob = _bucket.blob(remote)
        # Use service-account credentials for v4 signed URLs
        url = blob.generate_signed_url(
            version='v4',
            expiration=timedelta(hours=expiry_hours),
            method='GET',
        )
        return url
    except Exception as exc:
        logger.warning('[Firebase] Signed URL failed (%s) — will stream locally.', exc)
        return None


def delete(local_path: str, upload_folder: str, artifacts_dir: str) -> bool:
    """Delete a file from Firebase Storage (called by retention cleanup)."""
    if not is_available():
        return False
    remote = _remote_path(local_path, upload_folder, artifacts_dir)
    if not remote:
        return False
    try:
        blob = _bucket.blob(remote)
        if blob.exists():
            blob.delete()
            logger.info('[Firebase] Deleted  %s', remote)
        return True
    except Exception as exc:
        logger.error('[Firebase] Delete failed for %s: %s', local_path, exc)
        return False
