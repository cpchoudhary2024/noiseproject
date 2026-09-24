"""Metric-matched indicative comparisons; verified 2026-09-21.

WHO: https://www.who.int/publications/i/item/9789289053563
WHO 1999: https://www.who.int/publications/i/item/a68672
Maryland: https://regs.maryland.gov/us/md/exec/comar/26.02.03.02
Hours: https://regs.maryland.gov/us/md/exec/comar/26.02.03.01

A mixed-source, finite monitoring record cannot establish source-specific annual
exposure or legal compliance. No clinical risk or instrument provenance is inferred.
"""
import math

WHO_ROAD_LDEN, WHO_ROAD_LNIGHT = 53.0, 45.0
WHO_RAIL_LDEN, WHO_RAIL_LNIGHT = 54.0, 44.0
WHO_AIR_LDEN, WHO_AIR_LNIGHT = 45.0, 40.0
WHO_BEDROOM_LAEQ, WHO_BEDROOM_LAMAX = 30.0, 45.0
WHO_LIVING_LAEQ = WHO_CLASS_LAEQ = 35.0
MD_RESIDENTIAL_DAY, MD_RESIDENTIAL_NIGHT = 65.0, 55.0
MD_COMMERCIAL_DAY, MD_COMMERCIAL_NIGHT = 67.0, 62.0
MD_INDUSTRIAL_DAY = MD_INDUSTRIAL_NIGHT = 75.0

REFERENCE_NOTE = (
    'Indicative monitoring-period comparison only. WHO transport values concern '
    'long-term outdoor noise from the named source; the sensor measures all sources. '
    'An annual exposure or individual health outcome is not established by this comparison.'
)
MD_METRIC_NOTE = (
    'COMAR 26.02.03.02B Table 1: residential receiving land use. This period-LAeq '
    'comparison is screening only, not a legal determination. Source exemptions '
    '(including public-road motor vehicles, aircraft and railways), construction '
    'provisions and the 5 dB adjustment for prominent tones/periodic noise may apply. '
    'Receiving-property location and compliant measurement equipment must be established.'
)


def finite(value):
    try:
        return value is not None and math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def primary_column(columns):
    for col in columns:
        name = ''.join(c for c in str(col).lower() if c.isalnum())
        if 'leq' in name or 'laeq' in name:
            return col
    # Other SPL streams may be descriptive, but extrema are not interval LAeq.
    return next((c for c in columns if not any(t in ''.join(x for x in str(c).lower() if x.isalnum())
                                               for t in ('lmax', 'lmin'))), None)


def matrix_from_analysis(analysis, environment='outdoor'):
    stats = analysis.get('statistics', {})
    col = primary_column(stats)
    env = analysis.get('environmental_metrics', {}).get(col, {})
    stat = stats.get(col, {})
    lmax_col = next((c for c in stats if 'lmax' in ''.join(x for x in str(c).lower() if x.isalnum())), None)
    return evaluate_compliance(
        lden=env.get('Lden'), lnight=env.get('Lnight'), ldn=env.get('Ldn'),
        laeq=stat.get('laeq_db'), laeq_day=env.get('LAeq_day_ldn'),
        laeq_night=env.get('LAeq_night_ldn'),
        lamax=stats.get(lmax_col, {}).get('max'), environment=environment,
        bedroom_laeq=env.get('Lnight'))


def evaluate_compliance(*, lden=None, lnight=None, ldn=None, laeq=None,
                        laeq_day=None, laeq_night=None, lamax=None,
                        environment='outdoor', bedroom_laeq=None):
    """Return only available, matched metrics; no PASS/FAIL legal verdicts.

    Indoor placement alone does not prove a bedroom. Indoor rows are explicitly
    conditional bedroom references. LAmax timing/weighting is unverified.
    """
    indoor = environment == 'indoor'
    rows = []
    if not indoor:
        for source, day, night in [('Road Traffic', 53, 45), ('Aircraft Noise', 45, 40), ('Railway', 54, 44)]:
            for metric, value, limit in [('Lden', lden, day), ('Lnight', lnight, night)]:
                rows.append((f'WHO 2018 — {source}', metric, value, limit, 'who_env',
                             'WHO Environmental Noise Guidelines (2018)', REFERENCE_NOTE, True))
        rows.extend([
            ('MD COMAR Day (07:00-22:00)', 'LAeq (07:00–22:00)', laeq_day, 65, 'maryland',
             'COMAR 26.02.03.02B(1), Table 1', MD_METRIC_NOTE, False),
            ('MD COMAR Night (22:00-07:00)', 'LAeq (22:00–07:00)', laeq_night, 55, 'maryland',
             'COMAR 26.02.03.02B(1), Table 1', MD_METRIC_NOTE, False)])
    else:
        rows.extend([
            ('WHO Indoor — Bedroom (night average)', 'LAeq,8h (23:00–07:00)',
             bedroom_laeq if bedroom_laeq is not None else lnight, 30, 'who_indoor',
             'WHO Guidelines for Community Noise (1999), Table 4.1',
             'Conditional bedroom reference, 8-hour night average. Confirm bedroom placement and '
             'representative night coverage. Not applicable to all indoor spaces.', False),
            ('WHO Indoor — Bedroom (single event)', 'LAmax', lamax, 45, 'who_indoor',
             'WHO Guidelines for Community Noise (1999), Table 4.1',
             'Conditional bedroom reference: confirm nighttime events and A-weighted Fast maximum '
             '(LAFmax). An interval LAeq maximum cannot substitute for an event maximum.', False)])
    result = []
    for name, metric, measured, limit, category, source, note, specific in rows:
        if not finite(measured):
            continue
        value = float(measured)
        result.append(dict(standard=name, metric=metric, measured_db=round(value, 1),
                           limit_db=float(limit), status='ABOVE' if value > limit else 'BELOW',
                           kind='indicative', source_specific=specific,
                           delta_db=round(value-limit, 1), source=source, tooltip=note,
                           category=category))
    return result
