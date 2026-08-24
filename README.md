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

Assessment pipeline converting raw sound-level-meter exports into community and technical
noise-exposure reports evaluated against WHO, ISO, and jurisdictional criteria. In
continuous use on an ongoing residential monitoring study: **39.5 million 1-second
A-weighted measurements across four sites**, and growing.

**Live:** https://cpchoudhary2024-noise-analysis-platform.hf.space

---

## 1. Executive Summary & Problem Statement

### The engineering challenge

Environmental noise assessment fails quietly. The arithmetic is easy — an energy average is
four lines of NumPy — and that is exactly why errors survive to the conclusion: every
number looks plausible.

The failure that matters is **comparing a metric to a limit not defined on that metric.**
The WHO 53 dB guideline applies to L<sub>den</sub>: a duration-weighted 24-hour average
that adds +5 dB to evening and +10 dB to night samples. It says nothing about an individual
reading, an hourly average, or one day's average — yet all three are routinely compared
against it, in tables and in charts. The comparison always biases the same direction:
**toward understating exceedance.**

Three further failure modes recur in production noise work. Each is handled explicitly
here, and each was observed in real data from this study:

**Sample-density bias in rating levels.** L<sub>dn</sub> and L<sub>den</sub> are defined as
duration-weighted combinations of day, evening, and night periods. Weighting by sample
count instead makes the result depend on when the meter happened to be running. A record
starting mid-morning and ending mid-evening over-represents daytime and biases
L<sub>dn</sub> low — measured at up to 0.24 dB on partial-day datasets here.

**Metrics computed from incomplete periods.** A night-only record has no daytime samples.
Renormalising across only the periods present still produces an L<sub>den</sub> —
structurally just L<sub>night</sub> plus its penalty — which a report then declares "within
the WHO guideline of 53 dB". That is a false pass on a metric that could not be computed.
The daytime contribution is unknown, not zero, and it can only raise the result.

**Single-sample energy domination.** Because L<sub>Aeq</sub> is an energy average, a very
small number of very loud samples can dominate it entirely. In one record in this study, a
single one-second sample of 120 dB(A) out of 86,400 carried 98% of total acoustic energy
and moved L<sub>Aeq</sub> from 53.5 to 70.7 dB(A) — flipping the verdict from "low concern"
to "significantly exceeds WHO guidelines". The arithmetic was correct. The conclusion
rested on one unverified sample, and nothing in the report disclosed it.

### Technical objective

Build the assessment path so that each measured quantity can only be evaluated against a
criterion defined on that same quantity, and so that the platform declines to answer where
it cannot answer honestly.

### Where it declines to answer

Each of these replaced a confident output the platform could not support:

- **Unreadable timestamps** → every time-based result withheld, rather than dated to the
  day the report happened to run
- **A missing period** → no L<sub>dn</sub> or L<sub>den</sub>. A night-only record yields
  no 24-hour index rather than one that reads as passing
- **No L<sub>den</sub>** → no guideline verdict. L<sub>Aeq</sub> is never substituted; it
  omits the evening and night penalties and would pass records that fail
- **Source attribution** → never inferred from level data
- **Identifiers** → names, addresses, and filenames withheld from every output, including
  chart annotations rendered into images and Word document metadata. A SHA-256 of the
  source file preserves chain of custody without disclosing whose home produced it

The platform also volunteers what weakens its own conclusions: when few samples dominate
the energy average, the record's true coverage and gaps, that WHO guidelines are long-term
averages while any deployment is finite, and the 1–3 dB combined uncertainty ISO 1996-2
associates with environmental measurement.

### Compliance and risk-mitigation impact

Outputs support residential noise complaints, community monitoring programmes, and
technical assessment against local ordinances. Because these reports can enter regulatory
or legal processes, the design priority is that a stated exceedance withstands adversarial
review — including disclosure of the cases where the data does not support a finding.

---

## 2. Regulatory & Industry Standards Alignment

### ISO 1996 — Description, measurement and assessment of environmental noise

ISO 1996-1 and 1996-2 are **methodology** standards. They define how to describe, measure,
and assess environmental noise; they do not set universal legal limits.

