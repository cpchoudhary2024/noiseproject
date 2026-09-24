# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""Retrieval of official surface weather observations for noise-data screening.

Sources (all public, no key required)
-------------------------------------
* NOAA/FAA ASOS 1-minute observations (NCEI DSI-6405/6406) via the Iowa
  Environmental Mesonet (IEM): 2-minute average wind, peak 5-second gust,
  precipitation identifier and 1-minute precipitation amount.
* ASOS METAR reports via IEM: 5-minute high-frequency (HFMETAR), routine and
  special reports from the same station and sensors. The 1-minute archive has
  multi-hour gaps (NCEI retrieves the on-station 12-hour buffer by modem), and
  these reports fill them.
* NOAA GHCN-Daily snow depth (SNWD) and snowfall (SNOW) via the NCEI Access
  Data Service. ASOS cannot measure snow on the ground.
* Station index: NCEI ISD station history (ICAO -> WBAN) and IEM network
  metadata (coordinates, time zone, ASOS vs AWOS).
* ZIP-code centroids: US Census 2020 ZCTA Gazetteer, downloaded once and used
  offline, so a participant's location is never sent to a geocoding service.

Every failure raises :class:`WeatherDataUnavailable`. Callers must stop rather
than screen with partial or substituted weather.
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import os
import re
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timedelta, timezone

import pandas as pd

logger = logging.getLogger(__name__)

IEM_BASE = "https://mesonet.agron.iastate.edu"
ONEMIN_URL = f"{IEM_BASE}/cgi-bin/request/asos1min.py"
METAR_URL = f"{IEM_BASE}/cgi-bin/request/asos.py"
NETWORK_URL = f"{IEM_BASE}/geojson/network/{{network}}.geojson"
NCEI_DAILY_URL = "https://www.ncei.noaa.gov/access/services/data/v1"
ISD_HISTORY_URL = "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv"
GHCN_INVENTORY_URL = "https://www.ncei.noaa.gov/pub/data/ghcn/daily/ghcnd-inventory.txt"
ZCTA_URL = ("https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
            "2020_Gazetteer/2020_Gaz_zcta_national.zip")

USER_AGENT = "noise-analysis-platform/weather-screening (research use)"
HTTP_TIMEOUT_S = 180
HTTP_RETRIES = 3

# A chunk is cached permanently only once it can no longer change: its end is
# at least this far in the past, so late-arriving archive data has landed.
SETTLED_AFTER = timedelta(days=7)
# Station metadata and the ZCTA/ISD indexes change rarely.
INDEX_MAX_AGE = timedelta(days=30)
# Unsettled observation chunks are reused briefly, so repeated screens of the
# same file in one session see identical inputs.
UNSETTLED_MAX_AGE = timedelta(hours=1)

EARTH_RADIUS_KM = 6371.0088


class WeatherDataUnavailable(RuntimeError):
    """Weather data needed for screening could not be obtained or verified."""


class LocationError(ValueError):
    """The location text could not be resolved to coordinates."""


# ── HTTP + cache ─────────────────────────────────────────────────────────────

_SYSTEM_CA_BUNDLES = ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt",
                      "/etc/pki/tls/certs/ca-bundle.crt")


def _ssl_context() -> ssl.SSLContext:
    """Verifying TLS context. Python.org builds on macOS ship without a CA store;
    fall back to the operating system bundle rather than ever disabling checks."""
    ctx = ssl.create_default_context()
    if ctx.cert_store_stats().get("x509_ca", 0) == 0:
        for bundle in _SYSTEM_CA_BUNDLES:
            if os.path.exists(bundle):
                ctx.load_verify_locations(bundle)
                break
    return ctx


def _http_get(url: str, params: dict | list | None = None) -> bytes:
    query = urllib.parse.urlencode(params or {}, doseq=True)
    full = f"{url}?{query}" if query else url
    last_err: Exception | None = None
    for attempt in range(HTTP_RETRIES):
        try:
            req = urllib.request.Request(full, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S, context=_ssl_context()) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_err = exc
            if exc.code not in (429, 500, 502, 503, 504):
                break
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_err = exc
        if attempt < HTTP_RETRIES - 1:
            time.sleep(3 * (3 ** attempt))
    raise WeatherDataUnavailable(
        f"Could not reach {urllib.parse.urlsplit(url).netloc} ({last_err}). "
        "Weather screening needs this service; try again later."
    )


def _cache_path(cache_dir: str, name: str) -> str:
    os.makedirs(cache_dir, exist_ok=True)
    return os.path.join(cache_dir, re.sub(r"[^A-Za-z0-9_.-]", "_", name))


