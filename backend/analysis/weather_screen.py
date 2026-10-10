# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""Meteorological screening of environmental noise records."""
from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd

KNOT_MS = 1852.0 / 3600.0

# Reason codes, in the priority order used when several apply.
REASONS = (
    "precipitation",
    "thunder",
    "wind",
    "snow_cover",
    "precip_buffer",
    "snow_cover_unverified",
    "weather_unverified",
    "clock_ambiguous",
)
REASON_LABELS = {
    "precipitation": "Precipitation (rain, drizzle, snow or other)",
    "thunder": "Thunder reported",
    "wind": "Station wind or gust / estimated microphone wind above limit",
    "snow_cover": "Snow on the ground",
    "precip_buffer": "Buffer before/after precipitation",
    "snow_cover_unverified": "Snow cover could not be verified",
    "weather_unverified": "Weather data incomplete for this period",
    "clock_ambiguous": "Timestamp missing, invalid or ambiguous at daylight saving transition",
}

CLOCK_MODES = ("local_dst", "local_standard", "utc")

# Temperature threshold for a diagnostic suspicion flag, not a dry-weather test.
IMPOSSIBLE_SNOW_TEMP_C = 5.0
IDENTIFIER_DISAGREEMENT_LIMIT = 0.20
IDENTIFIER_MIN_MINUTES = 200

_METAR_PRECIP = {"DZ", "RA", "SN", "SG", "PL", "GS", "GR", "IC", "UP"}
_METAR_FROZEN = {"SN", "SG", "PL", "GS", "GR", "IC"}


@dataclass(frozen=True)
class ScreenConfig:
    """Screening rules."""
    block_minutes: int = 15
    wind_limit_ms: float = 5.0
    mic_height_m: float = 1.5
    anemometer_height_m: float = 10.0
    roughness_length_m: float = 0.05
    buffer_before_blocks: int = 1
    buffer_after_blocks: int = 1
    snow_depth_limit_mm: float = 25.4
    slot_minutes: int = 5
    screen_station_wind: bool = False

    def validate(self):
        if any(type(v) is not int for v in (self.block_minutes, self.slot_minutes,
                                               self.buffer_before_blocks, self.buffer_after_blocks)):
            raise ValueError("Block, slot and buffer settings must be integers.")
        if self.slot_minutes <= 0:
            raise ValueError("Coverage slot must be positive.")
        if not (0 < self.roughness_length_m < float("inf")):
            raise ValueError("Roughness length must be finite and positive.")
        if self.block_minutes not in (5, 10, 15):
            raise ValueError("Screening block must be 5, 10 or 15 minutes.")
        if self.block_minutes % self.slot_minutes:
            raise ValueError("Screening block must be a whole number of coverage slots.")
        if not (0 < self.wind_limit_ms <= 30):
            raise ValueError("Wind limit must be between 0 and 30 m/s.")
        if not (self.roughness_length_m < self.mic_height_m <= 30):
            raise ValueError("Microphone height must be above the roughness length and at most 30 m.")
        if not (self.roughness_length_m < self.anemometer_height_m <= 100):
            raise ValueError("Anemometer height must be above the roughness length.")
        if not (0 <= self.buffer_before_blocks <= 8 and 0 <= self.buffer_after_blocks <= 8):
            raise ValueError("Buffers must be 0 to 8 blocks.")
        if not (0 < self.snow_depth_limit_mm <= 1000):
            raise ValueError("Snow depth limit must be positive.")
        return self

    def to_dict(self):
        return asdict(self)


def wind_height_factor(mic_height_m, anemometer_height_m,
                       roughness_length_m):
    """Log-profile ratio v(mic) / v(anemometer), dimensionless."""
    return (math.log(mic_height_m / roughness_length_m)
            / math.log(anemometer_height_m / roughness_length_m))


# Per-minute states

def _classify_ptype(code):
    code = (code or "").strip()
    if code in ("", "M", "--", "///"):
        return np.nan, None
    if code == "NP":
        return 0.0, None
    if code[0] == "R":
        return 1.0, "rain"
    if code[0] == "S":
        return 1.0, "snow"
    return 1.0, "other"