This platform does not return "ISO compliance" without a named jurisdiction. Presenting a
non-authoritative value as an "ISO limit" misstates the standard, and the assessment layer
was changed to prevent it. ISO 1996-1 governs the energy-averaging and rating-level
definitions implemented here. ISO 1996-2 supplies the 1–3 dB combined measurement
uncertainty disclosed alongside results.

Also referenced: **ISO 3744** (sound power determination) and **ISO 3746** (survey-grade
methods for construction equipment).

### WHO Environmental Noise Guidelines for the European Region (2018)

Health-based reference values, applied by source with the metric each value is defined on:

| Source | L<sub>den</sub> | L<sub>night</sub> |
|---|---|---|
| Road traffic | 53 dB | 45 dB |
| Railway | 54 dB | 44 dB |
| Aircraft | 45 dB | 40 dB |
| Wind turbine | 45 dB | not specified |

Indoor and leisure references: bedroom ≤ 30 dB L<sub>Aeq</sub> with ≤ 45 dB L<sub>Amax</sub>
for single events; living room ≤ 35 dB L<sub>Aeq</sub>; classroom ≤ 35 dB L<sub>Aeq</sub>;
leisure noise ≤ 70 dB L<sub>Aeq,24h</sub> annual average.

Source-specific criteria are reported as **indicative only**: a source-blind meter cannot
attribute noise to a source.

### WHO Night Noise Guidelines for Europe (2009)

Applied for night-period health effect thresholds on L<sub>night</sub>.

Health-effect thresholds are attributed to WHO 2018 and WHO 1999/2009 with the governing
metric named. An earlier revision attributed these to "EPA guidelines"; that attribution
was incorrect and was corrected.

### EU Directive 2002/49/EC, Annex I

Defines L<sub>den</sub> as the duration-weighted combination of a 12-hour day, a 4-hour
evening with a +5 dB penalty, and an 8-hour night with a +10 dB penalty. Implemented
exactly as published.

### Maryland COMAR 26.02.03 — Environmental Noise Standards

Confirmed against the regulation text:

- **Table 2** (26.02.03.03), maximum allowable levels, dB(A):
  industrial 75 day / 75 night; commercial 67 day / 62 night;
  **residential 65 day / 55 night**
- **Day** is 07:00–22:00 and **night** is 22:00–07:00, per .01B(5) and .01B(15)
- COMAR's own L<sub>dn</sub> definition (.01B(4)) applies the +10 dB night penalty

Because COMAR's windows are 07:00–22:00 and 22:00–07:00, the platform's generic
`LAeq_day` and `LAeq_night` carry the **L<sub>dn</sub>** windows, not the L<sub>den</sub>
windows. An earlier revision aliased these to the L<sub>den</sub> windows (07:00–19:00 and
23:00–07:00), so reports printed "daytime levels (07:00–22:00) averaged X" where X was the
07:00–19:00 figure, and compared a 23:00–07:00 average against COMAR's 22:00–07:00 legal
limit. Both are now enforced by test.

### US EPA noise guidance

EPA environmental noise limits are **not** evaluated by default. The 1974 EPA "Levels
Document" is guidance identifying levels protective of public health and welfare; it is not
an enforceable federal limit, and the platform does not present it as one.

### What may be expressed as "percentage of time above a limit"

A percentage of time above a level describes the **distribution** of measured levels. It is
computable for any limit whose value lives on the same scale as a measured reading.

- **COMAR 65/55 dB(A) and WHO L<sub>night</sub> 45 dB(A)** qualify. Both are energy
  averages of measured levels, differing in window and value, not in kind. A share of time
  at or above each is meaningful, provided each uses its own window as the denominator.
- **WHO L<sub>den</sub> 53 dB(A) does not.** L<sub>den</sub> adds +5 dB to evening and
  +10 dB to night readings before averaging, so its value sits on a penalty-weighted scale
  that no measured reading occupies. "Time above 53 dB(A)" is a real statistic about the
  data, but it is unrelated to L<sub>den</sub> and is not presented beside it. For
  L<sub>den</sub> the answerable question is how many individual days exceeded the guideline.

In every case the share of time is a description of exposure, never a compliance verdict:
these limits are assessed on a period average, and a period can pass on its average while
spending real time above the level.

---

## 3. Technical Methodology & Mathematical Framework

### Pipeline

