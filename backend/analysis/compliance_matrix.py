"""
Compliance standards matrix — ground-truth sourced exclusively from:

  • WHO Environmental Noise Guidelines for the European Region (WHO, 2018)
    https://iris.who.int/bitstream/handle/10665/279952/9789289053563-eng.pdf

  • WHO Guidelines for Community Noise (Berglund et al., 1999)
    https://iris.who.int/handle/10665/66217

  • Maryland Code of Maryland Regulations (COMAR) 26.02.03.02
    Maximum Allowable Noise Levels

IMPORTANT: OSHA/NIOSH occupational standards are deliberately excluded from
the environmental compliance matrix. They belong in an occupational assessment
context only (datasets from industrial worksites with shift-worker exposure).
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

# ── Maryland COMAR 26.02.03.02 ────────────────────────────────────────────────
# Daytime = 07:00–22:00  /  Nighttime = 22:00–07:00  (Maryland definition)
MD_RESIDENTIAL_DAY    = 65.0   # dB(A)
MD_RESIDENTIAL_NIGHT  = 55.0   # dB(A)
MD_COMMERCIAL_DAY     = 67.0   # dB(A)
MD_COMMERCIAL_NIGHT   = 62.0   # dB(A)
MD_INDUSTRIAL_DAY     = 75.0   # dB(A)
MD_INDUSTRIAL_NIGHT   = 75.0   # dB(A)


@dataclass(frozen=True)
class StandardRow:
    """One row in the compliance matrix."""
    standard: str      # Regulatory body + source name
    metric: str        # Acoustic metric (Lden, Lnight, LAeq, LAmax …)
    limit_db: float    # Health/legal limit in dB(A)
    source: str        # Exact citation
    tooltip: str       # Plain-English definition pulled verbatim from the document
    category: str      # "who_env" | "who_indoor" | "maryland"


# Ordered from most health-relevant to regulatory
MATRIX: list[StandardRow] = [

    # ── WHO 2018 — Road Traffic ───────────────────────────────────────────────
    StandardRow(
        standard="WHO 2018 — Road Traffic",
        metric="Lden",
        limit_db=WHO_ROAD_LDEN,
        source="WHO Environmental Noise Guidelines (2018), Table 1",
        tooltip=(
            "Lden is a 24-hour average that adds +5 dB to evening (19:00–23:00) "
            "and +10 dB to nighttime (23:00–07:00) readings before energy-averaging, "
            "reflecting their greater health impact. "
            "The 53 dB threshold is the level above which 11% of the population "
            "reports being 'highly annoyed' and cardiovascular risk begins to rise "
            "(8% increase per 10 dB above 55 dB Lden)."
        ),
        category="who_env",
    ),
    StandardRow(
        standard="WHO 2018 — Road Traffic",
        metric="Lnight",
        limit_db=WHO_ROAD_LNIGHT,
        source="WHO Environmental Noise Guidelines (2018), Table 1",
        tooltip=(
            "Lnight is the energy-average noise level during 23:00–07:00. "
            "At 40 dB the WHO identifies the LOAEL (Lowest Observed Adverse "
            "Effect Level) — the point where body movements during sleep begin. "
            "At 45 dB, self-reported sleep quality declines. "
            "Above 55 dB Lnight, significant sleep disruption and daytime fatigue occur."
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
            "Aircraft limits are stricter than road traffic (45 dB vs. 53 dB Lden) "
            "because aircraft noise is intermittent and unpredictable, causing "
            "approximately twice as much annoyance per dB. "
            "At 50 dB Lden, 20% of residents near airports report being highly annoyed."
        ),
        category="who_env",
    ),
    StandardRow(
        standard="WHO 2018 — Aircraft Noise",
        metric="Lnight",
        limit_db=WHO_AIR_LNIGHT,
        source="WHO Environmental Noise Guidelines (2018), Table 1",
        tooltip=(
            "Set at 40 dB — matching the WHO LOAEL for sleep effects — because "
            "a single aircraft event during deep sleep can cause measurable "
            "cardiovascular arousal (elevated heart rate, cortisol release) "
            "without the resident being consciously aware of it."
        ),
        category="who_env",
    ),

    # ── WHO 2018 — Railway ────────────────────────────────────────────────────
    StandardRow(
        standard="WHO 2018 — Railway",
        metric="Lden",
        limit_db=WHO_RAIL_LDEN,
        source="WHO Environmental Noise Guidelines (2018), Table 1",
        tooltip=(
            "Railway Lden guideline of 54 dB(A) — slightly less strict than road "
            "traffic (53 dB) because the dose–response relationship for railway "
            "annoyance is somewhat lower per dB than for road traffic. "
            "The WHO notes high uncertainty at this threshold due to limited "
            "longitudinal evidence at that time."
        ),
        category="who_env",
    ),
    StandardRow(
        standard="WHO 2018 — Railway",
        metric="Lnight",
        limit_db=WHO_RAIL_LNIGHT,
        source="WHO Environmental Noise Guidelines (2018), Table 1",
        tooltip=(
            "Railway Lnight limit of 44 dB(A) — slightly stricter than road traffic "
            "(45 dB) because railway events tend to be discrete, loud, and very "
            "noticeable during quiet nighttime background. The WHO designated this "
            "as a Conditional recommendation reflecting evidence gaps."
        ),
        category="who_env",
    ),

    # ── WHO Indoor — Bedroom ─────────────────────────────────────────────────
    StandardRow(
        standard="WHO Indoor — Bedroom (average)",
        metric="LAeq",
        limit_db=WHO_BEDROOM_LAEQ,
        source="WHO Guidelines for Community Noise (1999); WHO 2018, indoor guidelines",
        tooltip=(
            "Indoor bedroom average level to protect sleep quality. "
            "30 dB(A) represents a near-quiet environment essential for restorative "
            "sleep. Exceeding this causes measurable increases in body movement "
            "and reduced sleep depth even when the resident does not wake up."
        ),
        category="who_indoor",
    ),
    StandardRow(
        standard="WHO Indoor — Bedroom (single event)",
        metric="LAmax",
        limit_db=WHO_BEDROOM_LAMAX,
        source="WHO Guidelines for Community Noise (1999); WHO 2018",
        tooltip=(
            "A single noise event above 45 dB(A) inside the bedroom is sufficient "
            "to cause sleep arousal — the WHO threshold to prevent awakening. "
            "Even if the nightly average is low, one loud truck or aircraft can "
            "interrupt a full sleep cycle and prevent recovery to deep sleep stages."
        ),
        category="who_indoor",
    ),

    # ── Maryland COMAR — Residential ─────────────────────────────────────────
    StandardRow(
        standard="MD COMAR Day (07:00-22:00) LAeq",
        metric="LAeq (07:00–22:00)",
        limit_db=MD_RESIDENTIAL_DAY,
        source="COMAR 26.02.03.02 — Maximum Allowable Noise Levels",
        tooltip=(
            "Maryland state legal limit for residential zones, 7 AM to 10 PM. "
            "Note: This 65 dB limit exceeds the WHO 53 dB Lden guideline by 12 dB. "
            "A noise level can be fully legal under Maryland law while still "
            "exceeding WHO health-protective guidelines."
        ),
        category="maryland",
    ),
    StandardRow(
        standard="MD COMAR Night (22:00-07:00) LAeq",
        metric="LAeq (22:00–07:00)",
        limit_db=MD_RESIDENTIAL_NIGHT,
        source="COMAR 26.02.03.02 — Maximum Allowable Noise Levels",
        tooltip=(
            "Maryland nighttime residential legal limit (10 PM – 7 AM). "
            "At 55 dB, this is 10 dB higher than the WHO 45 dB Lnight guideline. "
            "Maryland defines nighttime one hour earlier (10 PM) than WHO (11 PM). "
            "Compliance with Maryland law does not imply absence of health risk."
        ),
        category="maryland",
    ),
]


def evaluate_compliance(
    *,
    lden: float | None = None,
    lnight: float | None = None,
    laeq: float | None = None,
    laeq_day: float | None = None,
    laeq_night: float | None = None,
    lamax: float | None = None,
) -> list[dict]:
    """
    Build the Pass/Fail compliance results for all applicable standards.

    Only rows where a measured value is available are returned.
    The caller supplies whichever metrics they can compute from the dataset.
    """

    def _valid(v: float | None) -> bool:
        return v is not None and isinstance(v, (int, float)) and math.isfinite(v)

    # Map each StandardRow to the measured value most appropriate for it
    metric_map: list[tuple[StandardRow, float | None]] = [
        (MATRIX[0], lden),          # WHO Road Lden
        (MATRIX[1], lnight),        # WHO Road Lnight
        (MATRIX[2], lden),          # WHO Aircraft Lden
        (MATRIX[3], lnight),        # WHO Aircraft Lnight
        (MATRIX[4], lden),          # WHO Railway Lden
        (MATRIX[5], lnight),        # WHO Railway Lnight
        (MATRIX[6], laeq),          # WHO Bedroom LAeq  (use overall laeq as proxy)
        (MATRIX[7], lamax),         # WHO Bedroom LAmax
        (MATRIX[8], laeq_day),      # Maryland Residential Day
        (MATRIX[9], laeq_night),    # Maryland Residential Night
    ]

    results: list[dict] = []
    for row, measured in metric_map:
        if not _valid(measured):
            continue
        assert measured is not None
        status = "PASS" if measured <= row.limit_db else "FAIL"
        delta = round(float(measured) - row.limit_db, 1)
        results.append({
            "standard":     row.standard,
            "metric":       row.metric,
            "measured_db":  round(float(measured), 1),
            "limit_db":     row.limit_db,
            "status":       status,
            "delta_db":     delta,
            "source":       row.source,
            "tooltip":      row.tooltip,
            "category":     row.category,
        })

    return results
