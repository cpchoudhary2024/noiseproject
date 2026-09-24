"""Unit tests for meteorological screening (backend/analysis/weather_screen.py).

Expected values are worked by hand from the rules in the module docstring
(ISO 1996-2, NSW NPfI Fact Sheet A4, IOA GPG 2013), not read back from the
code. Network sources are replaced with fixed text, so no test touches the
internet.
"""
import math
import os
import sys
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend"))

from analysis.weather_screen import (  # noqa: E402
    KNOT_MS, ScreenConfig, _classify_ptype, block_keys, classify_blocks, local_to_utc,
    minute_states, parse_metar_weather, screen_mask, snow_cover_status, wind_height_factor,
)
import services.weather_sources as ws  # noqa: E402
from services.weather_screening import (  # noqa: E402
    WeatherScreenError, apply_record, fingerprint,
)

TZ = "America/New_York"


# ── Physical conversions ────────────────────────────────────────────────────

def test_wind_height_factor_log_profile():
    # ln(1.5/0.05) / ln(10/0.05) = ln 30 / ln 200 = 3.401197 / 5.298317
    assert wind_height_factor(1.5, 10.0, 0.05) == pytest.approx(0.641939, abs=1e-6)
    assert wind_height_factor(10.0, 10.0, 0.05) == pytest.approx(1.0)


def test_knot_conversion():
    assert KNOT_MS == pytest.approx(0.514444, abs=1e-6)


# ── Code parsing ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("code,state,kind", [
    ("NP", 0.0, None), ("R-", 1.0, "rain"), ("R+", 1.0, "rain"), ("S", 1.0, "snow"),
    ("P", 1.0, "other"), ("?0", 1.0, "other"), ("?3", 1.0, "other"),
])
def test_ptype_classification(code, state, kind):
    s, k = _classify_ptype(code)
    assert s == state and k == kind


def test_ptype_missing_is_unknown():
    s, k = _classify_ptype("")
    assert math.isnan(s) and k is None


@pytest.mark.parametrize("wx,exp", [
    ("-RA BR", dict(state=1.0, rain=True, snow=False, thunder=False)),
    ("+TSRA", dict(state=1.0, rain=True, thunder=True)),
    ("-FZRA", dict(state=1.0, rain=True)),
    ("-DZ", dict(state=1.0, rain=True)),
    ("SN", dict(state=1.0, snow=True, frozen=True)),
    ("BLSN", dict(state=1.0, snow=True)),
    ("UP", dict(state=1.0, other=True)),
    ("BR", dict(state=0.0, thunder=False)),
    ("VCSH", dict(state=0.0)),                  # vicinity: not at the station
    ("VCTS", dict(state=0.0, thunder=True)),    # thunder is audible from the vicinity
    ("", dict(state=0.0)),
])
def test_metar_weather(wx, exp):
    got = parse_metar_weather(wx, "KXXX 010000Z AUTO 00000KT 10SM CLR")
    for k, v in exp.items():
        assert got[k] == v, (wx, k)


def test_metar_pwino_makes_state_unknown():
    got = parse_metar_weather("", "KXXX 010000Z AUTO 00000KT 10SM CLR RMK AO2 PWINO")
    assert math.isnan(got["state"])
    # Reported precipitation is still evidence even when the identifier is out.
    assert parse_metar_weather("-RA", "... PWINO")["state"] == 1.0


# ── Clock mapping ────────────────────────────────────────────────────────────

def test_local_dst_to_utc():
    ts = pd.Series(pd.to_datetime([
        "2026-03-08 01:59:59",   # EST (UTC-5)
        "2026-03-08 02:30:00",   # does not exist: clocks jump 02:00 -> 03:00
        "2026-03-08 03:00:00",   # EDT (UTC-4)
        "2026-07-01 12:00:00",   # EDT
        "2026-11-01 01:30:00",   # occurs twice: ambiguous
    ]))
    utc = local_to_utc(ts, TZ, "local_dst")
    assert utc[0] == pd.Timestamp("2026-03-08 06:59:59")
    assert pd.isna(utc[1])
    assert utc[2] == pd.Timestamp("2026-03-08 07:00:00")
    assert utc[3] == pd.Timestamp("2026-07-01 16:00:00")
    assert pd.isna(utc[4])


