import os
import re
from dataclasses import dataclass
from typing import Iterable


_TIMESTAMP_RE = re.compile(r"^(?P<ts>\d{8}_\d{6})")


def _safe_listdir(directory: str) -> list[str]:
    try:
        return os.listdir(directory)
    except FileNotFoundError:
        return []


def _mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


@dataclass(frozen=True)
class RetentionPolicy:
    keep_raw_uploads: int = 15
    keep_charts_html: int = 15
    keep_reports: int = 15
    enabled: bool = True


def _is_raw_upload(filename: str) -> bool:
    return bool(re.match(r"^\d{8}_\d{6}_.+\.(csv|xlsx|xls)$", filename, re.IGNORECASE))


def _is_chart_html(filename: str) -> bool:
    return bool(re.match(r"^charts_\d{8}_\d{6}\.html$", filename, re.IGNORECASE))


def _is_report_file(filename: str) -> bool:
    return bool(re.match(r"^noise_analysis_.+_\d{8}_\d{6}\.(pdf|html)$", filename, re.IGNORECASE))


def _apply_retention(
    directory: str,
    predicate,
    keep: int,
    keep_paths: set[str],
) -> tuple[int, int]:
    """Delete older matching files in directory.

    Returns (deleted_count, kept_count).
    """
    if keep < 0:
        keep = 0

    candidates: list[tuple[float, str]] = []
    for name in _safe_listdir(directory):
        if not predicate(name):
            continue
        path = os.path.abspath(os.path.join(directory, name))
        if path in keep_paths:
            continue
        candidates.append((_mtime(path), path))

    # Newest first
    candidates.sort(key=lambda x: x[0], reverse=True)

    kept = candidates[:keep]
    to_delete = candidates[keep:]

    deleted_count = 0
    for _, path in to_delete:
        try:
            os.remove(path)
            deleted_count += 1
        except OSError:
            # Best-effort deletion: never break app behavior.
            continue

    return deleted_count, len(kept)


def enforce_retention(
    uploads_dir: str,
    root_dir: str | None = None,
    raw_uploads_dir: str | None = None,
    artifacts_reports_dir: str | None = None,
    artifacts_charts_dir: str | None = None,
    policy: RetentionPolicy | None = None,
    keep_paths: Iterable[str] = (),
) -> dict:
    """Enforce retention for generated artifacts.

    Safety properties:
    - Only deletes files in uploads_dir and (optionally) root_dir.
    - Only deletes files matching known generated filename patterns.
    - Best-effort: never raises on delete failures.

    Categories:
    - Raw uploads: YYYYMMDD_HHMMSS_<original>.(csv|xlsx|xls)
    - Charts: charts_YYYYMMDD_HHMMSS.html
    - Reports: noise_analysis_*_YYYYMMDD_HHMMSS.(pdf|html)
    """

    policy = policy or RetentionPolicy()
    if not policy.enabled:
        return {"enabled": False}

    uploads_dir = os.path.abspath(uploads_dir)
    root_dir = os.path.abspath(root_dir) if root_dir else None

    raw_uploads_dir = os.path.abspath(raw_uploads_dir) if raw_uploads_dir else None
    artifacts_reports_dir = os.path.abspath(artifacts_reports_dir) if artifacts_reports_dir else None
    artifacts_charts_dir = os.path.abspath(artifacts_charts_dir) if artifacts_charts_dir else None

    keep_set = {os.path.abspath(p) for p in keep_paths if p}

    os.makedirs(uploads_dir, exist_ok=True)

    # Backwards-compatible defaults: if callers don't provide specific dirs,
    # assume everything lives directly under uploads_dir.
    raw_dir = raw_uploads_dir or uploads_dir
    charts_dir = artifacts_charts_dir or uploads_dir
    reports_dir = artifacts_reports_dir or uploads_dir

    if raw_dir:
        os.makedirs(raw_dir, exist_ok=True)
    if charts_dir:
        os.makedirs(charts_dir, exist_ok=True)
    if reports_dir:
        os.makedirs(reports_dir, exist_ok=True)

    deleted_raw, kept_raw = _apply_retention(raw_dir, _is_raw_upload, policy.keep_raw_uploads, keep_set)
    deleted_charts, kept_charts = _apply_retention(charts_dir, _is_chart_html, policy.keep_charts_html, keep_set)
    deleted_reports, kept_reports = _apply_retention(reports_dir, _is_report_file, policy.keep_reports, keep_set)

    deleted_root_reports = 0
    kept_root_reports = 0
    if root_dir and os.path.isdir(root_dir):
        dr, kr = _apply_retention(root_dir, _is_report_file, policy.keep_reports, keep_set)
        dc, kc = _apply_retention(root_dir, _is_chart_html, policy.keep_charts_html, keep_set)
        deleted_root_reports = dr + dc
        kept_root_reports = kr + kc

    return {
        "enabled": True,
        "uploads_dir": uploads_dir,
        "root_dir": root_dir,
        "raw_uploads_dir": raw_dir,
        "artifacts_reports_dir": reports_dir,
        "artifacts_charts_dir": charts_dir,
        "policy": {
            "keep_raw_uploads": policy.keep_raw_uploads,
            "keep_charts_html": policy.keep_charts_html,
            "keep_reports": policy.keep_reports,
        },
        "deleted": {
            "uploads_raw": deleted_raw,
            "uploads_charts": deleted_charts,
            "uploads_reports": deleted_reports,
            "root_generated": deleted_root_reports,
        },
        "kept": {
            "uploads_raw": kept_raw,
            "uploads_charts": kept_charts,
            "uploads_reports": kept_reports,
            "root_generated": kept_root_reports,
        },
    }