def parse_metar_weather(wxcodes, metar=""):
    """Precipitation / thunder state of one METAR report."""
    rain = snow = other = thunder = frozen = False
    for group in (wxcodes or "").split():
        g = group.lstrip("+-")
        vicinity = g.startswith("VC")
        if vicinity:
            g = g[2:]
        tokens = {g[i:i + 2] for i in range(0, len(g) - 1, 2)}
        if "TS" in tokens:
            thunder = True
        if vicinity:
            continue            # precipitation in the vicinity is not at the station
        precip = tokens & _METAR_PRECIP
        if not precip:
            continue
        if tokens & _METAR_FROZEN:
            snow = frozen = True
        if "RA" in precip or "DZ" in precip:
            rain = True
        if precip - {"RA", "DZ"} - _METAR_FROZEN:
            other = True       # UP: unknown precipitation
    any_precip = rain or snow or other
    unknown = re.search(r"\bPWINO\b", metar or "") is not None
    state = 1.0 if any_precip else (np.nan if unknown else 0.0)
    return {"state": state, "rain": rain, "snow": snow, "other": other,
            "thunder": thunder, "frozen": frozen}


def _f_to_c(f):
    return (f - 32.0) * 5.0 / 9.0


def _drop_impossible_snow(state, kind, tmpf):
    warm = pd.Series(tmpf).map(lambda v: pd.notna(v) and _f_to_c(v) > IMPOSSIBLE_SNOW_TEMP_C).to_numpy()
    bad = warm & (pd.Series(kind).to_numpy() == "snow")
    state = pd.Series(state).mask(pd.Series(bad), np.nan)
    kind = pd.Series(kind).mask(pd.Series(bad), None)
    return state.to_numpy(), kind.to_numpy(), int(bad.sum())


def assess_identifier(onemin, metar):
    """Is the 1-minute precipitation identifier consistent with the station's METARs?"""
    out = {"reliable": True, "minutes_compared": 0, "only_1min_pct": None,
           "agree_pct": None, "impossible_snow_minutes": 0}
    if onemin.empty or metar.empty:
        return out
    o = onemin.assign(minute=onemin["valid_utc"].dt.floor("min")).drop_duplicates("minute", keep="last").set_index("minute")
    m = metar.assign(minute=metar["valid_utc"].dt.floor("min")).drop_duplicates("minute", keep="last").set_index("minute")
    idx = o.index.intersection(m.index)
    if len(idx) == 0:
        return out
    cls = [_classify_ptype(p) for p in o.loc[idx, "ptype"]]
    s1, kind, n_bad = _drop_impossible_snow([c[0] for c in cls], [c[1] for c in cls],
                                            o.loc[idx, "tmpf"] if "tmpf" in o else [np.nan] * len(idx))
    s1 = pd.Series(s1, index=idx)
    s1[o.loc[idx, "precip"].fillna(0).to_numpy() > 0] = 1.0
    s2 = pd.Series([parse_metar_weather(a, b)["state"]
                    for a, b in zip(m.loc[idx, "wxcodes"], m.loc[idx, "metar"])], index=idx)
    both = s1.notna() & s2.notna()
    out["impossible_snow_minutes"] = n_bad
    if int(both.sum()) < IDENTIFIER_MIN_MINUTES:
        out["minutes_compared"] = int(both.sum())
        return out
    only_1min = float(((s1 == 1) & (s2 == 0))[both].mean())
    out.update(minutes_compared=int(both.sum()),
               only_1min_pct=round(100 * only_1min, 1),
               agree_pct=round(100 * float((s1[both] == s2[both]).mean()), 1),
               reliable=only_1min <= IDENTIFIER_DISAGREEMENT_LIMIT)
    return out