def _cached_fetch(cache_dir: str, name: str, fetch, *, permanent: bool,
                  max_age: timedelta | None = None) -> bytes:
    """Return cached bytes when valid, else fetch and (if allowed) store them.

    Permanent and time-limited copies are kept under different names, so data
    cached before its period settled is never later served as final.
    """
    path = _cache_path(cache_dir, name if permanent else f"{name}.recent")
    if os.path.exists(path):
        age = datetime.now() - datetime.fromtimestamp(os.path.getmtime(path))
        if permanent or (max_age is not None and age <= max_age):
            with open(path, "rb") as f:
                return f.read()
    data = fetch()
    if permanent or max_age is not None:
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    return data


def _months(start: datetime, end: datetime):
    """Yield (month_start, next_month_start) for every UTC month touching [start, end)."""
    cur = datetime(start.year, start.month, 1)
    while cur < end:
        nxt = datetime(cur.year + (cur.month == 12), cur.month % 12 + 1, 1)
        yield cur, nxt
        cur = nxt


def _settled(chunk_end: datetime) -> bool:
    return chunk_end + SETTLED_AFTER < datetime.now(timezone.utc).replace(tzinfo=None)


# ── Location ─────────────────────────────────────────────────────────────────

_LATLON_RE = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*[,\s]\s*(-?\d+(?:\.\d+)?)\s*$")
_ZIP_RE = re.compile(r"^\s*(\d{5})(?:-\d{4})?\s*$")


def _load_zcta_centroids(cache_dir: str) -> dict[str, tuple[float, float]]:
    raw = _cached_fetch(cache_dir, "zcta_2020.zip", lambda: _http_get(ZCTA_URL),
                        permanent=True)
    with zipfile.ZipFile(io.BytesIO(raw)) as zf:
        name = next(n for n in zf.namelist() if n.endswith(".txt"))
        text = zf.read(name).decode("utf-8", errors="replace")
    out: dict[str, tuple[float, float]] = {}
    reader = csv.reader(io.StringIO(text), delimiter="\t")
    header = [h.strip() for h in next(reader)]
    i_id, i_lat, i_lon = header.index("GEOID"), header.index("INTPTLAT"), header.index("INTPTLONG")
    for row in reader:
        if len(row) > max(i_id, i_lat, i_lon):
            out[row[i_id].strip()] = (float(row[i_lat]), float(row[i_lon]))
    return out


def resolve_location(text: str, cache_dir: str) -> tuple[float, float, str]:
    """Resolve a 5-digit US ZIP code or a 'lat, lon' pair to coordinates.

    Returns
    -------
    (lat_deg, lon_deg, basis) where basis is 'ZIP code centroid' or 'coordinates'.
    """
    text = str(text or "").strip()
    m = _LATLON_RE.match(text)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise LocationError("Latitude must be -90 to 90 and longitude -180 to 180.")
        return lat, lon, "coordinates"
    m = _ZIP_RE.match(text)
    if m:
        centroids = _load_zcta_centroids(cache_dir)
        if m.group(1) not in centroids:
            raise LocationError(f"ZIP code {m.group(1)} is not in the US Census ZIP-area list.")
        lat, lon = centroids[m.group(1)]
        return lat, lon, "ZIP code centroid"
    raise LocationError("Enter a 5-digit US ZIP code or coordinates as 'latitude, longitude'.")


# ── Station index ────────────────────────────────────────────────────────────

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def _load_isd_history(cache_dir: str) -> pd.DataFrame:
    raw = _cached_fetch(cache_dir, "isd-history.csv", lambda: _http_get(ISD_HISTORY_URL),
                        permanent=False, max_age=INDEX_MAX_AGE)
    df = pd.read_csv(io.BytesIO(raw), dtype=str, keep_default_na=False)
    df = df[(df["CTRY"] == "US") & (df["ICAO"].str.len() >= 3)].copy()
    df["LAT"] = pd.to_numeric(df["LAT"], errors="coerce")
    df["LON"] = pd.to_numeric(df["LON"], errors="coerce")
    return df.dropna(subset=["LAT", "LON"])


def _load_network(cache_dir: str, network: str) -> list[dict]:
    raw = _cached_fetch(cache_dir, f"net_{network}.geojson",
                        lambda: _http_get(NETWORK_URL.format(network=network)),
                        permanent=False, max_age=INDEX_MAX_AGE)
    try:
        return json.loads(raw).get("features", [])
    except json.JSONDecodeError as exc:
        raise WeatherDataUnavailable(f"Station list for {network} was unreadable.") from exc