def test_local_standard_and_utc_clocks():
    ts = pd.Series(pd.to_datetime(["2026-07-01 12:00:00"]))
    assert local_to_utc(ts, TZ, "local_standard")[0] == pd.Timestamp("2026-07-01 17:00:00")
    assert local_to_utc(ts, TZ, "utc")[0] == pd.Timestamp("2026-07-01 12:00:00")


# ── Block classification ─────────────────────────────────────────────────────

def _onemin(start, end, *, drop=(), ptype=None, wind=None):
    """1-minute frame, NP and 5 kt by default; overrides keyed by 'HH:MM'."""
    t = pd.date_range(start, end, freq="min", inclusive="left")
    df = pd.DataFrame({"valid_utc": t, "sknt": 5.0, "gust_sknt": 8.0, "ptype": "NP", "precip": 0.0})
    hhmm = df["valid_utc"].dt.strftime("%H:%M")
    for k, v in (ptype or {}).items():
        df.loc[hhmm == k, "ptype"] = v
    for k, v in (wind or {}).items():
        df.loc[hhmm == k, "sknt"] = v
    return df[~hhmm.isin(drop)].reset_index(drop=True)


EMPTY_METAR = pd.DataFrame(columns=["valid_utc", "sknt", "gust", "wxcodes", "metar"])


def _classify(onemin, metar=EMPTY_METAR, start="2026-07-01 00:00", end="2026-07-01 02:59:59", cfg=None):
    cfg = cfg or ScreenConfig()
    mins = minute_states(onemin, metar)
    return classify_blocks(mins, pd.Timestamp(start), pd.Timestamp(end), cfg, {}, TZ, "local_dst")


def _reasons(blocks):
    return [r or "clean" for r in blocks["reason"]]


def test_rain_block_and_buffers():
    b = _classify(_onemin("2026-07-01 00:00", "2026-07-01 03:00", ptype={"00:47": "R-"}))
    r = _reasons(b)
    # 15-min blocks: 00:00 00:15 00:30 00:45 01:00 ...
    assert r[3] == "precipitation"
    assert r[2] == "precip_buffer" and r[4] == "precip_buffer"
    assert r[0] == r[1] == r[5] == "clean"
    assert bool(b.iloc[3]["rain"])


def test_tipping_bucket_amount_counts_as_precipitation():
    om = _onemin("2026-07-01 00:00", "2026-07-01 03:00")
    om.loc[om["valid_utc"] == pd.Timestamp("2026-07-01 01:20"), "precip"] = 0.01
    assert _reasons(_classify(om))[5] == "precipitation"


def test_wind_limit_boundary():
    # Both exceed 5 m/s at station height, even if the height model reduces them.
    k15 = {f"01:{m:02d}": 15.0 for m in range(0, 15)}
    k16 = {f"01:{m:02d}": 16.0 for m in range(15, 30)}
    b = _classify(_onemin("2026-07-01 00:00", "2026-07-01 03:00", wind={**k15, **k16}))
    assert b.iloc[4]["wind_mic_ms"] == pytest.approx(15 * KNOT_MS * 0.641939, rel=1e-5)
    assert _reasons(b)[4] == "wind"
    assert _reasons(b)[5] == "wind"


def test_coverage_is_slot_based_and_gaps_spread_to_neighbours():
    # 00:05-00:08 missing but 00:09 present: that 5-min slot is still observed.
    # 01:50-01:54 missing: a whole slot of block 01:45 has no observation.
    drop = [f"00:0{m}" for m in range(5, 9)] + [f"01:5{m}" for m in range(0, 5)]
    r = _reasons(_classify(_onemin("2026-07-01 00:00", "2026-07-01 03:00", drop=drop)))
    assert r[0] == "clean"
    assert r[7] == "weather_unverified"                          # 01:45
    assert r[6] == "weather_unverified" and r[8] == "weather_unverified"   # neighbours
    assert r[5] == "clean" and r[9] == "clean"


def test_metar_fills_gap_and_adds_evidence():
    om = _onemin("2026-07-01 00:00", "2026-07-01 03:00",
                 drop=[f"{h:02d}:{m:02d}" for h in (2,) for m in range(30, 45)])
    metar = pd.DataFrame({
        "valid_utc": pd.to_datetime(["2026-07-01 02:30", "2026-07-01 02:35",
                                     "2026-07-01 02:40", "2026-07-01 02:50"]),
        "sknt": [5.0, 5.0, 5.0, 5.0], "gust": [np.nan] * 4,
        "wxcodes": ["", "", "", "-RA"], "metar": [""] * 4,
    })
    b = _classify(om, metar)
    r = _reasons(b)
    assert b.iloc[10]["verified"] and b.iloc[10]["n_1min"] == 0     # 02:30, METAR only
    assert r[11] == "precipitation"                                 # 02:45: 1-min NP, METAR -RA
    assert r[10] == "precip_buffer"


