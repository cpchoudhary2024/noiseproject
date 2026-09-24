# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""Build, store and apply weather screens for an uploaded noise record.

A screen is computed once per (file, station, rules) and saved server-side as
JSON. Requests refer to it by ID, so the exclusion list cannot be altered by the
browser, and every later analysis or report on the same file applies exactly
the same screen. The record is bound to the file by a fingerprint (SHA-256 of the full ordered timestamp sequence, plus row count and range); applying it to any other data is refused.

The participant's location is used only to rank stations. It is not stored in
the record and never reaches a report.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from datetime import datetime, timezone

import numpy as np
import pandas as pd

from analysis.weather_screen import (
    CLOCK_MODES, REASONS, REASON_LABELS, ScreenConfig, _classify_ptype, assess_identifier, block_keys,
    check_clock_setting, classify_blocks, local_to_utc, minute_states, parse_metar_weather, screen_mask,
    snow_cover_status, utc_to_local, wind_height_factor,
)
from services.weather_sources import (
    WeatherDataUnavailable, fetch_ghcn_daily, fetch_metar, fetch_onemin, nearest_snow_stations, haversine_km,
)

logger = logging.getLogger(__name__)

RECORD_VERSION = 2
_ID_RE = re.compile(r"^[0-9a-f]{16}$")

# Days of GHCN-Daily history fetched before the record, so a missing snow depth
# on the first days can be resolved from the last reported value.
SNOW_LOOKBACK_DAYS = 45

# Snow depth is not an ASOS measurement. When no station within this radius
# reports it, snow cover remains unverified.
SNOW_SEARCH_RADIUS_KM = 100.0
# Share of the period a snow-depth record must cover to settle snow cover.
# Volunteer observers report irregularly, which would leave most days unknown
# and therefore excluded; airport and cooperative stations report daily.
SNOW_MIN_COVERAGE = 0.9
# Candidate stations tried before giving up (each is one request).
SNOW_MAX_CANDIDATES = 6
SOURCES_TEXT = (
    "NOAA/FAA ASOS 1-minute observations (NCEI DSI-6405/6406) and ASOS METAR reports "
    "(5-minute, routine and special), retrieved from the Iowa Environmental Mesonet; "
    "NOAA GHCN-Daily snow depth and snowfall, retrieved from the NCEI Access Data Service."
)


class WeatherScreenError(ValueError):
    """The screen could not be applied as requested."""


def fingerprint(ts: pd.Series) -> dict:
    """Identity of the full ordered timestamp sequence, including missing times."""
    valid = ts.dropna()
    digest = hashlib.sha256(pd.DatetimeIndex(ts).as_unit("ns").asi8.tobytes()).hexdigest()
    return {"timestamp_sha256": digest, "rows": int(len(ts)),
            "start": valid.min().isoformat() if not valid.empty else None,
            "end": valid.max().isoformat() if not valid.empty else None}


def record_tz(rec: dict) -> str:
    """Time zone the logger clock is read in: the site's, which may differ from
    the station's near a zone boundary."""
    return rec.get("tz") or rec["station"]["tz"]


def _record_path(store_dir: str, screen_id: str, ext: str) -> str:
    if not _ID_RE.match(str(screen_id or "")):
        raise WeatherScreenError("Invalid weather screen reference.")
    return os.path.join(store_dir, f"{screen_id}.{ext}")


def load_record(store_dir: str, screen_id: str) -> dict:
    path = _record_path(store_dir, screen_id, "json")
    if not os.path.exists(path):
        raise WeatherScreenError(
            "The weather screen for this analysis is no longer on the server. "
            "Run weather screening again before analysing or downloading reports.")
    with open(path, encoding="utf-8") as f:
        rec = json.load(f)
    if rec.get("version") != RECORD_VERSION:
        raise WeatherScreenError("This weather screen was made by an older version. Run it again.")
    return rec


def audit_csv_path(store_dir: str, screen_id: str) -> str:
    return _record_path(store_dir, screen_id, "csv")


def apply_record(ts: pd.Series, rec: dict) -> tuple[np.ndarray, pd.Series]:
    """Keep-mask and reasons for ``ts`` (the file's full parsed timestamps).

    Raises WeatherScreenError if ``ts`` is not the record the screen was built for.
    """
    fp = fingerprint(ts)
    if fp != rec["fingerprint"]:
        raise WeatherScreenError(
            "This weather screen was computed for a different file (or a different "
            "version of it). Run weather screening again for the current file.")
    excluded = {int(k): v for k, v in rec["excluded"].items()}
    return screen_mask(ts, excluded, tz=record_tz(rec), clock=rec["clock"],
                       block_minutes=rec["config"]["block_minutes"],
                       range_blocks=tuple(rec["range_blocks"]))