def minute_states(onemin, metar,
                  trust_identifier=True):
    """Merge both sources into one row per observed UTC minute."""
    parts = []
    if not onemin.empty:
        cls = [_classify_ptype(p) for p in onemin["ptype"]]
        state = [c[0] for c in cls]
        kind = [c[1] for c in cls]
        # Disagreement is diagnostic, not proof that precipitation did not occur.
        o = pd.DataFrame({
            "minute": onemin["valid_utc"].dt.floor("min"),
            "state": state,
            "kind": kind,
            "wind_kt": onemin["sknt"].to_numpy(dtype=float),
        })
        # A tipping-bucket amount is evidence of precipitation whatever the identifier says.
        tip = onemin["precip"].fillna(0).to_numpy() > 0
        o.loc[tip, "state"] = 1.0
        o.loc[tip & o["kind"].isna().to_numpy(), "kind"] = "other"
        o["rain"] = o["kind"] == "rain"
        o["snow"] = o["kind"] == "snow"
        o["other"] = o["kind"] == "other"
        o["thunder"] = False
        o["wind_peak_kt"] = onemin.reindex(columns=["sknt", "gust_sknt"]).where(lambda x: x >= 0).max(axis=1).to_numpy()
        o["src"] = "1min"
        parts.append(o.drop(columns="kind"))
    if not metar.empty:
        w = [parse_metar_weather(a, b) for a, b in zip(metar["wxcodes"], metar["metar"])]
        m_state = [x["state"] for x in w]
        keep_flag = lambda key: [bool(x[key]) for x in w]
        m = pd.DataFrame({
            "minute": metar["valid_utc"].dt.floor("min"),
            "state": m_state,
            "rain": keep_flag("rain"),
            "snow": keep_flag("snow"),
            "other": keep_flag("other"),
            "thunder": [x["thunder"] for x in w],
            "wind_kt": metar["sknt"].to_numpy(dtype=float),
        })
        m["wind_peak_kt"] = metar.reindex(columns=["sknt", "gust"]).where(lambda x: x >= 0).max(axis=1).to_numpy()
        m["src"] = "metar"
        parts.append(m)
    if not parts:
        return pd.DataFrame(columns=["state", "rain", "snow", "other", "thunder",
                                     "wind_kt", "src_1min", "src_metar"])
    allp = pd.concat(parts, ignore_index=True)
    allp["src_1min"] = allp["src"] == "1min"
    allp["src_metar"] = allp["src"] == "metar"
    g = allp.groupby("minute")
    out = pd.DataFrame({
        "state": g["state"].max(),                      # NaN only if every source is unknown
        "rain": g["rain"].any(), "snow": g["snow"].any(),
        "other": g["other"].any(), "thunder": g["thunder"].any(),
        "src_1min": g["src_1min"].any(), "src_metar": g["src_metar"].any(),
    })
    allp["wind_kt"] = allp["wind_kt"].where(lambda x: np.isfinite(x) & (x >= 0))
    allp["wind_peak_kt"] = allp["wind_peak_kt"].where(lambda x: np.isfinite(x) & (x >= 0))
    out["wind_kt"] = g["wind_kt"].max()
    out["wind_peak_kt"] = g["wind_peak_kt"].max()
    return out.sort_index()


# Clock mapping

def standard_utc_offset(tz):
    """The zone's standard-time UTC offset (the smaller of its Jan/Jul offsets)."""
    offs = [pd.Timestamp(f"2021-{m}-15 12:00").tz_localize(tz).utcoffset() for m in ("01", "07")]
    return min(offs)


def local_to_utc(ts, tz, clock):
    """Naive logger timestamps -> naive UTC. Unmappable times become NaT."""
    ts = pd.to_datetime(ts)
    if clock == "utc":
        return ts
    if clock == "local_standard":
        return ts - standard_utc_offset(tz)
    if clock == "local_dst":
        loc = ts.dt.tz_localize(tz, ambiguous="NaT", nonexistent="NaT")
        return loc.dt.tz_convert("UTC").dt.tz_localize(None)
    raise ValueError(f"Unknown clock mode '{clock}'.")


def utc_to_local(ts_utc, tz, clock):
    """Inverse of :func:`local_to_utc` (for labelling blocks in local time)."""
    if clock == "utc":
        return ts_utc
    if clock == "local_standard":
        return ts_utc + standard_utc_offset(tz)
    idx = pd.DatetimeIndex(ts_utc).tz_localize("UTC").tz_convert(tz).tz_localize(None)
    return idx


def check_clock_setting(ts, tz, clock):
    """Evidence in the record that contradicts the chosen clock setting."""
    if clock == "utc":
        return None
    valid = ts.dropna().sort_values()
    if valid.empty:
        return None
    hours = pd.Series(pd.date_range(valid.iloc[0].floor("h"), valid.iloc[-1].ceil("h"), freq="h"))
    # ambiguous=True resolves the repeated autumn hour, so only skipped hours are NaT.
    skipped = hours[hours.dt.tz_localize(tz, ambiguous=True, nonexistent="NaT").isna().to_numpy()]
    for gap_start in skipped:
        gap_end = gap_start + pd.Timedelta(hours=1)
        inside = int(((valid >= gap_start) & (valid < gap_end)).sum())
        before = valid[(valid >= gap_start - pd.Timedelta(minutes=1)) & (valid < gap_start)]
        after = valid[(valid >= gap_end) & (valid < gap_end + pd.Timedelta(minutes=1))]
        if clock == "local_dst" and inside:
            return (f"This file has {inside:,} readings stamped between {gap_start:%H:%M} and "
                    f"{gap_end:%H:%M} on {gap_start:%d %b %Y}, a time that did not exist in {tz} "
                    "because clocks moved forward. The logger clock does not follow daylight "
                    "saving: choose 'Local standard time'.")
        if clock == "local_standard" and not inside and len(before) and len(after):
            return (f"The readings jump from {before.iloc[-1]:%H:%M:%S} to {after.iloc[0]:%H:%M:%S} "
                    f"on {gap_start:%d %b %Y}, exactly when clocks moved forward in {tz}. The "
                    "logger clock follows daylight saving: choose 'Local time, with daylight saving'.")
    return None


