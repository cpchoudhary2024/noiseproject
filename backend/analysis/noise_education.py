"""Educational content about noise, decibels, and health impacts.

This module provides reference information for reports and educational materials
aligned with WHO 2018 Environmental Noise Guidelines.
"""


def decibel_scale_reference() -> dict:
    """Reference guide: Decibel scale and examples.
    
    The decibel scale is logarithmic, not linear. Each 10 dB increase 
    represents 10× more acoustic energy and sounds ~2× louder to the human ear.
    """
    return {
        "title": "Decibel Scale Reference",
        "note": "dB scale is LOGARITHMIC: +10 dB = 10× energy, sounds 2× louder",
        "levels": [
            {"dB": 30, "example": "Quiet bedroom / whisper", "feel": "Whisper quiet", "health_concern": "None - ideal for sleep"},
            {"dB": 40, "example": "Library / soft background", "feel": "Peaceful", "health_concern": "None - good for sleep"},
            {"dB": 50, "example": "Light rain / soft conversation", "feel": "Comfortable", "health_concern": "Conversation interference may start"},
            {"dB": 60, "example": "Normal conversation / office", "feel": "Noticeable", "health_concern": "Annoyance begins"},
            {"dB": 70, "example": "Busy street / vacuum cleaner", "feel": "Loud", "health_concern": "Stress responses start"},
            {"dB": 80, "example": "Heavy traffic / alarm clock", "feel": "Very loud", "health_concern": "Hearing risk with long exposure"},
            {"dB": 85, "example": "Vacuum / lawn mower / alarm", "feel": "Extremely loud", "health_concern": "Occupational threshold - 8hr exposure can cause permanent damage"},
            {"dB": 90, "example": "Power tools / heavy machinery", "feel": "Very uncomfortable", "health_concern": "Hearing damage possible after 4 hours continuous"},
            {"dB": 100, "example": "Motorcycle / chainsaw", "feel": "Painful", "health_concern": "Hearing damage likely after 15 minutes"},
            {"dB": 110, "example": "Live rock concert", "feel": "Intolerable", "health_concern": "Immediate hearing damage risk"},
            {"dB": 130, "example": "Jet aircraft takeoff", "feel": "Dangerous", "health_concern": "Immediate pain and hearing damage"},
        ],
        "key_concepts": {
            "3dB_rule": {
                "change": "+3 dB",
                "acoustic_energy": "Doubles (2×)",
                "perceived_loudness": "Just noticeable difference",
                "significance": "Minimum change humans typically detect"
            },
            "10dB_rule": {
                "change": "+10 dB",
                "acoustic_energy": "Increases 10 times (10×)",
                "perceived_loudness": "Sounds approximately twice as loud",
                "significance": "Readily noticeable; commonly used in regulations"
            },
            "20dB_rule": {
                "change": "+20 dB",
                "acoustic_energy": "Increases 100 times (100×)",
                "perceived_loudness": "Sounds approximately 4× as loud",
            },
        },
        "sources": [
            "WHO Guidelines for Community Noise (1999)",
            "NIOSH Occupational Exposure Criteria",
            "ISO 1996-1:2016 Acoustic Standards",
        ],
    }