def _snow_record(station: dict, cache_dir: str, local_from, local_to,
                 site: tuple[float, float] | None) -> tuple[pd.DataFrame, dict]:
    """Daily snow depth for the period, from the nearest station that reports it.

    Returns (daily, source). ``source['mode']`` is 'depth' when measured depth
    was obtained. Uncovered days remain unverified.
    """
    lat, lon = site or (station["lat"], station["lon"])
    candidates = []
    if station.get("ghcnd_id"):
        candidates.append({"ghcnd_id": station["ghcnd_id"],
                           "distance_km": haversine_km(lat, lon, station["lat"], station["lon"])})
    seen = {c["ghcnd_id"] for c in candidates}
    for c in nearest_snow_stations(lat, lon, local_from.year, cache_dir,
                                   max_km=SNOW_SEARCH_RADIUS_KM):
        if c["ghcnd_id"] not in seen:
            candidates.append(c)
    days = max(1, (local_to - local_from).days + 1)
    best = (0.0, pd.DataFrame(columns=["date", "snwd_mm", "snow_mm"]), {})
    candidates = sorted((c for c in candidates if c["distance_km"] <= SNOW_SEARCH_RADIUS_KM),
                        key=lambda c: c["distance_km"])
    for cand in candidates[:SNOW_MAX_CANDIDATES]:
        try:
            daily = fetch_ghcn_daily(cand["ghcnd_id"], local_from, local_to, cache_dir)
        except WeatherDataUnavailable:
            continue
        if daily.empty:
            continue
        coverage = float(daily["snwd_mm"].notna().sum()) / days
        source = {"mode": "depth", "ghcnd_id": cand["ghcnd_id"],
                  "distance_km": cand["distance_km"], "coverage_pct": round(100 * coverage, 1)}
        if coverage >= SNOW_MIN_COVERAGE:
            return daily, source
        if coverage > best[0]:
            best = (coverage, daily, source)
    # Nothing complete enough: hand back the best partial record, if any. The days
    # it does not cover stay unknown, and unknown is treated as affected.
    return best[1], (best[2] or {"mode": "none"})