# Blocks

def block_keys(ts_utc, block_minutes):
    """Integer block index (UTC epoch minutes // block) per timestamp; -1 for NaT."""
    idx = pd.DatetimeIndex(ts_utc).as_unit("ns")
    ns = idx.asi8.astype("int64")
    keys = np.floor_divide(ns, np.int64(block_minutes * 60 * 10**9))
    keys[idx.isna()] = -1
    return keys


def classify_blocks(minutes, start_utc, end_utc,
                    cfg, snow_status, tz, clock):
    """Classify every block overlapping [start_utc, end_utc]."""
    cfg.validate()
    b0 = int(block_keys(pd.DatetimeIndex([start_utc]), cfg.block_minutes)[0])
    b1 = int(block_keys(pd.DatetimeIndex([end_utc]), cfg.block_minutes)[0])
    keys = np.arange(b0, b1 + 1, dtype=np.int64)
    out = pd.DataFrame(index=pd.Index(keys, name="block"))
    out["block_start_utc"] = pd.to_datetime(keys * cfg.block_minutes * 60, unit="s")

    slots_per_block = cfg.block_minutes // cfg.slot_minutes
    if minutes.empty:
        mk = pd.DataFrame(columns=["block", "slot"])
    else:
        mk = minutes.copy()
        mk["block"] = block_keys(mk.index, cfg.block_minutes)
        mk["slot"] = block_keys(mk.index, cfg.slot_minutes)
        mk = mk[(mk["block"] >= b0) & (mk["block"] <= b1)]

    if mk.empty:
        for c in ("precip", "thunder", "rain", "snow", "other"):
            out[c] = False
        out["verified"] = False
        out["wind_mic_ms"] = np.nan
        out["wind_station_max_ms"] = np.nan
        out["n_minutes"] = out["n_1min"] = out["n_metar"] = 0
    else:
        mk["p_known"] = mk["state"].notna()
        mk["w_known"] = mk["wind_kt"].notna()
        slot = mk.groupby("slot").agg(block=("block", "first"),
                                      p_known=("p_known", "any"), w_known=("w_known", "any"))
        slot["ok"] = slot["p_known"] & slot["w_known"]
        covered = slot.groupby("block")["ok"].sum()
        g = mk.groupby("block")
        agg = pd.DataFrame({
            "precip": g["state"].max() == 1.0,
            "thunder": g["thunder"].any(),
            "rain": g["rain"].any(), "snow": g["snow"].any(), "other": g["other"].any(),
            "wind_kt": g["wind_kt"].mean(),
            "wind_station_max_ms": g["wind_peak_kt"].max() * KNOT_MS,
            "n_minutes": g.size(),
            "n_1min": g["src_1min"].sum(),
            "n_metar": g["src_metar"].sum(),
        })
        out = out.join(agg)
        out["verified"] = covered.reindex(out.index).fillna(0).to_numpy() >= slots_per_block
        for c in ("precip", "thunder", "rain", "snow", "other"):
            out[c] = out[c].fillna(False).astype(bool)
        for c in ("n_minutes", "n_1min", "n_metar"):
            out[c] = out[c].fillna(0).astype(int)
        factor = wind_height_factor(cfg.mic_height_m, cfg.anemometer_height_m, cfg.roughness_length_m)
        out["wind_mic_ms"] = out.pop("wind_kt") * KNOT_MS * factor

    # Snow observations use civil local dates even for a UTC/standard-time logger.
    local_dates = pd.DatetimeIndex(utc_to_local(out["block_start_utc"], tz, "local_dst")).normalize()
    out["local_date"] = local_dates
    snow = pd.Series(np.asarray([(snow_status or {}).get(d.date()) for d in local_dates], dtype=object),
                     index=out.index)

    wet = (out["precip"] | out["thunder"]).to_numpy()
    buffer = np.zeros(len(out), dtype=bool)
    for k in range(1, cfg.buffer_before_blocks + 1):
        buffer[:-k] |= wet[k:]          # block k steps before a wet block
    for k in range(1, cfg.buffer_after_blocks + 1):
        buffer[k:] |= wet[:-k]          # block k steps after a wet block

    unverified = ~out["verified"].to_numpy()
    near_unverified = np.zeros(len(out), dtype=bool)
    for k in range(1, cfg.buffer_before_blocks + 1):
        near_unverified[:-k] |= unverified[k:]
    for k in range(1, cfg.buffer_after_blocks + 1):
        near_unverified[k:] |= unverified[:-k]

    # NSW A4 applies the limit to mean wind at mic height; station max is optional
    windy = (out["wind_mic_ms"] > cfg.wind_limit_ms).fillna(False).to_numpy(copy=True)
    if cfg.screen_station_wind:
        windy |= (out["wind_station_max_ms"] > cfg.wind_limit_ms).fillna(False).to_numpy()
    conds = [
        out["precip"].to_numpy(),
        out["thunder"].to_numpy(),
        windy,
        (snow == "snow_cover").to_numpy(),
        buffer,
        (snow == "snow_cover_unverified").to_numpy(),
        unverified | near_unverified,
    ]
    reason = pd.Series(np.select(conds, list(REASONS[:7]), default=""), index=out.index, dtype=object)
    reason[reason == ""] = None
    out["reason"] = reason
    return out