def test_unknown_ptype_minutes_do_not_verify():
    om = _onemin("2026-07-01 00:00", "2026-07-01 03:00",
                 ptype={f"00:{m:02d}": "" for m in range(15, 20)})   # identifier missing
    assert _reasons(_classify(om))[1] == "weather_unverified"


# ── Snow cover ───────────────────────────────────────────────────────────────

def test_snow_cover_status():
    d = pd.date_range("2026-01-10", periods=9, freq="D")
    daily = pd.DataFrame({"date": d,
                          "snwd_mm": [0, 0, 30, 0, 0, np.nan, np.nan, 0, 0],
                          "snow_mm": [0, 0, 30, 0, 0, 0, np.nan, 0, 0]})
    days = [x.date() for x in d[1:8]]
    st = snow_cover_status(daily, days, ScreenConfig())
    D = lambda i: d[i].date()
    assert st[D(1)] == "snow_cover" and st[D(2)] == "snow_cover" and st[D(3)] == "snow_cover"
    assert D(4) not in st                                   # 0, 0, deduced 0
    assert st[D(5)] == st[D(6)] == st[D(7)] == "snow_cover_unverified"


def test_missing_depth_after_snow_is_unverified():
    d = pd.date_range("2026-01-10", periods=4, freq="D")
    daily = pd.DataFrame({"date": d, "snwd_mm": [40, np.nan, np.nan, np.nan],
                          "snow_mm": [0, 0, 0, 0]})
    st = snow_cover_status(daily, [d[2].date()], ScreenConfig())
    assert st[d[2].date()] == "snow_cover_unverified"


# ── Applying to samples ──────────────────────────────────────────────────────

def test_screen_mask_reasons():
    ts = pd.Series(pd.to_datetime([
        "2026-07-01 08:00:00",    # 12:00 UTC  -> excluded block
        "2026-07-01 08:20:00",    # 12:20 UTC  -> clean
        "2026-11-01 01:30:00",    # ambiguous
        "2026-07-02 08:00:00",    # outside range
    ]))
    k_rain = int(block_keys(pd.DatetimeIndex(["2026-07-01 12:00"]), 15)[0])
    k_end = int(block_keys(pd.DatetimeIndex(["2026-07-01 23:45"]), 15)[0])
    keep, reason = screen_mask(ts, {k_rain: "precipitation"}, tz=TZ, clock="local_dst",
                               block_minutes=15, range_blocks=(k_rain - 10, k_end))
    assert keep.tolist() == [False, True, False, False]
    assert reason.tolist() == ["precipitation", None, "clock_ambiguous", "weather_unverified"]


def test_apply_record_refuses_other_file():
    ts = pd.Series(pd.to_datetime(["2026-07-01 08:00:00", "2026-07-01 08:00:01"]))
    rec = {"fingerprint": fingerprint(ts[:1]), "excluded": {}, "station": {"tz": TZ},
           "clock": "local_dst", "config": {"block_minutes": 15}, "range_blocks": [0, 1]}
    with pytest.raises(WeatherScreenError):
        apply_record(ts, rec)


# ── Source parsing (network replaced by fixed text) ─────────────────────────

def test_fetch_onemin_parses_missing_markers(monkeypatch, tmp_path):
    text = ("station,station_name,valid(UTC),tmpf,sknt,gust_sknt,ptype,precip\n"
            "BWI,Baltimore,2026-07-01 00:00,71,10,14,NP,0.0\n"
            "BWI,Baltimore,2026-07-01 00:01,M,M,M,M,M\n"
            "BWI,Baltimore,2026-07-01 00:02,70,9,12,R-,0.01\n"
            "BWI,Baltimore,2026-07-01 00:02,70,9,12,R-,0.01\n")
    monkeypatch.setattr(ws, "_http_get", lambda url, params=None: text.encode())
    df = ws.fetch_onemin("BWI", datetime(2026, 7, 1), datetime(2026, 7, 1, 1), str(tmp_path))
    assert len(df) == 3
    assert df["ptype"].tolist() == ["NP", "", "R-"]
    assert math.isnan(df["sknt"][1]) and df["precip"][2] == 0.01


