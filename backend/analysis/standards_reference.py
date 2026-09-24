# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""Authoritative standards & guideline reference data.

This module centralizes externally-sourced guideline values so they can be reused
consistently across the API, UI, and report generation.

Important:
- Many standards (e.g., ISO 1996) define *methods* for assessment, not universal
  legal limits.
- Legal limits vary by jurisdiction. The values here are published guideline
  levels and should be presented as such.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class GuidelineSource:
    key: str
    title: str
    year: int
    publisher: str
    url: str
    pdf_url: str | None = None


WHO_ENV_NOISE_2018 = GuidelineSource(
    key="who_2018_env_noise_europe",
    title=(
        "Environmental Noise Guidelines for the European Region"
    ),
    year=2018,
    publisher="World Health Organization (WHO), Regional Office for Europe",
    url="https://www.who.int/publications/i/item/9789289053563",
    # Direct PDF bitstream (may change over time; keep the publication page above as canonical).
    pdf_url="https://iris.who.int/server/api/core/bitstreams/f53c45ba-11d3-4502-a424-c1cf49f5a053/content",
)


def who_2018_environmental_noise_guideline_levels() -> dict:
    """WHO 2018 guideline levels for key environmental noise sources.

    Values are expressed as the guideline development group (GDG) recommendations
    for long-term average exposure.

    - Transport sources use $L_{den}$ and $L_{night}$.
    - Leisure noise uses $L_{Aeq,24h}$.
    - Indoor spaces use $L_{Aeq}$ or $L_{Amax}$ as specified.

    Returns a JSON-serializable dict.
    """

    return {
        "source": {
            "key": WHO_ENV_NOISE_2018.key,
            "title": WHO_ENV_NOISE_2018.title,
            "year": WHO_ENV_NOISE_2018.year,
            "publisher": WHO_ENV_NOISE_2018.publisher,
            "url": WHO_ENV_NOISE_2018.url,
            "pdf_url": WHO_ENV_NOISE_2018.pdf_url,
        },
        "guidelines": {
            # Transport sources - Outdoor environmental noise
            "road_traffic": {
                "recommendation_strength": "strong",
                "metrics_db": {"Lden": 53, "Lnight": 45},
                "notes": "Reduce road traffic noise below 53 dB Lden and 45 dB Lnight.",
                "health_threshold": "Source-specific long-term guideline; not an individual risk prediction",
            },
            "railway": {
                "recommendation_strength": "strong",
                "metrics_db": {"Lden": 54, "Lnight": 44},
                "notes": "Reduce railway noise below 54 dB Lden and 44 dB Lnight.",
            },
            "aircraft": {
                "recommendation_strength": "strong",
                "metrics_db": {"Lden": 45, "Lnight": 40},
                "notes": "Reduce aircraft noise below 45 dB Lden and 40 dB Lnight. Aircraft noise causes greater annoyance than road/rail noise at same dB level.",
            },
            "wind_turbines": {
                "recommendation_strength": "conditional",
                "metrics_db": {"Lden": 45},
                "notes": "Conditionally recommend reducing wind turbine noise below 45 dB Lden. No Lnight recommendation.",
            },
            "leisure": {
                "recommendation_strength": "conditional",
                "metrics_db": {"LAeq_24h": 70},
                "notes": "Conditionally recommend reducing yearly average leisure noise to 70 dB LAeq,24h.",
            },
            # Indoor spaces (from WHO 1999 Guidelines for Community Noise)
            "bedroom_sleep": {
                "recommendation_strength": "WHO 1999 guidance",
                "metrics_db": {"LAeq": 30, "LAmax": 45},
                "notes": "Bedroom average: ≤30 dB LAeq protects sleep quality. Single-event reference: 45 dB LAFmax; not a guarantee against awakening.",
                "time_period": "8-hour bedroom night; the application uses 23:00–07:00",
            },
            "living_room": {
                "recommendation_strength": "WHO 1999 guidance",
                "metrics_db": {"LAeq": 35},
                "notes": "Living room: ≤35 dB LAeq allows comfortable conversation.",
            },
            "classroom": {
                "recommendation_strength": "WHO 1999 guidance",
                "metrics_db": {"LAeq": 35},
                "notes": "Classroom: ≤35 dB LAeq allows effective learning and concentration.",
            },
        },
        "health_thresholds": {},
        "definitions": {
            "Lden": (
                "Day-Evening-Night level: 24-hour average with penalties to reflect greater impact during evening/night. "
                "Calculation: Evening hours (+5 dB penalty) and night hours (+10 dB penalty) are weighted more heavily than daytime. "
                "Time periods: Day 07:00-19:00 (no penalty), Evening 19:00-23:00 (+5 dB), Night 23:00-07:00 (+10 dB)"
            ),
            "Lnight": (
                "Average sound level during nighttime hours (23:00-07:00). "
                "No penalty applied; represents the actual average noise level during this sleep-critical period. "
                "Used for assessing sleep disturbance and chronic health effects."
            ),
            "LAeq": (
                "Equivalent Continuous Level: The steady sound level that contains the same total sound energy "
                "as the actual fluctuating sound over the measurement period. Energy-averaged (logarithmic) measurement."
            ),
            "LAmax": (
                "Maximum A-weighted sound level recorded during measurement period. "
                "The WHO 1999 bedroom event reference requires Fast time weighting."
            ),
            "LA90": (
                "Background level: The sound level exceeded 90% of the time. "
                "Represents the underlying baseline ambient noise when obvious discrete events are removed."
            ),
            "LAeq_24h": "24-hour energy-average sound level (typically for leisure/recreational noise assessment)",
            "dB_A": (
                "Decibels A-weighted: Standard for environmental and occupational noise. "
                "A-weighting filter adjusts measurements to match human hearing perception across frequencies."
            ),
            "dB_C": (
                "Decibels C-weighted: Used for peak/impulsive noise and low-frequency assessment. "
                "Flatter frequency response than A-weighting; important for measuring loud transient events."
            ),
            "LOAEL": (
                "Lowest Observed Adverse Effect Level: The lowest exposure level at which adverse health effects "
                "are observed in a specified study; it is not a universal individual threshold."
            ),
            "Decibel_scale": (
                "Logarithmic scale where +3 dB = energy doubles (just noticeable); +10 dB = sounds twice as loud. "
                "Each 10 dB increase represents 10× more sound energy."
            ),
        },
        "disclaimer": (
            "These are WHO guideline levels (health-based recommendations), not universal legal limits. "
            "Applicable regulatory limits depend on jurisdiction, land use, and permitting context. "
            "Maryland state regulations differ: residential daytime 65 dB(A), nighttime 55 dB(A) (more permissive than WHO)."
        ),
    }