# Snow cover

def snow_cover_status(daily, dates, cfg):
    """Snow-cover status for each local date that is not verifiably clear."""
    limit = cfg.snow_depth_limit_mm
    depth = {d.date(): v for d, v in zip(daily["date"], daily["snwd_mm"]) if pd.notna(v)}
    fall = {d.date(): v for d, v in zip(daily["date"], daily["snow_mm"]) if pd.notna(v)}
    known_days = sorted(depth)

    def status(day):            # 'cover' | 'clear' | 'unknown'
        if day in depth:
            return "cover" if depth[day] >= limit else "clear"
        prior = [d for d in known_days if d < day]
        if not prior or depth[prior[-1]] >= limit:
            return "unknown"
        d = prior[-1] + pd.Timedelta(days=1).to_pytimedelta()
        while d <= day:
            if fall.get(d) != 0:
                return "unknown"
            d += pd.Timedelta(days=1).to_pytimedelta()
        return "clear"

    out = {}
    one = pd.Timedelta(days=1).to_pytimedelta()
    for day in sorted(set(dates)):
        s = [status(day - one), status(day), status(day + one)]
        if "cover" in s:
            out[day] = "snow_cover"
        elif "unknown" in s:
            out[day] = "snow_cover_unverified"
    return out


# Applying a screen to samples

def screen_mask(ts_naive, excluded, *, tz, clock,
                block_minutes, range_blocks):
    """Keep-mask and exclusion reason for each sample."""
    utc = local_to_utc(ts_naive, tz, clock)
    keys = block_keys(utc, block_minutes)
    reason = pd.Series(keys).map(excluded).astype(object)
    reason[reason.isna()] = None
    unmapped = keys == -1
    outside = ~unmapped & ((keys < range_blocks[0]) | (keys > range_blocks[1]))
    reason[unmapped] = "clock_ambiguous"
    reason[outside] = "weather_unverified"
    reason.index = ts_naive.index
    keep = reason.isna().to_numpy()
    return keep, reason


def disclose_weather_screen(fig, df):
    """Keep exported figures explicit about the population they describe."""
    screen = df.attrs.get("weather_screen")
    if screen and fig is not None:
        # Mark observations, rather than drawing continuity through removed periods.
        for trace in fig.data:
            if trace.type in ("scatter", "scattergl") and trace.x is not None and len(trace.x):
                first = trace.x[0]
                if isinstance(first, (pd.Timestamp, np.datetime64)) or hasattr(first, "year"):
                    trace.mode = "markers"
                    trace.fill = "none"
                    trace.connectgaps = False
        removed = screen["rows_removed"]
        total = screen["rows_considered"]
        fig.add_annotation(x=0, y=-0.24, xref="paper", yref="paper", showarrow=False,
                           xanchor="left", align="left", font=dict(size=10),
                           text=f"Weather-screened subset: {total - removed:,}/{total:,} readings retained. "
                                "Remote station conditions; excluded periods are not reconstructed.")
        fig.update_layout(margin=dict(b=max(fig.layout.margin.b or 0, 110)))
    return fig