def test_fetch_rejects_error_page(monkeypatch, tmp_path):
    monkeypatch.setattr(ws, "_http_get", lambda url, params=None: b"ERROR: service unavailable\n")
    with pytest.raises(ws.WeatherDataUnavailable):
        ws.fetch_onemin("BWI", datetime(2026, 7, 1), datetime(2026, 7, 1, 1), str(tmp_path))


def test_resolve_location_coordinates():
    lat, lon, basis = ws.resolve_location("39.2, -76.8", "/nonexistent")
    assert (lat, lon, basis) == (39.2, -76.8, "coordinates")
    with pytest.raises(ws.LocationError):
        ws.resolve_location("somewhere", "/nonexistent")
    with pytest.raises(ws.LocationError):
        ws.resolve_location("95, 10", "/nonexistent")


# ── Clock-setting contradiction check ────────────────────────────────────────

def _record_across_dst(follow_dst: bool) -> pd.Series:
    """1 Hz record over the 8 Mar 2026 spring-forward change (America/New_York)."""
    before = pd.date_range("2026-03-08 01:50:00", "2026-03-08 01:59:59", freq="s")
    if follow_dst:
        after = pd.date_range("2026-03-08 03:00:00", "2026-03-08 03:10:00", freq="s")
    else:
        after = pd.date_range("2026-03-08 02:00:00", "2026-03-08 02:10:00", freq="s")
    return pd.Series(before.append(after))


def test_clock_check_accepts_consistent_settings():
    from analysis.weather_screen import check_clock_setting
    assert check_clock_setting(_record_across_dst(True), TZ, "local_dst") is None
    assert check_clock_setting(_record_across_dst(False), TZ, "local_standard") is None
    assert check_clock_setting(_record_across_dst(False), TZ, "utc") is None


def test_clock_check_rejects_contradictions():
    from analysis.weather_screen import check_clock_setting
    msg = check_clock_setting(_record_across_dst(False), TZ, "local_dst")
    assert msg and "does not follow daylight saving" in msg
    msg = check_clock_setting(_record_across_dst(True), TZ, "local_standard")
    assert msg and "follows daylight saving" in msg


def test_clock_check_ignores_records_without_transition():
    from analysis.weather_screen import check_clock_setting
    ts = pd.Series(pd.date_range("2026-07-01", periods=100, freq="s"))
    assert check_clock_setting(ts, TZ, "local_dst") is None
    # Autumn fall-back hour is repeated, not skipped: never a contradiction.
    ts = pd.Series(pd.date_range("2026-11-01 00:30", "2026-11-01 02:30", freq="min"))
    assert check_clock_setting(ts, TZ, "local_standard") is None


# ── Station sensor faults ────────────────────────────────────────────────────

def _pair(n, *, ptype, tmpf, wxcodes=""):
    """Matching 1-minute and METAR frames for one station."""
    t = pd.date_range("2026-06-01", periods=n, freq="min")
    om = pd.DataFrame({"valid_utc": t, "tmpf": tmpf, "sknt": 6.0, "gust_sknt": 9.0,
                       "ptype": ptype, "precip": 0.0})
    mt = pd.DataFrame({"valid_utc": t, "tmpf": tmpf, "sknt": 6.0, "gust": np.nan,
                       "wxcodes": wxcodes, "metar": ""})
    return om, mt


def test_suspect_warm_snow_is_not_silently_cleared():
    """Temperature alone cannot justify accepting suspect precipitation as dry."""
    from analysis.weather_screen import assess_identifier
    om, mt = _pair(600, ptype="S-", tmpf=84.0)          # 29 C
    q = assess_identifier(om, mt)
    assert q["impossible_snow_minutes"] == 600
    mins = minute_states(om, mt, trust_identifier=q["reliable"])
    assert int((mins["state"] == 1).sum()) == 600
    assert mins["snow"].all()


def test_genuine_cold_snow_is_kept():
    from analysis.weather_screen import assess_identifier
    om, mt = _pair(600, ptype="S-", tmpf=26.0, wxcodes="SN")   # -3 C
    q = assess_identifier(om, mt)
    assert q["impossible_snow_minutes"] == 0 and q["reliable"]
    mins = minute_states(om, mt, trust_identifier=True)
    assert int((mins["state"] == 1).sum()) == 600 and mins["snow"].all()