def _ghcnd_id(isd: pd.DataFrame, sid: str, lat: float, lon: float) -> str | None:
    """GHCN-Daily ID ('USW000' + WBAN) for an ASOS station, when resolvable."""
    cands = isd[isd["ICAO"].isin({sid, f"K{sid}", f"P{sid}"})
                & (isd["WBAN"] != "99999")].copy()
    if cands.empty:
        return None
    cands["d"] = [haversine_km(lat, lon, a, b) for a, b in zip(cands["LAT"], cands["LON"])]
    cands = cands[cands["d"] <= 5.0].sort_values("END", ascending=False)
    return f"USW000{cands.iloc[0]['WBAN']}" if not cands.empty else None


def nearest_stations(lat: float, lon: float, cache_dir: str, n: int = 3) -> list[dict]:
    """The ``n`` nearest ASOS stations, plus the nearest with 1-minute data.

    Stations reporting only hourly cannot verify a 15-minute block, so the
    nearest station carrying the 1-minute archive is always offered even when it
    is not among the ``n`` closest. Returns dicts with station_id, name, lat,
    lon, distance_km, tz, network, has_1min and ghcnd_id (None when no daily
    record can be linked).
    """
    isd = _load_isd_history(cache_dir)
    d = [haversine_km(lat, lon, a, b) for a, b in zip(isd["LAT"], isd["LON"])]
    near = isd.assign(d=d).nsmallest(40, "d")
    states = [s for s in dict.fromkeys(near["STATE"]) if re.fullmatch(r"[A-Z]{2}", s or "")]
    if not states:
        raise WeatherDataUnavailable("No US weather stations were found near this location.")

    found: dict[str, dict] = {}
    for state in states:
        for feat in _load_network(cache_dir, f"{state}_ASOS"):
            p = feat.get("properties") or {}
            coords = (feat.get("geometry") or {}).get("coordinates") or [None, None]
            if str((p.get("attributes") or {}).get("IS_AWOS", "0")) == "1":
                continue
            if coords[0] is None or not p.get("sid") or not p.get("tzname"):
                continue
            slat, slon = float(coords[1]), float(coords[0])
            attrs = p.get("attributes") or {}
            found[p["sid"]] = {
                "has_1min": str(attrs.get("HAS1MIN", "0")) == "1",
                "ghcnh_id": attrs.get("GHCNH_ID"),
                "station_id": p["sid"],
                "name": p.get("sname") or p["sid"],
                "lat": slat, "lon": slon,
                "distance_km": round(haversine_km(lat, lon, slat, slon), 1),
                "tz": p["tzname"],
                "network": p.get("network") or f"{state}_ASOS",
                "archive_begin": p.get("archive_begin"),
                "archive_end": p.get("archive_end"),
            }
    by_distance = sorted(found.values(), key=lambda s: s["distance_km"])
    if not by_distance:
        raise WeatherDataUnavailable("No ASOS weather station was found near this location.")
    ranked = by_distance[:n]
    if not any(s["has_1min"] for s in ranked):
        nearest_1min = next((s for s in by_distance if s["has_1min"]), None)
        if nearest_1min is not None:
            ranked.append(nearest_1min)
    for s in ranked:
        # The network metadata names the NOAA station directly; the ISD history
        # is the fallback where it does not.
        s["ghcnd_id"] = s.pop("ghcnh_id", None) or _ghcnd_id(isd, s["station_id"], s["lat"], s["lon"])
    return ranked


def _load_ghcn_snwd_index(cache_dir: str) -> pd.DataFrame:
    """GHCN-Daily stations reporting snow depth, with coordinates and year range."""
    raw = _cached_fetch(cache_dir, "ghcnd-inventory-snwd.csv",
                        lambda: _filter_snwd(_http_get(GHCN_INVENTORY_URL)),
                        permanent=False, max_age=INDEX_MAX_AGE)
    return pd.read_csv(io.BytesIO(raw))


def _filter_snwd(raw: bytes) -> bytes:
    """Reduce the 34 MB inventory to its snow-depth rows before caching."""
    rows = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        if line[31:35] == "SNWD":
            rows.append((line[0:11].strip(), line[12:20].strip(), line[21:30].strip(),
                         line[36:40].strip(), line[41:45].strip()))
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "lat", "lon", "first_year", "last_year"])
    w.writerows(rows)
    return buf.getvalue().encode()