```
Instrument export (.wlg binary, .xlsx, .xls, .csv, .parquet)
        ↓
Byte-signature format detection  →  timestamp reconstruction  →  truncation check
        ↓
Energy-domain aggregation  →  windowed LAeq  →  Lx percentiles
        ↓
Rating levels (Ldn / Lden / Lnight)  ·  exceedance statistics
        ↓
QA/QC: energy-concentration diagnostic, gap inventory, coverage scoring
        ↓
Compliance matrix (jurisdiction-named)  →  PDF / HTML / DOCX deliverables
```

### Ingestion

Formats are identified by **byte signature rather than file extension**, because logger
exports are routinely mislabelled. Timestamps are recovered from year-first, day-first, and
epoch formats, and absolute time is reconstructed where an export preserved only a
minute-of-hour clock.

The parser detects **silent truncation at spreadsheet row ceilings**, which had been
removing days of monitoring from source files without warning.

Larson Davis `.wlg` binary files are parsed directly. Large records convert to Parquet for
columnar access; multi-million-row series use vectorised NumPy boolean masks for hour-range
membership rather than per-row mapping.

### Energy averaging (L<sub>Aeq</sub>)

```
LAeq = 10 · log₁₀( (1/N) · Σ 10^(Lᵢ/10) )        [dB(A)]
```

Non-finite samples (dropouts) are excluded, not propagated. An all-invalid window returns
no value rather than 0 dB(A).

Doubling acoustic energy raises the level by 10·log₁₀(2) = 3.0103 dB. By Jensen's
inequality applied to the convex map L → 10^(L/10), the energy mean strictly exceeds the
arithmetic mean for any non-constant series. For a record alternating between 60 and
80 dB(A): arithmetic mean 70.00 dB(A), energy mean **77.03 dB(A)**.

### Exceedance percentiles

L<sub>x</sub> is the level exceeded x% of the time — the (100 − x)th ascending percentile:

```
L5 = P95 ·  L10 = P90 ·  L50 = P50 ·  L90 = P10 ·  L95 = P05
```

### Rating levels — duration-weighted

L<sub>dn</sub>, 15-hour day (07:00–22:00) and 9-hour night (22:00–07:00), +10 dB night
penalty:

```
Ldn = 10 · log₁₀( [15 · 10^(Lday/10) + 9 · 10^((Lnight+10)/10)] / 24 )
```

L<sub>den</sub>, per EU 2002/49/EC Annex I — 12-hour day, 4-hour evening (+5 dB), 8-hour
night (+10 dB):

```
Lden = 10 · log₁₀( [12·10^(Lday/10) + 4·10^((Lev+5)/10) + 8·10^((Lnight+10)/10)] / 24 )
```

Weights are **window durations in hours**, not sample counts. Night windows wrap midnight.

For a flat 60 dB(A) 24-hour record: **L<sub>dn</sub> = 66.4098 dB(A)** and
**L<sub>den</sub> = 66.3952 dB(A)**. L<sub>den</sub> sits marginally below L<sub>dn</sub>
because it penalises 8 night hours at +10 dB where L<sub>dn</sub> penalises 9.

If any constituent period has no data, the rating level returns **no value**.

### QA/QC — energy-concentration diagnostic

The platform reports how much of the total energy sits in the loudest samples:

- `top1_energy_pct` — energy share of the single loudest sample
- `top01pct_energy_pct` — share of the loudest 0.1%
- `laeq_excluding_top01pct` — L<sub>Aeq</sub> with that tail removed
- `laeq_minus_l5` — the diagnostic

Since L5 is exceeded 5% of the time, an energy mean **above** L5 means the average is being
carried by the top few percent of samples. A record is flagged as dominated when the single
loudest sample carries ≥ 10% of total energy, or when L<sub>Aeq</sub> − L5 > 1.0 dB.

This is also the signature of a sensor artefact: a handling knock, a dropped microphone, or
clipping produces exactly one enormous reading. Whether an event is genuine or spurious
cannot be settled from level data alone, so the platform's obligation is to surface the
dependency, not resolve it silently.

### Model limitations and physical assumptions

- **Samples are assumed equally weighted in time.** Variable integration times are not
  duration-weighted within a window.
- **A-weighting is assumed applied at the instrument.** The platform applies no frequency
  weighting of its own.
- **No source apportionment.** Source-specific criteria are indicative only.
- **No meteorological correction.** ISO 1996-2 propagation adjustments for wind and
  temperature gradients are out of scope.