def test_identifier_disagreement_preserves_precipitation_evidence():
    from analysis.weather_screen import assess_identifier
    # Rain claimed every minute at 20 C while the station's METARs report nothing.
    om, mt = _pair(600, ptype="R-", tmpf=68.0)
    q = assess_identifier(om, mt)
    assert q["reliable"] is False and q["only_1min_pct"] == 100.0
    mins = minute_states(om, mt, trust_identifier=q["reliable"])
    assert int((mins["state"] == 1).sum()) == 600    # disagreement cannot prove dry
    # ...but a measured rainfall amount is still evidence.
    om2 = om.copy(); om2.loc[5, "precip"] = 0.01
    mins2 = minute_states(om2, mt, trust_identifier=False)
    assert mins2.loc[mins2.index[5], "state"] == 1.0


def test_healthy_identifier_is_trusted():
    from analysis.weather_screen import assess_identifier
    om, mt = _pair(600, ptype="R-", tmpf=68.0, wxcodes="-RA")
    q = assess_identifier(om, mt)
    assert q["reliable"] and q["agree_pct"] == 100.0


def test_too_few_shared_minutes_to_judge():
    from analysis.weather_screen import assess_identifier
    om, mt = _pair(50, ptype="R-", tmpf=68.0)
    q = assess_identifier(om, mt)
    assert q["reliable"] and q["minutes_compared"] == 50 and q["only_1min_pct"] is None


def test_gust_and_short_wind_peak_cannot_average_away():
    om = _onemin("2026-07-01 00:00", "2026-07-01 03:00")
    om.loc[47, "gust_sknt"] = 25.0
    om.loc[77, "sknt"] = 20.0
    b = _classify(om)
    assert _reasons(b)[3] == _reasons(b)[5] == "wind"
    assert b.iloc[3]["wind_station_max_ms"] == pytest.approx(25 * KNOT_MS)


def test_timestamp_fingerprint_detects_interior_changes_and_order():
    ts = pd.Series(pd.date_range("2026-01-01", periods=4, freq="min"))
    changed = ts.copy()
    changed.iloc[1] += pd.Timedelta(seconds=1)
    assert fingerprint(ts) != fingerprint(changed)
    assert fingerprint(ts) != fingerprint(ts.iloc[[0, 2, 1, 3]])


@pytest.mark.parametrize("zone,offset", [("America/Anchorage", 9), ("Pacific/Honolulu", 10),
                                         ("America/Phoenix", 7), ("America/Los_Angeles", 8)])
def test_us_standard_time_zones(zone, offset):
    ts = pd.Series(pd.to_datetime(["2026-07-01 12:00"]))
    assert local_to_utc(ts, zone, "local_standard").iloc[0] == ts.iloc[0] + pd.Timedelta(hours=offset)


def test_utc_logger_uses_site_civil_date_for_snow():
    om = _onemin("2026-07-01 00:00", "2026-07-01 03:00")
    b = classify_blocks(minute_states(om, EMPTY_METAR), pd.Timestamp("2026-07-01"),
                        pd.Timestamp("2026-07-01 02:59"), ScreenConfig(),
                        {date(2026, 6, 30): "snow_cover"}, TZ, "utc")
    assert set(b["reason"]) == {"snow_cover"}


@pytest.mark.parametrize("settings", [{"slot_minutes": 0}, {"slot_minutes": -5},
                                      {"buffer_before_blocks": 1.5}, {"roughness_length_m": 0}])
def test_invalid_config_is_rejected(settings):
    with pytest.raises(ValueError):
        ScreenConfig(**settings).validate()


def test_ghcn_rejects_quality_flags_missing_and_trace(monkeypatch, tmp_path):
    raw = ('DATE,SNWD,SNWD_ATTRIBUTES,SNOW,SNOW_ATTRIBUTES\n'
           '2026-01-01,0,",X,0,0700",0,",,0,0700"\n'
           '2026-01-02,-9999,",,0,0700",0,"T,,0,0700"\n'
           '2026-01-03,1,",,0,0700",0,",,0,0700"\n').encode()
    monkeypatch.setattr(ws, "_http_get", lambda *args: raw)
    df = ws.fetch_ghcn_daily("TEST", date(2026, 1, 1), date(2026, 1, 3), str(tmp_path))
    assert df["snwd_mm"].isna().tolist() == [True, True, False]
    assert df.iloc[2]["snwd_mm"] == pytest.approx(25.4)
    assert pd.isna(df.iloc[1]["snow_mm"])


