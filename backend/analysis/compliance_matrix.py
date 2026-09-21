# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""
Compliance standards matrix — ground-truth sourced exclusively from:

  • WHO Environmental Noise Guidelines for the European Region (WHO, 2018)
    https://iris.who.int/bitstream/handle/10665/279952/9789289053563-eng.pdf

  • WHO Guidelines for Community Noise (Berglund et al., 1999)
    https://iris.who.int/handle/10665/66217

  • Maryland Code of Maryland Regulations (COMAR) 26.02.03
    .01 Definitions  ·  .02 Environmental Noise Standards (Table 1, goals)
    .03 General Regulations (Table 2, maximum allowable levels)

VERIFICATION (5 Aug 2026). Values below were checked against primary sources,
not against secondary summaries:
  • WHO 2018 road/rail/aircraft/wind-turbine Lden and Lnight — confirmed against
    the WHO Compendium of WHO and other UN guidance on health and environment,
    chapter 11 "Environmental noise", 2022 update (cdn.who.int).
  • Lden and Lnight period definitions — confirmed against Directive 2002/49/EC
    Annex I: day 07:00-19:00 (12 h), evening 19:00-23:00 (4 h, +5 dB), night
    23:00-07:00 (8 h, +10 dB), averaged over a YEAR. Member States may shorten
    the evening by 1-2 h; the defaults are used here.
  • Maryland COMAR 26.02.03 — confirmed against the regulation text.
    .01B(5)  "Daytime hours" = 7 a.m. to 10 p.m.
    .01B(15) "Nighttime hours" = 10 p.m. to 7 a.m.
    .01B(4)  Ldn = 24 h energy average, +10 dB applied to 10 p.m.-7 a.m.
    .02  Table 1 (GOALS): Industrial 70 dBA Leq(24), Commercial 64 dBA Ldn,
         Residential 55 dBA Ldn.
    .03  Table 2 (ENFORCEABLE, "a person may not cause or permit noise levels
         which exceed those specified in Table 2"):
         Day 75/67/65, Night 75/62/55 dBA for industrial/commercial/residential.
         Table 2 states NO averaging period — see MD_TABLE2_METRIC_NOTE.

IMPORTANT: OSHA/NIOSH occupational standards are deliberately excluded from
the environmental compliance matrix. They belong in an occupational assessment
context only (datasets from industrial worksites with shift-worker exposure).


╔══════════════════════════════════════════════════════════════════════════════╗
║ KNOWN CONFLICT — read before changing any limit or writing anything about    ║
║ limits, in this repo or in a report.                                         ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ The study's own participant handout, "Noise Limits Guidelines pdf.pdf"        ║
║ (Understanding Noise: A Resident's Complete Guide), carries a "Safe Limits    ║
║ Summary" that does NOT come from WHO, although it is captioned as derived     ║
║ from the WHO 2018 guidelines:                                                 ║
║                                                                               ║
║     Daytime   07:00-19:00   outside the home   below 55 dB                    ║
║     Evening   19:00-23:00   outside the home   below 50 dB                    ║
║     Night     23:00-07:00   outside the home   below 45 dB                    ║
║                                                                               ║
║ WHO 2018 publishes no such per-period outdoor thresholds. It publishes Lden   ║
║ 53 dB and Lnight 45 dB for road traffic, and Lden is a single penalty-        ║
║ weighted annual figure that cannot be decomposed into separate day and        ║
║ evening limits. The 55 and 50 dB values appear to be back-calculated from     ║
║ Lden 53 by removing the +5/+10 dB penalties. That is an inference, not a      ║
║ WHO recommendation, and it is not labelled as one in the handout.             ║
║                                                                               ║
║ Only the night value (45 dB) coincides with a real WHO figure, and even that  ║
║ agreement is incidental — WHO's 45 dB is an Lnight annual average, not a      ║
║ "stay below this" outdoor threshold.                                          ║
║                                                                               ║
║ STATUS: the platform and its reports follow WHO as published (Lden 53 /       ║
║ Lnight 45) and do NOT reproduce the handout's table. The handout is a         ║
║ compiled PDF and has not been corrected. Anyone comparing a report against    ║
║ the handout will find a discrepancy on the day and evening rows; it is the    ║
║ handout that needs revising, not the report.                                  ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""
from __future__ import annotations

import math
from dataclasses import dataclass


# ── WHO 2018 Environmental Noise Guideline Levels ────────────────────────────
# Source: Table 1–3 in WHO 2018 Guidelines, p. 85–87
WHO_ROAD_LDEN    = 53.0   # dB(A) — road traffic, 24 h Lden
WHO_ROAD_LNIGHT  = 45.0   # dB(A) — road traffic, Lnight (23:00–07:00)
WHO_RAIL_LDEN    = 54.0   # dB(A) — railway
WHO_RAIL_LNIGHT  = 44.0   # dB(A) — railway nighttime
WHO_AIR_LDEN     = 45.0   # dB(A) — aircraft (stricter: unpredictable bursts)
WHO_AIR_LNIGHT   = 40.0   # dB(A) — aircraft nighttime
WHO_LOAEL_LNIGHT = 40.0   # dB(A) — LOAEL for nighttime sleep effects

# ── WHO Indoor Limits (WHO 1999, reaffirmed in WHO 2018) ─────────────────────
WHO_BEDROOM_LAEQ  = 30.0  # dB(A) — bedroom average (protect sleep quality)
WHO_BEDROOM_LAMAX = 45.0  # dB(A) — bedroom, single event (prevent awakening)
WHO_LIVING_LAEQ   = 35.0  # dB(A) — living room
WHO_CLASS_LAEQ    = 35.0  # dB(A) — classroom

# ── Maryland COMAR 26.02.03.03 Table 2 — maximum allowable levels ─────────────
# Daytime = 07:00–22:00  /  Nighttime = 22:00–07:00  (COMAR .01B(5) and .01B(15))
MD_RESIDENTIAL_DAY    = 65.0   # dB(A)
MD_RESIDENTIAL_NIGHT  = 55.0   # dB(A)
MD_COMMERCIAL_DAY     = 67.0   # dB(A)
MD_COMMERCIAL_NIGHT   = 62.0   # dB(A)
MD_INDUSTRIAL_DAY     = 75.0   # dB(A)
MD_INDUSTRIAL_NIGHT   = 75.0   # dB(A)

# ── Maryland COMAR 26.02.03.02 Table 1 — environmental noise standards ────────
# The state's GOALS, distinct from the enforceable Table 2 above and expressed
# on different metrics. The residential goal is an Ldn, which this platform
# computes directly, and COMAR's own Ldn definition (.01B(4): +10 dB applied to
# 10 p.m.–7 a.m.) matches the Ldn implemented in acoustics.py exactly.
MD_GOAL_RESIDENTIAL_LDN = 55.0   # dB(A) Ldn
MD_GOAL_COMMERCIAL_LDN  = 64.0   # dB(A) Ldn
MD_GOAL_INDUSTRIAL_LEQ24 = 70.0  # dB(A) Leq(24)

# COMAR Table 2 is headed "Maximum Allowable Noise Levels (dBA)" and states no
# averaging period. The regulation defines "equivalent sound level" in .01B(13)
# and says in .02A(2) that the STANDARDS (Table 1) are expressed in equivalent
# levels, but it does not say the same of Table 2. Comparing a period LAeq
# against Table 2 is therefore a reading of the regulation, not a quotation of
# it, and every row derived from Table 2 says so rather than implying the
# regulation settled the question.
MD_TABLE2_METRIC_NOTE = (
    "COMAR Table 2 is stated in dB(A) with no averaging period given. This "
    "report compares it against the LAeq of the period, which is the most "
    "common reading; a not-to-exceed reading of the same table would be stricter."
)


@dataclass(frozen=True)
class StandardRow:
    """One row in the compliance matrix."""
    standard: str      # Regulatory body + source name
    metric: str        # Acoustic metric (Lden, Lnight, LAeq, LAmax …)
    limit_db: float    # Health/legal limit in dB(A)
    source: str        # Exact citation
    tooltip: str       # Plain-English definition pulled verbatim from the document
    category: str      # "who_env" | "who_indoor" | "maryland"
    # Source-specific guidelines (aircraft, railway) were derived from studies that
    # attributed noise exclusively to one source. A source-blind sound level meter
    # measures total combined energy and cannot attribute it, so these rows are
    # reported as *indicative reference* comparisons rather than pass/fail compliance.
    source_specific: bool = False
    # Indoor guidelines (bedroom) are only valid against an indoor-placed sensor.
    indoor_only: bool = False
    # Shown as a reference comparison, never PASS/FAIL, because a single measured
    # value cannot decide the guideline as written.
    indicative_only: bool = False


# Ordered from most health-relevant to regulatory
MATRIX: list[StandardRow] = [

    # ── WHO 2018 — Road Traffic ───────────────────────────────────────────────
    StandardRow(
        standard="WHO 2018 — Road Traffic",
        metric="Lden",
        limit_db=WHO_ROAD_LDEN,
        source="WHO Environmental Noise Guidelines (2018), Table 1",
        tooltip=(
            "Lden: 24-hour energy average with +5 dB added to evening (19:00–23:00) and +10 dB to "
            "night (23:00–07:00) readings (EU Directive 2002/49/EC, Annex I). "
            "WHO Environmental Noise Guidelines for the European Region (2018): "
            "road traffic 53 dB Lden, strong recommendation."
        ),
        category="who_env",
    ),
    StandardRow(
        standard="WHO 2018 — Road Traffic",
        metric="Lnight",
        limit_db=WHO_ROAD_LNIGHT,
        source="WHO Environmental Noise Guidelines (2018), Table 1",
        tooltip=(
            "Lnight: energy average over 23:00–07:00, with no penalty (EU Directive 2002/49/EC, Annex I). "
            "WHO Environmental Noise Guidelines for the European Region (2018): "
            "road traffic 45 dB Lnight, strong recommendation."
        ),
        category="who_env",
    ),

    # ── WHO 2018 — Aircraft ───────────────────────────────────────────────────
    StandardRow(
        standard="WHO 2018 — Aircraft Noise",
        metric="Lden",
        limit_db=WHO_AIR_LDEN,
        source="WHO Environmental Noise Guidelines (2018), Table 1",
        tooltip=(
            "Lden: 24-hour energy average with +5 dB added to evening (19:00–23:00) and +10 dB to "
            "night (23:00–07:00) readings (EU Directive 2002/49/EC, Annex I). "
            "WHO Environmental Noise Guidelines for the European Region (2018): "
            "aircraft 45 dB Lden, strong recommendation. "
            "Shown for reference only: a sound level meter cannot identify the noise source."
        ),
        category="who_env",
        source_specific=True,
    ),
    StandardRow(
        standard="WHO 2018 — Aircraft Noise",
        metric="Lnight",
        limit_db=WHO_AIR_LNIGHT,
        source="WHO Environmental Noise Guidelines (2018), Table 1",
        tooltip=(
            "Lnight: energy average over 23:00–07:00, with no penalty (EU Directive 2002/49/EC, Annex I). "
            "WHO Environmental Noise Guidelines for the European Region (2018): "
            "aircraft 40 dB Lnight, strong recommendation. "
            "Shown for reference only: a sound level meter cannot identify the noise source."
        ),
        category="who_env",
        source_specific=True,
    ),

    # ── WHO 2018 — Railway ────────────────────────────────────────────────────
    StandardRow(
        standard="WHO 2018 — Railway",
        metric="Lden",
        limit_db=WHO_RAIL_LDEN,
        source="WHO Environmental Noise Guidelines (2018), Table 1",
        tooltip=(
            "Lden: 24-hour energy average with +5 dB added to evening (19:00–23:00) and +10 dB to "
            "night (23:00–07:00) readings (EU Directive 2002/49/EC, Annex I). "
            "WHO Environmental Noise Guidelines for the European Region (2018): "
            "railway 54 dB Lden, strong recommendation. "
            "Shown for reference only: a sound level meter cannot identify the noise source."
        ),
        category="who_env",
        source_specific=True,
    ),
    StandardRow(
        standard="WHO 2018 — Railway",
        metric="Lnight",
        limit_db=WHO_RAIL_LNIGHT,
        source="WHO Environmental Noise Guidelines (2018), Table 1",
        tooltip=(
            "Lnight: energy average over 23:00–07:00, with no penalty (EU Directive 2002/49/EC, Annex I). "
            "WHO Environmental Noise Guidelines for the European Region (2018): "
            "railway 44 dB Lnight, strong recommendation. "
            "Shown for reference only: a sound level meter cannot identify the noise source."
        ),
        category="who_env",
        source_specific=True,
    ),

    # ── WHO Indoor — Bedroom ─────────────────────────────────────────────────
    StandardRow(
        standard="WHO Indoor — Bedroom (average)",
        metric="LAeq, night 23:00–07:00",
        limit_db=WHO_BEDROOM_LAEQ,
        source="WHO Guidelines for Community Noise (1999), Table 4.1",
        tooltip=(
            "LAeq over the night inside a bedroom. WHO Guidelines for Community Noise (1999), "
            "Table 4.1: 30 dB(A) LAeq, 8 h, inside bedrooms. Evaluated only for a microphone placed "
            "inside the bedroom."
        ),
        category="who_indoor",
        indoor_only=True,
    ),
    StandardRow(
        standard="WHO Indoor — Bedroom (single event)",
        metric="LAmax, night 23:00–07:00",
        limit_db=WHO_BEDROOM_LAMAX,
        source="WHO Guidelines for Community Noise (1999), Table 4.1",
        tooltip=(
            "Highest night-time reading on the L-Max channel. WHO Guidelines for Community Noise "
            "(1999), Table 4.1: 45 dB LAmax (fast) for single sound events inside bedrooms at night, "
            "which the guideline text frames as a level not to be exceeded more than about 10-15 "
            "times per night; a single maximum therefore cannot decide it, and this row is shown "
            "for reference only. Evaluated only for a microphone placed inside the bedroom."
        ),
        category="who_indoor",
        indoor_only=True,
        indicative_only=True,
    ),

    # ── Maryland COMAR — Residential ─────────────────────────────────────────
    StandardRow(
        standard="MD COMAR Day (07:00-22:00)",
        metric="LAeq (07:00–22:00)",
        limit_db=MD_RESIDENTIAL_DAY,
        source="COMAR 26.02.03.03 Table 2 — Maximum Allowable Noise Levels (residential)",
        tooltip=(
            "Maryland's enforceable residential limit, 7 a.m. to 10 p.m. "
            "(COMAR .01B(5) defines daytime hours). It is measured at or within "
            "the property line of the RECEIVING property, not at the source "
            "(.03D(2)). " + MD_TABLE2_METRIC_NOTE
        ),
        category="maryland",
    ),
    StandardRow(
        standard="MD COMAR Night (22:00-07:00)",
        metric="LAeq (22:00–07:00)",
        limit_db=MD_RESIDENTIAL_NIGHT,
        source="COMAR 26.02.03.03 Table 2 — Maximum Allowable Noise Levels (residential)",
        tooltip=(
            "Maryland's enforceable residential limit, 10 p.m. to 7 a.m. "
            "(COMAR .01B(15) defines nighttime hours). " + MD_TABLE2_METRIC_NOTE + " "
            "Prominent discrete tones and periodic noises must be 5 dB(A) below "
            "this level (COMAR .03A(3))."
        ),
        category="maryland",
    ),

    # ── Maryland COMAR .02 Table 1 — state environmental noise goal ───────────
    # Distinct from Table 2 above: Table 1 is the goal the state is working
    # toward, and for residential zones it is stated on Ldn — the one Maryland
    # figure directly comparable with a long-term average rather than with a
    # period level. Omitting it left the report silent on the state's own
    # health-oriented standard while quoting only its enforcement floor.
    StandardRow(
        standard="MD COMAR environmental noise goal (residential)",
        metric="Ldn",
        limit_db=MD_GOAL_RESIDENTIAL_LDN,
        source="COMAR 26.02.03.02 Table 1 — Environmental Noise Standards",
        tooltip=(
            "Maryland's environmental noise standard for residential zoning "
            "districts, 55 dB(A) Ldn. COMAR .02B(1) calls the Table 1 values "
            "\"goals for the attainment of an adequate environment\", which the "
            "enforceable Table 2 limits are intended to achieve — so this is a "
            "target rather than a level a person may not exceed. "
            "Ldn is defined in COMAR .01B(4) as the 24-hour energy average with "
            "+10 dB applied to 10 p.m.–7 a.m., which is exactly the Ldn computed "
            "in this report."
        ),
        category="maryland",
    ),
]


def evaluate_compliance(
    *,
    lden: float | None = None,
    lnight: float | None = None,
    ldn: float | None = None,
    laeq: float | None = None,
    laeq_day: float | None = None,
    laeq_night: float | None = None,
    lamax: float | None = None,
    lamax_night: float | None = None,
    environment: str = "outdoor",
) -> list[dict]:
    """
    Build the compliance results for all applicable standards.

    Only rows where a measured value is available are returned.
    The caller supplies whichever metrics they can compute from the dataset.

    Parameters
    ----------
    environment : str
        Sensor placement, ``"outdoor"`` (default) or ``"indoor"``. Indoor-only
        WHO bedroom guidelines (30 dB LAeq / 45 dB LAmax) are only emitted when
        ``environment == "indoor"``; comparing an outdoor mic against an indoor
        bedroom limit is not physically valid (15–25 dB facade attenuation).

    Result ``kind`` field
    ---------------------
    ``"compliance"`` rows carry a PASS/FAIL verdict against a directly applicable
    limit (road traffic, Maryland, indoor bedroom when indoor). ``"indicative"``
    rows compare total measured energy against a *source-specific* reference
    (aircraft, railway) that a source-blind meter cannot attribute — these report
    ABOVE/BELOW the reference rather than a compliance verdict.
    """

    def _valid(v: float | None) -> bool:
        return v is not None and isinstance(v, (int, float)) and math.isfinite(v)

    is_indoor = str(environment or "outdoor").strip().lower() == "indoor"

    # Map each StandardRow to the measured value most appropriate for it
    metric_map: list[tuple[StandardRow, float | None]] = [
        (MATRIX[0], lden),          # WHO Road Lden
        (MATRIX[1], lnight),        # WHO Road Lnight
        (MATRIX[2], lden),          # WHO Aircraft Lden   (indicative)
        (MATRIX[3], lnight),        # WHO Aircraft Lnight (indicative)
        (MATRIX[4], lden),          # WHO Railway Lden    (indicative)
        (MATRIX[5], lnight),        # WHO Railway Lnight  (indicative)
        (MATRIX[6], lnight),        # WHO Bedroom LAeq, night 8 h = LAeq 23:00-07:00 (indoor only)
        (MATRIX[7], lamax_night),   # WHO Bedroom LAmax, night-time readings (indoor only, indicative)
        (MATRIX[8], laeq_day),      # Maryland Residential Day   (Table 2)
        (MATRIX[9], laeq_night),    # Maryland Residential Night (Table 2)
        (MATRIX[10], ldn),          # Maryland residential goal  (Table 1, Ldn)
    ]

    results: list[dict] = []
    for row, measured in metric_map:
        if not _valid(measured):
            continue
        # Indoor bedroom guidelines are not valid against an outdoor placement.
        if row.indoor_only and not is_indoor:
            continue
        assert measured is not None

        delta = round(float(measured) - row.limit_db, 1)
        if row.source_specific or row.indicative_only:
            # Reference comparison — report the relation, not a compliance verdict.
            kind = "indicative"
            status = "ABOVE" if measured > row.limit_db else "BELOW"
        else:
            kind = "compliance"
            status = "PASS" if measured <= row.limit_db else "FAIL"

        results.append({
            "standard":        row.standard,
            "metric":          row.metric,
            "measured_db":     round(float(measured), 1),
            "limit_db":        row.limit_db,
            "status":          status,
            "kind":            kind,
            "source_specific": row.source_specific,
            "delta_db":        delta,
            "source":          row.source,
            "tooltip":         row.tooltip,
            "category":        row.category,
        })

    return results