- **Calibration drift is not modelled.** Field calibration records are held by the operator.
- **WHO guidelines are long-term averages**; any deployment is finite.
- **Combined measurement uncertainty of 1–3 dB** (ISO 1996-2) applies to all results.
- **Percent-of-time-above statistics describe exposure, not compliance.**

### Verification

The acoustic core is checked against an **independent reimplementation** written from the
published metric definitions, sharing no code with the platform:

| Check | Result |
|---|---|
| L<sub>Aeq</sub>, percentiles, all windows, L<sub>dn</sub>, L<sub>den</sub> vs independent reference | **0.000 dB** |
| Cross-surface agreement — analyzer, API, compliance matrix, narrative prose, CSV export | **75 / 75** |
| Adversarial shapes — multi-gap, extreme peak, constant, night-only, 1-min cadence, unsorted, DST | **12 / 12** |
| API and report generation across 16 datasets | **288 calls, 0 failures** |
| 18-file merge, 10,703,625 rows | **exact, 0 duplicates** |
| Parquet vs XLSX, same record | **bit-identical** |

The cross-surface check exists because a number being correct in one place does not mean
the prose beside it says the same thing. It extracts figures from generated sentences and
compares them against the API and against ground truth.

`tests/test_acoustics_domain.py` adds **21 passing unit tests** validating the domain math
against hand-computed values and published definitions rather than against current output:

- Energy averaging: constant-signal identity, the 3.01 dB doubling rule, strict inequality
  against the arithmetic mean
- Non-finite exclusion; `None` on all-invalid input
- L<sub>x</sub> complement-percentile mapping and monotonic ordering
- L<sub>dn</sub> = 66.4098 dB(A) and L<sub>den</sub> = 66.3952 dB(A) at flat 60 dB(A)
- Exactly +10 dB night penalty
- **Duration weighting under 10× daytime oversampling** — the sample-density bias guard
- `None` when any period is missing
- Generic `LAeq_day`/`LAeq_night` carry L<sub>dn</sub> windows — the COMAR aliasing guard
- Midnight wrap of the night window
- Energy domination flagged on a single 120 dB(A) spike; not flagged on a steady record

---

## 4. Data Schema & Engineering Units

### Accepted inputs

| Format | Source | Notes |
|---|---|---|
| `.wlg` | Larson Davis sound level meters | Proprietary binary, parsed directly |
| `.xlsx` / `.xls` | Instrument exports | Row-ceiling truncation detection |
| `.csv` | Generic logger exports | Timestamp + level columns |
| `.parquet` | Converted archives | ~7× faster reads, half the size |

Any table with at least one dB column is accepted. Column names are matched on acoustic
tokens (`leq`, `laeq`, `lmax`, `lmin`, `db`, `spl`). A timestamp column enables the
time-based metrics; without one, L<sub>Aeq</sub> and the percentile profile are still
reported and everything time-dependent is withheld.

```
Time (Date hh:mm:ss.ms), L-Max dB -A , LEQ dB -A , L-Min dB -A
2026/04/12 20:04:31.000, 48.545, 48.345, 48.145
```

### Variables and units

| Variable | Definition | Units |
|---|---|---|
| `LAeq` | Energy-average A-weighted level over a window | dB(A) |
| `LAeq_24h` | Energy average over the full record | dB(A) |
| `LAeq_day` | Day average, 07:00–22:00 (L<sub>dn</sub> window) | dB(A) |
| `LAeq_night` | Night average, 22:00–07:00 (L<sub>dn</sub> window) | dB(A) |
| `LAeq_day_lden` | Day average, 07:00–19:00 | dB(A) |
| `LAeq_evening_lden` | Evening average, 19:00–23:00 | dB(A) |
| `LAeq_night_lden` / `Lnight` | Night average, 23:00–07:00 | dB(A) |
| `Ldn` | Day–night level, +10 dB night penalty | dB(A) |
| `Lden` | Day–evening–night level, +5/+10 dB penalties | dB(A) |
| `LAmax` / `LAmin` | Maximum / minimum A-weighted level | dB(A) |
| `L5`–`L95` | Level exceeded x% of the time | dB(A) |
| `top1_energy_pct` | Energy share of the loudest single sample | % |
| `top01pct_energy_pct` | Energy share of the loudest 0.1% | % |
| `laeq_minus_l5` | Energy-domination diagnostic | dB |
| `dominated` | Artefact / tail-domination flag | boolean |
| `time_above_level_pct` | Share of samples at or above a threshold | % |
| `coverage_score` | Actual ÷ expected records for the period | % |
| `sampling_interval` | Median positive timestamp gap | seconds |
| `source_sha256` | Chain-of-custody hash of the source file | hex digest |