def nearest_snow_stations(lat: float, lon: float, year: int, cache_dir: str,
                          n: int = 5, max_km: float = 100.0) -> list[dict]:
    """GHCN-Daily stations near (lat, lon) reporting snow depth in ``year``.

    Snow depth is not an ASOS measurement, so the airport's own record often has
    none; the surrounding cooperative-observer network usually does.
    """
    inv = _load_ghcn_snwd_index(cache_dir)
    inv = inv[(pd.to_numeric(inv["last_year"], errors="coerce") >= year)
              & (pd.to_numeric(inv["first_year"], errors="coerce") <= year)]
    if inv.empty:
        return []
    # Cheap bounding box first: 1 degree of latitude is ~111 km.
    deg = max_km / 111.0
    box = inv[(inv["lat"].between(lat - deg, lat + deg))
              & (inv["lon"].between(lon - deg / max(0.01, math.cos(math.radians(lat))),
                                    lon + deg / max(0.01, math.cos(math.radians(lat)))))].copy()
    if box.empty:
        return []
    box["distance_km"] = [haversine_km(lat, lon, a, b) for a, b in zip(box["lat"], box["lon"])]
    box = box[box["distance_km"] <= max_km].nsmallest(n, "distance_km")
    return [{"ghcnd_id": r.id, "distance_km": round(r.distance_km, 1)} for r in box.itertuples()]


# ── Observation fetchers ─────────────────────────────────────────────────────

def _read_iem_csv(raw: bytes, required: set[str], what: str) -> pd.DataFrame:
    text = raw.decode("utf-8", errors="replace")
    if not text.strip():
        return pd.DataFrame(columns=sorted(required))
    df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
    missing = required - set(df.columns)
    if missing:
        snippet = text.strip().splitlines()[0][:200]
        raise WeatherDataUnavailable(f"{what}: unexpected response from the data service ({snippet}).")
    return df


def _to_float(s: pd.Series) -> pd.Series:
    return pd.to_numeric(s.str.strip().replace({"M": None, "": None}), errors="coerce")


def fetch_onemin(station: str, start_utc: datetime, end_utc: datetime, cache_dir: str) -> pd.DataFrame:
    """ASOS 1-minute observations for [start_utc, end_utc), naive-UTC timestamps.

    Columns: valid_utc, tmpf (air temperature, F), sknt (2-min average wind, kt),
    gust_sknt (peak 5-s, kt), ptype (precipitation identifier, '' when missing),
    precip (in, NaN missing).
    """
    frames = []
    for month, nxt in _months(start_utc, end_utc):
        name = f"1min_v2_{station}_{month:%Y%m}.csv"
        params = {"station": station, "sts": f"{month:%Y-%m-%dT%H:%MZ}",
                  "ets": f"{nxt:%Y-%m-%dT%H:%MZ}",
                  "tz": "UTC", "sample": "1min", "what": "download", "delim": "comma",
                  "vars": "tmpf,sknt,gust_sknt,ptype,precip"}
        raw = _cached_fetch(cache_dir, name, lambda p=params: _http_get(ONEMIN_URL, p),
                            permanent=_settled(nxt), max_age=UNSETTLED_MAX_AGE)
        df = _read_iem_csv(raw, {"valid(UTC)", "tmpf", "sknt", "gust_sknt", "ptype", "precip"},
                           f"ASOS 1-minute data for {station}")
        frames.append(df)
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        return pd.DataFrame(columns=["valid_utc", "tmpf", "sknt", "gust_sknt", "ptype", "precip"])
    out = pd.DataFrame({
        "valid_utc": pd.to_datetime(df["valid(UTC)"], errors="coerce"),
        "tmpf": _to_float(df["tmpf"]),
        "sknt": _to_float(df["sknt"]),
        "gust_sknt": _to_float(df["gust_sknt"]),
        "ptype": df["ptype"].str.strip().replace({"M": "", "--": ""}),
        "precip": _to_float(df["precip"]),
    }).dropna(subset=["valid_utc"])
    out = out[(out["valid_utc"] >= start_utc) & (out["valid_utc"] < end_utc)]
    return out.drop_duplicates("valid_utc", keep="last").sort_values("valid_utc").reset_index(drop=True)


