# Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
"""Bring exports from different sound level meters to one column layout.

Everything downstream reads the NSRT_W mk4 names (LEQ, L-Max, L-Min, A-weighted),
so other instruments are mapped onto them here, once, instead of every module
guessing at column names on its own.
"""
import re

import numpy as np
import pandas as pd

LEQ = 'LEQ dB -A'
LMAX = 'L-Max dB -A'
LMIN = 'L-Min dB -A'
TIME = 'Timestamp'


class IngestError(ValueError):
    """The file was read but its columns can't be mapped without guessing."""


def _key(name):
    return re.sub(r'[^a-z0-9]', '', str(name).lower())


def _role(name):
    """'leq', 'lmax', 'lmin', None (not a level) or 'skip' (a level we must not use)."""
    k = _key(name)
    if not k.startswith('l') and 'db' not in k and 'level' not in k:
        return None
    # C/Z weighting, peaks, exposure levels and dosimeter averages. Lavg and TWA
    # use a 3, 4 or 5 dB exchange rate, so none of these is an A-weighted Leq.
    if (re.match(r'l[cz](eq|[fsi]?max|[fsi]?min|pk|peak|e)', k) or re.search(r'db[cz]$', k)
            or 'pk' in k or 'peak' in k or 'twa' in k or 'lavg' in k or 'dose' in k
            or re.match(r'(la?e|la?ex8?h?)(\d|db|$)', k) or k.startswith('sel')):
        return 'skip'
    # Leq first, so "LAeq,1min" is an Leq and not a minimum
    if re.match(r'la?eq', k) or 'leq' in k or 'laeq' in k:
        return 'leq'
    if re.match(r'la?[fsi]?max', k) or 'max' in k:
        return 'lmax'
    if re.match(r'la?[fsi]?min', k) or 'min' in k:
        return 'lmin'
    return None


def _pick(columns, role):
    """One column for a role. Fast time weighting wins for L-Max/L-Min (WHO LAmax is fast)."""
    found = [c for c in columns if _role(c) == role]
    if len(found) <= 1:
        return (found[0] if found else None), None
    for tag in ('f', '', 's', 'i'):
        exact = [c for c in found if _key(c).startswith(f'la{tag}{role[1:]}') or
                 (tag == '' and _key(c).startswith(role))]
        if len(exact) == 1:
            others = [c for c in found if c != exact[0]]
            return exact[0], others
    raise IngestError(
        f"More than one column could be the {role.upper()} level ({', '.join(map(str, found))}), "
        "so the file can't be read without guessing. Keep one of them and upload again.")


def normalise_levels(df):
    """Rename recognised level columns to the NSRT names and drop levels we must not use.

    Files already in NSRT form come back unchanged.
    """
    notes = []
    renames, alternates = {}, []
    for role, target in (('leq', LEQ), ('lmax', LMAX), ('lmin', LMIN)):
        col, unused = _pick(df.columns, role)
        if col is not None and _key(col) != _key(target):
            renames[col] = target
        alternates += unused or []
    other = [c for c in df.columns if _role(c) == 'skip']
    if not renames and not alternates and not other:
        return df

    out = df.drop(columns=alternates + other).rename(columns=renames)
    if renames:
        notes.append('Columns read as ' + ', '.join(f'{v.split(" dB")[0]} = "{k}"' for k, v in renames.items()) + '.')
    if alternates:
        notes.append('Also in the file but not used, because a fast-weighted column was preferred: '
                     + ', '.join(f'"{c}"' for c in alternates) + '.')
    if other:
        notes.append('Not used, because they are not A-weighted Leq, max or min levels '
                     '(C/Z weighting, peaks, dose or exposure values): ' + ', '.join(f'"{c}"' for c in other) + '.')
    out.attrs = dict(df.attrs)
    out.attrs['ingest_warnings'] = list(df.attrs.get('ingest_warnings', [])) + notes
    return out


def fix_decimal_commas(df):
    """Convert text columns written with a decimal comma ('45,6') to numbers."""
    for c in df.columns:
        s = df[c]
        if not (s.dtype == object or pd.api.types.is_string_dtype(s)):
            continue
        txt = s.dropna().astype(str).str.strip()
        if txt.empty:
            continue
        if txt.str.fullmatch(r'-?\d+,\d+').mean() >= 0.95:
            df[c] = pd.to_numeric(s.astype(str).str.strip().str.replace(',', '.', regex=False), errors='coerce')
    return df


# Larson Davis (G4 / LD Utility) workbook exports

def is_larson_davis_workbook(sheet_names):
    names = {str(s).strip().lower() for s in sheet_names}
    return 'time history' in names and 'summary' in names