### Reference values applied

| Criterion | Value | Metric | Source |
|---|---|---|---|
| Road traffic | 53 / 45 | L<sub>den</sub> / L<sub>night</sub> | WHO 2018 |
| Railway | 54 / 44 | L<sub>den</sub> / L<sub>night</sub> | WHO 2018 |
| Aircraft | 45 / 40 | L<sub>den</sub> / L<sub>night</sub> | WHO 2018 |
| Bedroom | 30 / 45 | L<sub>Aeq</sub> / L<sub>Amax</sub> | WHO 1999, 2018 |
| Living room | 35 | L<sub>Aeq</sub> | WHO 1999 |
| Night noise | L<sub>night</sub> thresholds | L<sub>night</sub> | WHO NNGL 2009 |
| MD residential | 65 / 55 | dB(A) day / night | COMAR 26.02.03.03 Table 2 |
| MD commercial | 67 / 62 | dB(A) day / night | COMAR 26.02.03.03 Table 2 |
| MD industrial | 75 / 75 | dB(A) day / night | COMAR 26.02.03.03 Table 2 |

Reference: 0 dB(A) corresponds to a sound pressure of 20 µPa.

---

## 5. Verification & Reproduction Instructions

### Requirements

Python 3.11 or later.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

pip install -r requirements.txt
pip install -r backend/requirements-dev.txt
```

### Run the domain test suite

```bash
pytest tests/test_acoustics_domain.py -v
```

Expected: **21 passing**.

Full suite:

```bash
pytest tests/ -q
```

The domain suite is dependency-light (NumPy, pandas, pytest) and runs without the Flask
application, instrument files, or study data.

### Run the application

```bash
pip install -r requirements.txt
python backend/app.py                  # http://127.0.0.1:5001

python tools/make_demo_dataset.py      # synthetic record to try it with
python tools/convert_to_parquet.py DIR # ~7x faster reads, half the size
```

### API surface

| Endpoint | Method | Purpose |
|---|---|---|
| `/api/upload` | POST | Single-file ingest |
| `/api/upload-multi` | POST | Multi-site ingest |
| `/api/analyze` | POST | Run the analysis pipeline |
| `/api/compliance-check` | POST | Jurisdictional compliance matrix |
| `/api/health-assessment` | POST | WHO-referenced health assessment |
| `/api/standards-reference` | GET | Applied criteria and citations |
| `/api/generate-report` | POST | Render PDF / HTML / DOCX deliverables |
| `/api/export-daily-summary` | POST | Daily aggregate export |

### Container

```bash
docker build -t noise-analysis-platform .
docker run -p 7860:7860 noise-analysis-platform
```

### Data handling

No measurement data is included in this repository. Study data under `data/`, generated
deliverables under `reports/` and `artifacts/`, and uploads are excluded from version
control. Raw logger directories and any report containing resident identifiers are covered
by an explicit exclusion block and are not published.

---

## Repository layout

```
backend/analysis/       acoustics, compliance matrix, standards reference, parsers
backend/app.py          Flask API
frontend/               interface templates and static assets
tools/                  analysis CLI utilities (Leq, exceedance, day/night, reporting)
tests/                  domain math and integration tests
docs/                   methodology and structure notes
```

## Stack

Python · Flask · pandas · NumPy · SciPy · Plotly · ReportLab · python-docx · PyArrow ·
Docker · Hugging Face Spaces

Every sentence in a generated report is templated from a computed value. There is no
language model in the reporting path.

## Disclaimer

Analyses are performed against published guideline and regulatory values. This platform is
not a substitute for a certified acoustic survey; instrument calibration records are held
by the operator. For formal compliance determinations, consult a certified acoustic
engineer.

Developed for a community noise study at the Johns Hopkins Bloomberg School of Public
Health. PI: Dr. Ana María Rule.
