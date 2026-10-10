# Noise Analysis Platform

A Flask web app that turns sound level meter exports into noise assessments:
energy-averaged levels, exceedance levels, day and night rating levels, and
comparisons with WHO guidelines and Maryland (COMAR) limits.

Live: https://noise-analysis-platform-ga5lsxtezq-uc.a.run.app/

## What it does

- Reads NSRT_W exports (`.csv`, `.xls`) and the logger's own `.cil` files,
  Larson Davis G4 exports (`.xlsx`), and generic CSV, Excel or Parquet files.
- Converts the logger clock to the report clock through UTC, including
  daylight saving changes.
- Computes LAeq, L10/L50/L90, Ldn, Lden and Lnight.
- Compares each metric only with a limit defined on that same metric.
- Can remove rain, snow, thunder and windy periods using NOAA weather records.
- Gives a dashboard, PDF / Word / HTML reports, CSV exports and a comparison
  of several locations.

## Running it

    ./quickstart.sh

or by hand:

    python3 -m venv .venv
    source .venv/bin/activate
    pip install -r requirements.txt
    python backend/app.py

The app listens on http://127.0.0.1:5001. With Docker:

    docker build -t noise-analysis-platform .
    docker run -p 7860:7860 noise-analysis-platform

`python tools/make_demo_dataset.py` writes a synthetic record to try it with.
Deploying to Cloud Run is described in `deploy/README.md`.

## Example data

`examples/demo-site-4-days.parquet` is a synthetic 4 day, 1 second record with a
2 hour gap built in. It is not measured data from anywhere. Upload it to try the
app, or compare your output with `examples/sample-report.pdf`, which the app
produced from it. `tools/make_demo_dataset.py` regenerates the record (and a CSV
version) from a fixed seed.

## Input

A file needs an A-weighted Leq column and, for anything that depends on time of
day, a time column. Column order doesn't matter and common names are recognised
(`LEQ dB -A`, `LAeq`, `Leq (dBA)`, `LAFmax`, `Lmin (dBA)` and so on). Semicolon
files with decimal commas work too.

- C- and Z-weighted levels, peaks (`LApk`, `LCpk`), exposure levels and
  dosimeter averages (`Lavg`, `TWA`) are never used as A-weighted levels.
- When a file has both fast and slow maxima, LAFmax is used (WHO LAmax is fast).
- If two columns could both be the Leq, the file is refused rather than guessed.
- `.cil` times are stored in UTC and are shown in US Eastern time, as the NSRT
  software export does. A `.cil` and its `.xls` export give identical results.
- Larson Davis readings flagged overloaded or invalid are treated as missing.
- `.wlg` files are refused for now: the format is undocumented and no decoder
  has been checked against an export. Upload the `.cil` or the `.xls` instead.

Anything the reader renames, sets aside or can't read (unused columns, flagged
readings, unreadable times, overlapping readings when files are merged) is
listed under "Notes on this file" on the results page.

## Metric definitions

- LAeq is the energy average, `10 * log10(mean(10 ** (L / 10)))`. It is not the
  arithmetic mean of the dB values.
- Lx is the level exceeded x% of the time, so L90 is the 10th percentile.
- Ldn is a 15 h day (07:00-22:00) and a 9 h night (22:00-07:00) with +10 dB at
  night. Lden is 12 h day, 4 h evening (+5 dB, 19:00-23:00) and 8 h night
  (+10 dB, 23:00-07:00). Both weight each period by its hours, not by its
  number of samples. If a period has no data, no Ldn or Lden is reported.

WHO's 53 dB (Lden) and 45 dB (Lnight) are annual averages for road traffic, so a
short record can only be indicative. The Maryland limits are residential
65 dB(A) by day and 55 dB(A) at night, from COMAR 26.02.03.02B(1) Table 1. COMAR
does not state an averaging time; comparing a period LAeq with the table is this
tool's own reading. ISO 1996 sets no limits and OSHA/NIOSH limits are for
workplaces, so neither is used. The sources are listed at the top of
`backend/analysis/compliance_matrix.py`.

## Weather screening

The record is cut into 15 minute periods. A period is removed for precipitation
or thunder, mean wind above 5 m/s at microphone height, or snow on the ground of
1 inch or more (plus the day on either side). The periods before and after rain
go too. A period whose weather cannot be confirmed is removed rather than kept.

Observations come from the nearest NOAA/FAA airport station (1-minute and
5-minute reports) and from GHCN-Daily for snow depth, so the feature needs an
internet connection. A station several kilometres away can miss a local shower.
Wind at the station (10 m) is converted to microphone height with a log wind
profile, which is an estimate. Each screen can be downloaded as a CSV, period by
period.

## Limitations

- This is not a certified measurement. Instrument class and calibration are
  yours to check.
- A sound level meter cannot say what produced a sound.
- Reports quote a combined uncertainty of roughly 1-3 dB as an order of
  magnitude. The app does not compute an uncertainty budget.
- A short US-style `MM/DD/YYYY` export where every day is 12 or less is read
  day-first. Year-first dates, as the loggers write them, are unaffected.

## Tests

    pip install -r backend/requirements-dev.txt
    pytest

A few tests are marked as expected failures. Each one records an open question
about behaviour and says so in its reason.

## Layout

    backend/app.py        Flask routes
    backend/analysis/     metrics, clock handling, compliance matrix, report content
    backend/services/     NOAA weather retrieval and screening
    frontend/             page, script and styles
    tools/                demo data generator, Parquet converter
    deploy/               Cloud Run script
    tests/

No measurement data is stored in this repository.

Copyright (c) 2026 Chandra Prakash Choudhary. All rights reserved.