def frequency_weighting_guide() -> dict:
    """Frequency weighting filters: dB(A), dB(C), dB(Z) explained.
    
    Different weighting filters match different measurement purposes and how
    human ears perceive sound at different levels.
    """
    return {
        "title": "Frequency Weighting Explained",
        "concept": "Human ears don't hear all frequencies equally. Weighting filters adjust for this.",
        "weightings": {
            "dB_A_weighting": {
                "symbol": "dB(A)",
                "purpose": "Environmental and occupational noise measurement - standard for most applications",
                "response": "Approximates human hearing at moderate loudness levels",
                "reduces": "Extreme low frequencies and high frequencies",
                "use_cases": [
                    "Community noise assessment (WHO standard)",
                    "Occupational exposure limits (OSHA, NIOSH, EU)",
                    "Environmental health studies",
                    "Industrial noise monitoring",
                ],
                "standard": "IEC 61672-1:2013",
                "note": "When a noise report shows 'dB' without specification, it usually means dB(A)",
            },
            "dB_C_weighting": {
                "symbol": "dB(C)",
                "purpose": "Peak and impulsive noise measurement; low-frequency assessment",
                "response": "Flatter response across frequencies; less filtering than A-weighting",
                "use_cases": [
                    "Measuring loud impact noise (explosions, pile driving, gunshots)",
                    "Peak noise levels in occupational settings",
                    "Low-frequency noise assessment",
                    "Building vibration measurements",
                ],
                "occupational_standard": "OSHA and EU specify occupational peak limits in dB(C): 140 dB(C)",
                "note": "Important distinction: OSHA uses dB(A) for 8-hour TWA but dB(C) for peak limits",
            },
            "dB_Z_weighting": {
                "symbol": "dB(Z)",
                "purpose": "Raw/unweighted measurement - purely technical/research applications",
                "response": "Completely flat; no frequency filtering applied",
                "use_cases": [
                    "Acoustic research and testing",
                    "Equipment calibration verification",
                    "Advanced acoustic modeling",
                ],
                "practical_use": "Not used for health assessments or regulatory compliance",
            },
        },
        "measurement_context": {
            "environmental_noise": {
                "metric": "dB(A)",
                "reason": "Matches how humans perceive environmental noise at moderate levels",
                "examples": "Road traffic, aircraft, neighborhood noise",
            },
            "occupational_exposure": {
                "average_exposure": {"metric": "dB(A) TWA", "reason": "Cumulative hearing damage risk"},
                "peak_events": {"metric": "dB(C)", "reason": "Immediate traumatic hearing damage risk"},
                "note": "OSHA requires both: dB(A) for 8-hour exposure AND dB(C) for peaks",
            },
            "sleep_protection": {
                "metric": "dB(A)",
                "reason": "Sleep disturbance is based on perceived loudness, not low-freq energy",
            },
        },
        "important_note": (
            "The same sound level in dB(A) and dB(C) can be different numbers because they measure "
            "different aspects of sound. For community noise health protection, dB(A) is standard."
        ),
    }


