# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""WHO guideline and Maryland COMAR comparisons.

Sources: WHO Environmental Noise Guidelines for the European Region (2018); WHO
Guidelines for Community Noise (1999), Table 4.1, for the indoor values; Maryland
COMAR 26.02.03.01 (definitions) and 26.02.03.02B(1) Table 1 (limits).
OSHA and NIOSH limits are for workplaces, so they are not used here.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


WHO_ROAD_LDEN    = 53.0   # dB(A), road traffic, 24 h Lden
WHO_ROAD_LNIGHT  = 45.0   # dB(A), road traffic, Lnight (23:00-07:00)
WHO_RAIL_LDEN    = 54.0   # dB(A), railway
WHO_RAIL_LNIGHT  = 44.0   # dB(A), railway nighttime
WHO_AIR_LDEN     = 45.0   # dB(A), aircraft (stricter: unpredictable bursts)
WHO_AIR_LNIGHT   = 40.0   # dB(A), aircraft nighttime
WHO_LOAEL_LNIGHT = 40.0   # dB(A), LOAEL for nighttime sleep effects

# WHO indoor limits (WHO 1999, Table 4.1)
WHO_BEDROOM_LAEQ  = 30.0  # dB(A), bedroom average (protect sleep quality)
WHO_BEDROOM_LAMAX = 45.0  # dB(A), bedroom, single event (prevent awakening)
WHO_LIVING_LAEQ   = 35.0  # dB(A), living room
WHO_CLASS_LAEQ    = 35.0  # dB(A), classroom

MD_RESIDENTIAL_DAY    = 65.0   # dB(A)
MD_RESIDENTIAL_NIGHT  = 55.0   # dB(A)
MD_COMMERCIAL_DAY     = 67.0   # dB(A)
MD_COMMERCIAL_NIGHT   = 62.0   # dB(A)
MD_INDUSTRIAL_DAY     = 75.0   # dB(A)
MD_INDUSTRIAL_NIGHT   = 75.0   # dB(A)

MD_METRIC_NOTE = (
    "COMAR 26.02.03.02A(2) expresses these standards as equivalent A-weighted sound "
    "levels; this comparison uses the LAeq of the daytime (07:00–22:00) or nighttime "
    "(22:00–07:00) hours. It is not a legal determination: COMAR exempts sources "
    "including motor vehicles on public roads, aircraft at licensed airports, railroads "
    "and residential air-conditioning (26.02.03.02C), and a compliance measurement is "
    "made at the receiving property line with a Type II or better meter (26.02.03.02D)."
)


@dataclass(frozen=True)
class StandardRow:
    """One row in the compliance matrix."""
    standard: str      # Regulatory body + source name
    metric: str        # Acoustic metric (Lden, Lnight, LAeq, LAmax …)
    limit_db: float    # Health/legal limit in dB(A)
    source: str        # Exact citation
    tooltip: str
    category: str      # "who_env" | "who_indoor" | "maryland"
    # source-specific limits are indicative only, a meter can't tell sources apart
    source_specific: bool = False
    # Indoor guidelines (bedroom) are only valid against an indoor-placed sensor.
    indoor_only: bool = False
    indicative_only: bool = False


# Ordered from most health-relevant to regulatory
MATRIX: list[StandardRow] = [

    # WHO 2018, Road Traffic
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

    # WHO 2018, Aircraft
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

    # WHO 2018, Railway
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

    # WHO Indoor, Bedroom
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

    # Maryland COMAR 26.02.03.02B(1), Table 1, residential receiving land
    StandardRow(
        standard="MD COMAR Day (07:00-22:00)",
        metric="LAeq (07:00–22:00)",
        limit_db=MD_RESIDENTIAL_DAY,
        source="COMAR 26.02.03.02B(1), Table 1 — Maximum Allowable Noise Levels (residential)",
        tooltip=(
            "Maryland maximum allowable level for residential receiving land, daytime "
            "hours 7 a.m. to 10 p.m. (COMAR 26.02.03.01B(4)). " + MD_METRIC_NOTE
        ),
        category="maryland",
    ),
    StandardRow(
        standard="MD COMAR Night (22:00-07:00)",
        metric="LAeq (22:00–07:00)",
        limit_db=MD_RESIDENTIAL_NIGHT,
        source="COMAR 26.02.03.02B(1), Table 1 — Maximum Allowable Noise Levels (residential)",
        tooltip=(
            "Maryland maximum allowable level for residential receiving land, nighttime "
            "hours 10 p.m. to 7 a.m. (COMAR 26.02.03.01B(14)). Prominent discrete tones and "
            "periodic noises must be 5 dBA below this level (26.02.03.02B(3)). " + MD_METRIC_NOTE
        ),
        category="maryland",
    ),
]


def evaluate_compliance(
    *,
    lden=None,
    lnight=None,
    ldn=None,
    laeq=None,
    laeq_day=None,
    laeq_night=None,
    lamax=None,
    lamax_night=None,
    environment="outdoor",
):
    """Build the compliance results for all applicable standards."""

    def valid(v):
        return v is not None and isinstance(v, (int, float)) and math.isfinite(v)

    is_indoor = str(environment or "outdoor").strip().lower() == "indoor"

    # Map each StandardRow to the measured value most appropriate for it
    metric_map = [
        (MATRIX[0], lden),          # WHO Road Lden
        (MATRIX[1], lnight),        # WHO Road Lnight
        (MATRIX[2], lden),          # WHO Aircraft Lden   (indicative)
        (MATRIX[3], lnight),        # WHO Aircraft Lnight (indicative)
        (MATRIX[4], lden),          # WHO Railway Lden    (indicative)
        (MATRIX[5], lnight),        # WHO Railway Lnight  (indicative)
        (MATRIX[6], lnight),
        (MATRIX[7], lamax_night),
        (MATRIX[8], laeq_day),      # Maryland Residential Day   (Table 1)
        (MATRIX[9], laeq_night),    # Maryland Residential Night (Table 1)
    ]

    results = []
    for row, measured in metric_map:
        if not valid(measured):
            continue
        # Indoor bedroom guidelines are not valid against an outdoor placement.
        if row.indoor_only and not is_indoor:
            continue
        assert measured is not None

        delta = round(float(measured) - row.limit_db, 1)
        # A monitoring record is compared with each value; it does not establish compliance.
        kind = "indicative" if (row.source_specific or row.indicative_only) else "comparison"
        status = "ABOVE" if measured > row.limit_db else "AT OR BELOW"

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