def build_screen(ts: pd.Series, station: dict, cfg: ScreenConfig, clock: str,
                 *, cache_dir: str, store_dir: str, location_basis: str, tz: str | None = None,
                 site: tuple[float, float] | None = None,
                 progress=lambda pct, msg: None) -> dict:
    """Fetch weather, classify blocks and (when usable) save the screen.

    Parameters
    ----------
    ts : the uploaded file's parsed timestamps (naive logger clock), all rows.
    station : one entry from ``nearest_stations``.
    tz : IANA zone the logger clock is in. Defaults to the station's zone, which
        is wrong when the site sits across a time-zone boundary from it.

    Returns the record dict. ``record['usable']`` is False when no sample
    survives; such a record is not saved and cannot be applied.

    Raises WeatherDataUnavailable when the weather needed cannot be obtained.
    """
    cfg.validate()
    if clock not in CLOCK_MODES:
        raise WeatherScreenError(f"Unknown logger clock setting '{clock}'.")
    tz = tz or station["tz"]
    valid = ts.dropna()
    if valid.empty:
        raise WeatherScreenError("This file has no readable timestamps, so it cannot be matched to weather.")
    contradiction = check_clock_setting(valid, tz, clock)
    if contradiction:
        raise WeatherScreenError(contradiction)
    utc = local_to_utc(valid, tz, clock).dropna()
    if utc.empty:
        raise WeatherScreenError("None of this file's timestamps could be placed in UTC.")

    blk = pd.Timedelta(minutes=cfg.block_minutes)
    pad = blk * (max(cfg.buffer_before_blocks, cfg.buffer_after_blocks) + 1)
    rec_start, rec_end = utc.min().floor(blk), utc.max().floor(blk)
    fetch_start = (rec_start - pad).to_pydatetime()
    fetch_end = (rec_end + blk + pad).to_pydatetime()

    progress(15, f"Fetching 1-minute observations from {station['station_id']}…")
    onemin = fetch_onemin(station["station_id"], fetch_start, fetch_end, cache_dir)
    progress(40, f"Fetching 5-minute reports from {station['station_id']}…")
    metar = fetch_metar(station["station_id"], fetch_start, fetch_end, cache_dir)
    if onemin.empty and metar.empty:
        raise WeatherDataUnavailable(
            f"Station {station['station_id']} ({station['name']}) has no archived observations for "
            f"{fetch_start:%d %b %Y} to {fetch_end:%d %b %Y} (UTC). Weather screening was not applied. "
            "Choose another station or try again later.")

    progress(60, "Fetching daily snow record…")
    local = pd.DatetimeIndex(utc_to_local(pd.DatetimeIndex([fetch_start, fetch_end]), tz, "local_dst"))
    daily, snow_source = _snow_record(
        station, cache_dir, (local[0] - pd.Timedelta(days=SNOW_LOOKBACK_DAYS)).date(),
        (local[1] + pd.Timedelta(days=1)).date(), site)
    progress(75, "Classifying weather blocks…")
    # Compare sources diagnostically; disagreement never silently clears precipitation.
    quality = assess_identifier(onemin, metar)
    minutes = minute_states(onemin, metar, trust_identifier=quality["reliable"])
    ext_dates = pd.DatetimeIndex(utc_to_local(
        pd.date_range(fetch_start, fetch_end, freq=blk), tz, "local_dst")).normalize()
    snow = snow_cover_status(daily, [d.date() for d in ext_dates.unique()], cfg)
    blocks = classify_blocks(minutes, pd.Timestamp(fetch_start), pd.Timestamp(fetch_end) - pd.Timedelta(seconds=1),
                             cfg, snow, tz, clock)
    excluded = {str(int(k)): r for k, r in blocks["reason"].items() if r}
    range_blocks = (int(block_keys(pd.DatetimeIndex([rec_start]), cfg.block_minutes)[0]),
                    int(block_keys(pd.DatetimeIndex([rec_end]), cfg.block_minutes)[0]))

    progress(88, "Applying screen to samples…")
    keep, reasons = screen_mask(valid, {int(k): v for k, v in excluded.items()}, tz=tz, clock=clock,
                                block_minutes=cfg.block_minutes, range_blocks=range_blocks)
    in_rec = blocks.loc[range_blocks[0]:range_blocks[1]]

    summary = _summary(ts, valid, keep, reasons, in_rec, minutes, onemin, metar, cfg, range_blocks)
    summary["identifier_quality"] = quality
    record = {
        "version": RECORD_VERSION,
        "screen_id": uuid.uuid4().hex[:16],
        "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "station": {k: station.get(k) for k in
                    ("station_id", "name", "lat", "lon", "distance_km", "tz", "network", "ghcnd_id")},
        "location_basis": location_basis,
        "snow_source": snow_source,
        "tz": tz,
        "clock": clock,
        "config": cfg.to_dict(),
        "wind_height_factor": round(wind_height_factor(cfg.mic_height_m, cfg.anemometer_height_m,
                                                       cfg.roughness_length_m), 4),
        "range_blocks": list(range_blocks),
        "fingerprint": fingerprint(ts),
        "excluded": excluded,
        "identifier_quality": quality,
        "summary": summary,
        "sources": SOURCES_TEXT,
        "usable": summary["samples_kept"] > 0,
    }
    if record["usable"]:
        os.makedirs(store_dir, exist_ok=True)
        path = _record_path(store_dir, record["screen_id"], "json")
        with open(path + ".part", "w", encoding="utf-8") as f:
            json.dump(record, f)
        os.replace(path + ".part", path)
        _write_audit_csv(audit_csv_path(store_dir, record["screen_id"]), in_rec, tz, clock)
    progress(100, "Weather screen ready")
    return record