def read_larson_davis(xl):
    """Time History sheet of a Larson Davis export, in the NSRT layout.

    Readings the instrument flagged as overloaded or invalid become missing data.
    """
    sheet = next(s for s in xl.sheet_names if str(s).strip().lower() == 'time history')
    raw = xl.parse(sheet)
    raw.columns = [str(c).strip() for c in raw.columns]
    if 'Date/Time' not in raw.columns:
        raise IngestError('This Larson Davis export has no Date/Time column in its Time History sheet.')

    # G4 writes the time as text with two spaces between date and time
    ts = raw['Date/Time']
    if ts.dtype == object or pd.api.types.is_string_dtype(ts):
        ts = ts.astype(str).str.split().str.join(' ')
    ts = pd.to_datetime(ts, errors='coerce', format='mixed')

    out = pd.DataFrame({TIME: ts})
    levels = normalise_levels(raw.drop(columns=['Date/Time']))
    for col in (LEQ, LMAX, LMIN):
        if col in levels.columns:
            out[col] = pd.to_numeric(levels[col], errors='coerce')
    if LEQ not in out.columns:
        raise IngestError('No LAeq column was found in the Time History sheet of this Larson Davis export.')

    notes = list(levels.attrs.get('ingest_warnings', []))
    flags = [c for c in ('Overload', 'Invalid') if c in raw.columns]
    if flags:
        bad = raw[flags].notna().any(axis=1) & raw[flags].astype(str).apply(
            lambda s: ~s.str.strip().str.lower().isin(('', 'nan', 'no', 'false', '0'))).any(axis=1)
        n_bad = int(bad.sum())
        if n_bad:
            out.loc[bad.to_numpy(), [c for c in (LEQ, LMAX, LMIN) if c in out.columns]] = np.nan
            notes.append(f'{n_bad:,} readings flagged {" or ".join(flags).lower()} by the instrument '
                         'were treated as missing.')

    out = out[out[TIME].notna()].reset_index(drop=True)
    model = str(xl.parse('Summary', header=None).iloc[0, 0]).replace('Summary', '').strip()
    out.attrs['source_format'] = f'Larson Davis {model}'.strip()
    out.attrs['ingest_warnings'] = notes
    return out


# Convergence Instruments NSRT_W .cil files
#
# Big-endian. Four length-prefixed strings (model, firmware, device id,
# calibration date), an int, then three blocks of the same shape:
# start (seconds since 1904-01-01 UTC), step (s), count, count doubles.
# The blocks are L-Max, LEQ and L-Min. Checked value for value against the
# .xls export of the same session.

_EPOCH_1904 = pd.Timestamp('1904-01-01')


def is_cil(head):
    return len(head) > 12 and head[4:8] == b'NSRT'


def read_cil(path, zone='America/New_York'):
    import struct
    with open(path, 'rb') as fh:
        buf = fh.read()
    try:
        pos, text = 0, []
        for _ in range(4):
            n = struct.unpack_from('>i', buf, pos)[0]
            text.append(buf[pos + 4:pos + 4 + n].decode('latin-1'))
            pos += 4 + n
        pos += 4
        blocks = []
        for _ in range(3):
            start, step = struct.unpack_from('>dd', buf, pos)
            count = struct.unpack_from('>i', buf, pos + 16)[0]
            pos += 20
            if count <= 0 or step <= 0 or pos + 8 * count > len(buf):
                raise IngestError('block header out of range')
            blocks.append((start, step, np.frombuffer(buf, '>f8', count, pos)))
            pos += 8 * count
    except (struct.error, IngestError) as exc:
        raise IngestError(f'This .cil file does not have the expected NSRT layout ({exc}).') from exc

    if len({(s, st, len(v)) for s, st, v in blocks}) != 1:
        raise IngestError('The three level blocks in this .cil file do not share one timeline.')
    start, step, _ = blocks[0]
    lmax, leq, lmin = (np.round(v.astype(float), 6) for _, _, v in blocks)
    if not (np.isfinite(leq).any() and np.nanmin(leq) > -10 and np.nanmax(leq) < 200):
        raise IngestError('The levels in this .cil file are outside any plausible range.')

    utc = _EPOCH_1904 + pd.to_timedelta(start + step * np.arange(len(leq)), unit='s')
    local = pd.DatetimeIndex(utc).tz_localize('UTC').tz_convert(zone).tz_localize(None)
    df = pd.DataFrame({'Time (Date hh:mm:ss.ms)': local, ' L-Max dB -A ': lmax,
                       ' LEQ dB -A ': leq, ' L-Min dB -A ': lmin})
    df.attrs['source_format'] = f'{text[0]} firmware {text[1]} (.cil)'
    df.attrs['ingest_warnings'] = [f'.cil times are stored in UTC and are shown here in {zone}, '
                                   'as in the NSRT software export.']
    return df