def fetch_metar(station: str, start_utc: datetime, end_utc: datetime, cache_dir: str) -> pd.DataFrame:
    """ASOS METAR reports (5-minute HFMETAR, routine, special), naive UTC.

    Columns: valid_utc, tmpf (air temperature, F), sknt (kt), gust (kt), wxcodes
    (present weather, '' = none reported), metar (raw report text, for remarks
    such as PWINO).
    """
    frames = []
    for month, nxt in _months(start_utc, end_utc):
        name = f"metar_v2_{station}_{month:%Y%m}.csv"
        params = [("station", station), ("sts", f"{month:%Y-%m-%dT%H:%MZ}"),
                  ("ets", f"{nxt:%Y-%m-%dT%H:%MZ}"), ("tz", "UTC"),
                  ("data", "tmpf"), ("data", "sknt"), ("data", "gust"), ("data", "wxcodes"), ("data", "metar"),
                  ("report_type", "1"), ("report_type", "3"), ("report_type", "4"),
                  ("format", "onlycomma"), ("missing", "empty")]
        raw = _cached_fetch(cache_dir, name, lambda p=params: _http_get(METAR_URL, p),
                            permanent=_settled(nxt), max_age=UNSETTLED_MAX_AGE)
        frames.append(_read_iem_csv(raw, {"valid", "tmpf", "sknt", "gust", "wxcodes", "metar"},
                                    f"METAR reports for {station}"))
    df = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    if df.empty:
        return pd.DataFrame(columns=["valid_utc", "tmpf", "sknt", "gust", "wxcodes", "metar"])
    out = pd.DataFrame({
        "valid_utc": pd.to_datetime(df["valid"], errors="coerce"),
        "tmpf": _to_float(df["tmpf"]),
        "sknt": _to_float(df["sknt"]),
        "gust": _to_float(df["gust"]),
        "wxcodes": df["wxcodes"].str.strip(),
        "metar": df["metar"].str.strip(),
    }).dropna(subset=["valid_utc"])
    out = out[(out["valid_utc"] >= start_utc) & (out["valid_utc"] < end_utc)]
    return out.sort_values("valid_utc").reset_index(drop=True)


def fetch_ghcn_daily(ghcnd_id: str, start_date, end_date, cache_dir: str) -> pd.DataFrame:
    """GHCN-Daily snow depth and snowfall, in mm (from inches), for [start_date, end_date].

    Columns: date (datetime64, local station day), snwd_mm, snow_mm (NaN missing).
    """
    frames = []
    for year in range(start_date.year, end_date.year + 1):
        y0 = max(pd.Timestamp(start_date), pd.Timestamp(year, 1, 1))
        y1 = min(pd.Timestamp(end_date), pd.Timestamp(year, 12, 31))
        params = {"dataset": "daily-summaries", "stations": ghcnd_id,
                  "dataTypes": "SNWD,SNOW", "startDate": f"{pd.Timestamp(year, 1, 1):%Y-%m-%d}",
                  "endDate": f"{pd.Timestamp(year, 12, 31):%Y-%m-%d}",
                  "format": "csv", "units": "standard", "includeAttributes": "true"}
        raw = _cached_fetch(cache_dir, f"ghcn_{ghcnd_id}_{year}_in_qc.csv",
                            lambda p=params: _http_get(NCEI_DAILY_URL, p),
                            permanent=_settled(datetime(year, 12, 31)), max_age=UNSETTLED_MAX_AGE)
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            continue
        df = pd.read_csv(io.StringIO(text), dtype=str, keep_default_na=False)
        if "DATE" not in df.columns:
            raise WeatherDataUnavailable(f"GHCN-Daily snow record for {ghcnd_id}: unexpected response.")
        df = df[(pd.to_datetime(df["DATE"]) >= y0) & (pd.to_datetime(df["DATE"]) <= y1)]
        frames.append(df)
    if not frames:
        return pd.DataFrame(columns=["date", "snwd_mm", "snow_mm"])
    df = pd.concat(frames, ignore_index=True)

    def mm(c: str) -> pd.Series:
        # Requested in inches, as observed: the metric service rounds to whole
        # millimetres, which turns a 1-inch depth into 25 mm, below 25.4 mm.
        if c not in df.columns:
            return pd.Series(float("nan"), index=df.index)
        values = pd.to_numeric(df[c], errors="coerce")
        # GHCN attributes: measurement flag, quality flag, source, observation time.
        # A nonblank quality flag means the value failed a quality check.
        attrs = df.get(c + "_ATTRIBUTES", pd.Series("", index=df.index)).str.split(",")
        quality = attrs.str[1].fillna("missing")
        values = values.where((values >= 0) & (quality == ""))
        # Trace snowfall is not zero: it cannot establish a snow-free missing day.
        if c == "SNOW":
            values = values.mask(attrs.str[0] == "T")
        return values * 25.4

    return pd.DataFrame({"date": pd.to_datetime(df["DATE"]),
                         "snwd_mm": mm("SNWD"), "snow_mm": mm("SNOW")}).sort_values("date").reset_index(drop=True)