def _summary(ts, valid, keep, reasons, in_rec, minutes, onemin, metar, cfg, range_blocks) -> dict:
    n_total = int(len(ts))
    n_unreadable = n_total - int(len(valid))
    by_reason = reasons.value_counts().to_dict()
    blk_counts = in_rec["reason"].value_counts().to_dict()
    n_blocks = int(len(in_rec))
    # Cross-source agreement on precipitation state, where both observed a minute.
    both = minutes[minutes["src_1min"] & minutes["src_metar"]]
    agree = None
    if not onemin.empty and not metar.empty and not both.empty:
        o = onemin.assign(minute=onemin["valid_utc"].dt.floor("min")).set_index("minute")
        o = o[~o.index.duplicated(keep="last")]
        m = metar.assign(minute=metar["valid_utc"].dt.floor("min")).drop_duplicates("minute", keep="last").set_index("minute")
        idx = o.index.intersection(m.index)
        s1 = pd.Series([_classify_ptype(p)[0] for p in o.loc[idx, "ptype"]], index=idx)
        tip = o.loc[idx, "precip"].fillna(0) > 0
        s1[tip] = 1.0
        s2 = pd.Series([parse_metar_weather(a, b)["state"] for a, b in zip(m.loc[idx, "wxcodes"], m.loc[idx, "metar"])], index=idx)
        ok = s1.notna() & s2.notna()
        if ok.any():
            agree = {"minutes_compared": int(ok.sum()),
                     "agree_pct": round(100.0 * float((s1[ok] == s2[ok]).mean()), 1),
                     "both_precip": int(((s1 == 1) & (s2 == 1))[ok].sum()),
                     "only_1min_precip": int(((s1 == 1) & (s2 == 0))[ok].sum()),
                     "only_metar_precip": int(((s1 == 0) & (s2 == 1))[ok].sum())}
    mins_in = minutes[(block_keys(minutes.index, cfg.block_minutes) >= range_blocks[0])
                      & (block_keys(minutes.index, cfg.block_minutes) <= range_blocks[1])] if not minutes.empty else minutes
    return {
        "samples_total": n_total,
        "samples_unreadable_time": n_unreadable,
        "samples_kept": int(keep.sum()),
        "samples_excluded": int((~keep).sum()) + n_unreadable,
        "pct_kept": round(100.0 * float(keep.sum()) / max(1, n_total), 2),
        "samples_by_reason": {r: int(by_reason.get(r, 0)) + (n_unreadable if r == "clock_ambiguous" else 0)
                              for r in REASONS if by_reason.get(r) or (r == "clock_ambiguous" and n_unreadable)},
        "blocks_total": n_blocks,
        "blocks_clean": int(in_rec["reason"].isna().sum()),
        "blocks_by_reason": {r: int(blk_counts.get(r, 0)) for r in REASONS if blk_counts.get(r)},
        "blocks_with_1min_pct": round(100.0 * float((in_rec["n_1min"] > 0).mean()), 1) if n_blocks else 0.0,
        "blocks_verified_pct": round(100.0 * float(in_rec["verified"].mean()), 1) if n_blocks else 0.0,
        "precip_minutes": {"rain": int(mins_in["rain"].sum()), "snow": int(mins_in["snow"].sum()),
                           "other": int(mins_in["other"].sum())} if not mins_in.empty else {},
        "max_block_wind_mic_ms": (round(float(in_rec["wind_mic_ms"].max()), 2)
                                  if in_rec["wind_mic_ms"].notna().any() else None),
        "source_agreement": agree,
        "record_start_local": valid.min().isoformat(),
        "record_end_local": valid.max().isoformat(),
        "reason_labels": {r: REASON_LABELS[r] for r in REASONS},
    }


def _write_audit_csv(path: str, in_rec: pd.DataFrame, tz: str, clock: str) -> None:
    """Block-by-block audit trail, in the logger's clock and in UTC."""
    out = pd.DataFrame({
        "block_start_logger_clock": pd.DatetimeIndex(utc_to_local(in_rec["block_start_utc"], tz, clock)).strftime("%Y-%m-%d %H:%M"),
        "block_start_utc": in_rec["block_start_utc"].dt.strftime("%Y-%m-%d %H:%M"),
        "status": in_rec["reason"].fillna("clean"),
        "weather_verified": in_rec["verified"],
        "precipitation": in_rec["precip"], "rain": in_rec["rain"], "snow": in_rec["snow"],
        "other_precip": in_rec["other"], "thunder": in_rec["thunder"],
        "estimated_mean_wind_at_mic_m_s": in_rec["wind_mic_ms"].round(2),
        "maximum_station_wind_or_gust_m_s": in_rec["wind_station_max_ms"].round(2),
        "minutes_1min_obs": in_rec["n_1min"], "minutes_metar_obs": in_rec["n_metar"],
    })
    out.to_csv(path + ".part", index=False)
    os.replace(path + ".part", path)