def health_effects_by_level() -> dict:
    """Health effects of noise exposure at different levels."""
    return {
        "title": "Health Effects of Noise by Exposure Level",
        "source": "WHO 2018 Environmental Noise Guidelines; peer-reviewed epidemiological studies",
        "effects_by_exposure": [
            {
                "exposure_level": "Below 45 dB Lnight",
                "time_scale": "Long-term (years)",
                "health_effects": [
                    "No significant adverse effects expected",
                    "Sleep remains largely protected",
                    "Cardiovascular risks not elevated",
                ],
                "recommendation": "WHO guideline target",
            },
            {
                "exposure_level": "40 dB Lnight",
                "time_scale": "Chronic exposure",
                "health_effects": [
                    "LOAEL (Lowest Observed Adverse Effect Level) for sleep",
                    "Increased body movements during sleep",
                    "Early sleep quality changes detectable in research",
                ],
                "recommendation": "WHO sleep guideline threshold",
            },
            {
                "exposure_level": "45-50 dB Lnight",
                "time_scale": "Chronic exposure",
                "health_effects": [
                    "Self-reported sleep quality begins to decline",
                    "Measurable sleep architecture changes",
                    "Heart rate and blood pressure increases during sleep",
                    "Increased awakenings possible",
                ],
                "vulnerable_groups": ["Elderly", "Shift workers", "Children"],
            },
            {
                "exposure_level": "55 dB Lden (24-hour)",
                "time_scale": "Long-term (years/decades)",
                "health_effects": [
                    "Cardiovascular disease risk increases (~8% per 10 dB above this)",
                    "High blood pressure risk increases (~5-7% per 10 dB)",
                    "Stroke risk increases (~14% per 10 dB)",
                    "Significant annoyance and mental health impacts",
                ],
                "vulnerable_groups": ["People with existing heart conditions", "Hypertension", "Diabetes"],
                "recommendation": "WHO environmental guideline for day-evening-night average",
            },
            {
                "exposure_level": "55-65 dB Lden",
                "time_scale": "Long-term",
                "health_effects": [
                    "Pronounced cardiovascular health impacts",
                    "Increased annoyance (17-25% of population highly annoyed)",
                    "Sleep disturbance in sensitive populations",
                    "Possible learning/cognitive effects in children",
                ],
            },
            {
                "exposure_level": "Above 70 dB Lden",
                "time_scale": "Any duration",
                "health_effects": [
                    "Severe annoyance (25-52% of population depending on source)",
                    "Marked cardiovascular health risks",
                    "Sleep disturbance common even in less sensitive people",
                    "Significant cognitive effects in children",
                ],
                "note": "Requires urgent intervention and mitigation",
            },
            {
                "exposure_level": "85+ dB (occupational 8-hour TWA)",
                "time_scale": "Work shift exposure",
                "health_effects": [
                    "Permanent hearing damage risk increases rapidly",
                    "Hearing protection program required",
                    "Noise-induced hearing loss (NIHL) probable with years of exposure",
                    "OSHA action level - mandatory controls",
                ],
            },
            {
                "exposure_level": "90+ dB (occupational 8-hour TWA)",
                "time_scale": "Work shift exposure",
                "health_effects": [
                    "High risk of permanent hearing damage",
                    "OSHA Permissible Exposure Limit (PEL)",
                    "Hearing protection mandatory",
                    "Significant NIHL risk even with protection",
                ],
            },
        ],
        "vulnerable_populations": {
            "children": {
                "why_vulnerable": "Developing brains more sensitive; learning is auditory-dependent",
                "specific_effects": ["Reading ability reduced by 1 month delay per 10 dB aircraft noise", "Memory and concentration impairment", "Language development delays possible"],
            },
            "elderly": {
                "why_vulnerable": "Sleep naturally lighter; existing health conditions worsen",
                "specific_effects": ["More sleep fragmentation from lower noise levels", "Cardiovascular conditions exacerbated"],
            },
            "shift_workers": {
                "why_vulnerable": "Must sleep during noisier daytime hours",
                "specific_effects": ["Cannot avoid peak traffic/construction noise during sleep", "Cumulative sleep debt"],
            },
            "pregnant_women": {
                "why_vulnerable": "Stress hormone increases may affect fetal development",
                "specific_effects": ["Possible fetal impacts from maternal stress response"],
            },
            "people_with_cardiovascular_disease": {
                "why_vulnerable": "Noise-induced stress response compounds existing disease",
                "specific_effects": ["Blood pressure spikes risk cardiovascular events", "Additional cortisol stress"],
            },
        },
        "important_notes": [
            "Health effects occur even when people are unaware of the noise or believe they've adapted",
            "The body's stress response (cortisol, adrenaline, blood pressure) happens automatically",
            "Chronic low-level noise is cumulative and causes lasting physiological damage",
            "Sleep disruption from noise is independent of conscious awakening",
        ],
    }


def annoyance_by_noise_source() -> dict:
    """Annoyance levels vary by noise source at the same dB level."""
    return {
        "title": "Community Annoyance by Noise Source",
        "key_finding": "Aircraft and railway noise cause more annoyance than road traffic at the same dB level",
        "source": "WHO 2018 Environmental Noise Guidelines; Guski et al. (2017) systematic review",
        "annoyance_data": {
            "50 dB Lden": {
                "road_traffic": "6% highly annoyed",
                "aircraft": "12% highly annoyed",
                "note": "Aircraft causes 2× more annoyance"
            },
            "55 dB Lden": {
                "road_traffic": "11% highly annoyed",
                "aircraft": "20% highly annoyed",
            },
            "60 dB Lden": {
                "road_traffic": "17% highly annoyed",
                "aircraft": "30% highly annoyed",
            },
            "65 dB Lden": {
                "road_traffic": "25% highly annoyed",
                "aircraft": "40% highly annoyed",
            },
            "70 dB Lden": {
                "road_traffic": "34% highly annoyed",
                "aircraft": "52% highly annoyed",
            },
        },
        "why_differences": [
            "Aircraft noise: sudden, unpredictable bursts; occurs at all hours including night",
            "Railway noise: intermittent with low-frequency vibration; affects sleep more",
            "Road traffic: more continuous/predictable; people habituate slightly more",
        ],
        "implications": [
            "WHO sets stricter aircraft limits (45 dB Lden) than road traffic (53 dB Lden)",
            "Same dB number does not mean same health/annoyance impact across sources",
            "Context and predictability matter for perceived and actual health impacts",
        ],
    }
