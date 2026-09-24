"""Shared, descriptive monitoring assessment (not a WHO clinical risk scale)."""
from .compliance_matrix import finite


def concern_level(lden, lnight):
    if not finite(lden) and not finite(lnight):
        return 'NOT ASSESSED'
    excesses = [float(v)-limit for v, limit in [(lden, 53), (lnight, 45)] if finite(v)]
    excess = max(excesses)
    if excess >= 10:
        return 'SERIOUS'
    if excess >= 5:
        return 'HIGH'
    if excess > 0:
        return 'MODERATE-HIGH'
    if not finite(lden) or not finite(lnight):
        return 'INCOMPLETE'
    return 'MODERATE' if float(lnight) > 40 else 'LOW'


def assessment_note(lden, lnight):
    level = concern_level(lden, lnight)
    text = {
        'NOT ASSESSED': 'No usable Lden or Lnight is available; no guideline comparison can be made.',
        'INCOMPLETE': 'An available metric is at or below its reference, but a required period is missing. No overall assessment is possible.',
        'LOW': 'The measured indices are at or below the road-traffic reference values.',
        'MODERATE': 'The measured indices are at or below the road-traffic references; Lnight is above the WHO 2009 outdoor annual night-noise target of 40 dB.',
        'MODERATE-HIGH': 'An available index is above its road-traffic reference by less than 5 dB.',
        'HIGH': 'An available index is above its road-traffic reference by 5 to less than 10 dB.',
        'SERIOUS': 'An available index is above its road-traffic reference by at least 10 dB.',
    }[level]
    return (text + ' These are application screening categories, not WHO clinical risk grades. '
            'Mixed-source, short-term measurements do not establish annual source-specific '
            'exposure or individual health effects. Check coverage, source and measurement uncertainty.')