@pytest.mark.parametrize('depth_available', [True, False])
def test_build_screen_missing_times_and_warm_missing_snow(monkeypatch, tmp_path, depth_available):
    import services.weather_screening as service
    om = _onemin('2026-07-01 00:00', '2026-07-01 05:00')
    om['tmpf'] = 85.0
    monkeypatch.setattr(service, 'fetch_onemin', lambda *a: om)
    monkeypatch.setattr(service, 'fetch_metar', lambda *a: EMPTY_METAR)
    daily = pd.DataFrame({'date': pd.date_range('2026-06-28', periods=7),
                          'snwd_mm': 0.0, 'snow_mm': 0.0})
    if not depth_available:
        daily = daily.iloc[:0]
    monkeypatch.setattr(service, '_snow_record', lambda *a: (daily, {'mode': 'depth' if depth_available else 'none'}))
    ts = pd.Series([pd.Timestamp('2026-07-01 02:00'), pd.NaT])
    rec = service.build_screen(ts, {'station_id': 'TEST', 'name': 'Test', 'tz': TZ},
                              ScreenConfig(), 'utc', cache_dir=str(tmp_path), store_dir=str(tmp_path),
                              location_basis='test')
    summary = rec['summary']
    assert summary['samples_kept'] + summary['samples_excluded'] == 2
    assert sum(summary['samples_by_reason'].values()) == summary['samples_excluded']
    assert rec['usable'] is depth_available
    if depth_available:
        keep, _ = service.apply_record(ts, service.load_record(str(tmp_path), rec['screen_id']))
        assert keep.tolist() == [True, False]
    else:
        assert summary['samples_by_reason']['snow_cover_unverified'] == 1
        assert not (tmp_path / (rec['screen_id'] + '.json')).exists()


def test_screened_chart_caption_and_no_lines_across_exclusions():
    from analysis.chart_generator import AdvancedChartGenerator
    df = pd.DataFrame({'Time': pd.to_datetime(['2026-07-01 00:00', '2026-07-01 02:00']),
                       'LEQ dB -A': [40.0, 50.0]})
    df.attrs['weather_screen'] = {'rows_removed': 60, 'rows_considered': 62}
    fig = AdvancedChartGenerator(df).generate_time_series_heatmap()
    assert fig.data[0].mode == 'markers'
    assert any('2/62 readings retained' in a.text for a in fig.layout.annotations)


def test_weather_report_section_renders_pdf_and_word(tmp_path):
    from analysis.report_generator_v2 import ReportGeneratorV2
    from analysis.docx_from_story import render_story_to_docx
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate
    from docx import Document
    generator = ReportGeneratorV2.__new__(ReportGeneratorV2)
    generator.weather_screen = {
        'station': {'name': 'Test Airport', 'station_id': 'TEST', 'distance_km': 12.0, 'tz': TZ},
        'config': ScreenConfig().to_dict(), 'clock': 'utc', 'screen_id': 'test',
        'location_basis': 'coordinates', 'sources': 'Synthetic test observations',
        'wind_height_factor': 0.641939, 'rows_considered': 100, 'rows_removed': 25,
        'rows_removed_by_reason': {'wind': 25},
        'snow_source': {'mode': 'depth', 'ghcnd_id': 'TEST', 'distance_km': 12.0},
        'summary': {'blocks_with_1min_pct': 90.0, 'blocks_verified_pct': 95.0},
    }
    styles = getSampleStyleSheet()
    story = []
    generator._add_weather_screen_section(story, styles)
    word = tmp_path / 'weather.docx'
    render_story_to_docx(story, str(word), page_width_in=8.5, page_height_in=11,
                         margins_in=(0.75, 0.75, 0.75, 0.75))
    text = ' '.join(p.text for p in Document(word).paragraphs)
    assert '75 of 100' in text and 'not certified free of weather effects' in text
    pdf = tmp_path / 'weather.pdf'
    SimpleDocTemplate(str(pdf)).build(story)
    assert pdf.read_bytes().startswith(b'%PDF')
