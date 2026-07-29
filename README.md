---
title: Noise Analysis Platform
emoji: 📊
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# Environmental Noise Analysis Platform

**Live:** https://cpchoudhary2024-noise-analysis-platform.hf.space

Turns raw sound-level-meter exports into community and technical noise-exposure
reports assessed against WHO, ISO and local regulatory criteria. In continuous
use on an ongoing community monitoring study — **39.5 million 1-second
A-weighted measurements across four residential sites to date, and growing.**

---

## The problem

Environmental noise assessment fails quietly. The arithmetic is easy — an energy
average is four lines of NumPy — and that is exactly why errors survive: every
number looks plausible.

The failure that matters is **comparing a metric to a limit not defined on that
metric.** WHO's 53 dB guideline applies to L<sub>den</sub>: a duration-weighted
24-hour average that adds +5 dB to evening and +10 dB to night samples. It says
nothing about an individual reading, an hourly average, or one day's average —
yet all three get compared against it, in tables and in charts. The comparison
always biases the same way: **toward understating exceedance.**

This platform is built so each measured quantity can only be evaluated against a
criterion defined on that same quantity, and so that it declines to answer where
it cannot answer honestly.

## Capabilities

**Ingestion** — Reads CSV, XLSX, XLS, Parquet and Larson Davis WLG by byte
signature rather than file extension, because logger exports are routinely
mislabelled. Recovers timestamps from year-first, day-first and epoch formats,
and reconstructs absolute time where an export preserved only a minute-of-hour
clock. Detects silent truncation at spreadsheet row ceilings, which had been
removing days of monitoring from source files without warning.

**Metrics** — L<sub>Aeq</sub>, L<sub>10</sub>/L<sub>50</sub>/L<sub>90</sub>,
L<sub>dn</sub>, L<sub>den</sub>, L<sub>night</sub>, day and night period
averages, L<sub>Amax</sub>, with interval-aware coverage and a gap inventory.
L<sub>dn</sub> and L<sub>den</sub> are duration-weighted per ISO 1996-1 and
EU 2002/49/EC rather than sample-weighted, so results do not depend on how many
samples happened to fall in each period.

**Assessment** — WHO 2018 (road, rail, aircraft, indoor), WHO Night Noise
Guidelines 2009, and Maryland COMAR 26.02.03.02, each evaluated on its own
metric and averaging window. Source-specific criteria are reported as indicative
only: a source-blind meter cannot attribute noise to a source.

**Scale** — Merges arbitrary numbers of weekly exports into a single timeline
with gap accounting; 18 files and 10.7M rows merge with zero loss and no
duplicates. Multi-site comparison across up to six locations.

**Reporting** — Technical PDF, plain-language community HTML, and Word. Every
sentence is templated from a computed value; there is no language model in the
reporting path.

## Where it declines to answer

Each of these replaced a confident output the platform could not support:

- **Unreadable timestamps** → every time-based result withheld, rather than
  dated to the day the report happened to run
- **A missing period** → no L<sub>dn</sub> or L<sub>den</sub>. A night-only
  record yields no 24-hour index rather than one that reads as passing
- **No L<sub>den</sub>** → no guideline verdict. L<sub>Aeq</sub> is never
  substituted; it omits the evening and night penalties and would pass records
  that fail
- **Source attribution** → never inferred from level data
- **Identifiers** → names, addresses and filenames withheld from every output,
  including chart annotations rendered into images and Word document metadata.
  A SHA-256 of the source file preserves chain of custody without disclosing
  whose home produced it

It also volunteers what weakens its own conclusions: when few samples dominate
the energy average, the record's true coverage and gaps, that WHO guidelines are
long-term averages while any deployment is finite, and the 1–3 dB combined
uncertainty ISO 1996-2 associates with environmental measurement.

## Verification

The acoustic core is checked against an independent reimplementation written
from the published metric definitions, sharing no code with the platform.

| check | result |
|---|---|
| L<sub>Aeq</sub>, percentiles, all windows, L<sub>dn</sub>, L<sub>den</sub> vs independent reference | **0.000 dB** |
| Cross-surface agreement — analyzer, API, compliance matrix, narrative prose, CSV export | **75 / 75** |
| Adversarial shapes — multi-gap, extreme peak, constant, night-only, 1-min cadence, unsorted, DST | **12 / 12** |
| API and report generation across 16 datasets | **288 calls, 0 failures** |
| 18-file merge, 10,703,625 rows | **exact, 0 duplicates** |
| Parquet vs XLSX, same record | **bit-identical** |

The cross-surface check exists because a number being correct in one place does
not mean the prose beside it says the same thing. It extracts figures from the
generated sentences and compares them against the API and against ground truth.

## Running it

```bash
pip install -r requirements.txt
python backend/app.py                  # http://127.0.0.1:5001

python tools/make_demo_dataset.py      # synthetic record to try it with
python tools/convert_to_parquet.py DIR # ~7x faster reads, half the size
```

## Data format

Any table with at least one dB column. Names are matched on acoustic tokens
(`leq`, `laeq`, `lmax`, `lmin`, `db`, `spl`). A timestamp column enables the
time-based metrics; without one, L<sub>Aeq</sub> and the percentile profile are
still reported and everything time-dependent is withheld.

```
Time (Date hh:mm:ss.ms), L-Max dB -A , LEQ dB -A , L-Min dB -A
2026/04/12 20:04:31.000, 48.545, 48.345, 48.145
```

## Stack

Python · Flask · pandas · NumPy · SciPy · Plotly · ReportLab · python-docx ·
Docker · Hugging Face Spaces

## Disclaimer

Analyses against published guideline and regulatory values. Not a substitute for
a certified acoustic survey; instrument calibration records are held by the
operator. For formal compliance determinations, consult a certified acoustic
engineer.

Developed for a community noise study at the Johns Hopkins Bloomberg School of
Public Health. PI: Dr. Ana María Rule. No measurement data is included in this
repository.
