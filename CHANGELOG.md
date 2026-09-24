# Changelog

## 2026-09-21 — External review: verified findings and fixes

Each finding from the external code review was checked against the code and, where it
concerns a standard, against the primary source. Findings that did not hold are listed
at the end.

### Standards and wording

- **Maryland COMAR citations updated.** Regulation 26.02.03.03 has been repealed. The
  maximum allowable noise levels are now in 26.02.03.02B(1), Table 1 (residential 65 dBA
  day, 55 dBA night; commercial 67/62; industrial 75/75), expressed as equivalent
  A-weighted sound levels (.02A(2)). Day and night hours are defined in .01B(4) and
  .01B(14). Checked against regs.maryland.gov on 21 Sep 2026. The former Ldn "state goal"
  row has been removed because the regulation no longer contains it.
- **COMAR exemptions and measurement conditions stated.** Motor vehicles on public roads,
  aircraft at licensed airports, railroads and residential air-conditioning are exempt
  (.02C). A compliance measurement is made at the receiving property line with a Type II
  or better meter (.02D). These conditions are now stated on the website, in the PDF/Word
  report and in the Standards reference.
- **Comparisons, not compliance verdicts.** A sound level meter records the combined
  sound of all sources, so a guideline row can say only whether the measured level is
  above the value or at or below it. PASS/FAIL, "compliant" and "non-compliant" have been
  replaced with ABOVE / AT OR BELOW in:
  - the website table, which is renamed from "Compliance matrix" to
    "Guideline comparison";
  - report Section 3, which is renamed to "Comparison with Health Guidelines and
    Regulatory Limits";
  - the Section 1 summary;
  - the results workbook, whose sheet is renamed to "Guideline comparison";
  - the API.

  Source-specific rows (aircraft, railway) and the indoor bedroom LAmax row remain marked
  as indicative.
- **Instrument details are no longer assumed.** Reports used to state an instrument model
  and calibration that were never supplied. They now state only what the user enters in
  the new Instrument field; otherwise the report says that none was provided.
- **Unverified health statements removed.** The following have been deleted:
  - the "85 dB — immediate permanent hearing damage risk" effect table and the screening
    recommendations in `iso_epa_standards.py`;
  - an unused occupational-standards table with unsourced health statements;
  - the unused `chart_generator.py`.

### Upload privacy and storage

- **Unguessable, collision-free upload names.** Uploaded and merged files used to be named
  by the upload time to the second plus the original filename. Anyone who guessed a name
  could open another user's upload, and two same-named uploads in the same second
  overwrote each other. Each file now gets 64 random bits in its name, known only to the
  browser that uploaded it.
- **Only names the server issued are accepted.** The server used to accept any path inside
  its upload and artifact folders.
- **Automatic deletion is complete and time-limited.** Uploads are deleted after two hours
  (`RETENTION_MAX_AGE_HOURS`), or sooner once 15 newer uploads arrive. Merged, Parquet and
  WLG files were previously never deleted.
- **Accurate upload page text.** The landing page used to say files "are not retained after
  the session", which was inaccurate. It now states the retention period and the 32 MB
  per-request limit of the hosted site.
- **Non-ASCII filenames keep their extension.** A file such as `测试.csv` used to lose its
  extension on upload and could not be read.

### Reliability

- **Valid JSON for NumPy and missing values.** NumPy integers, booleans and arrays, and
  pandas `NA` and `NaT`, used to make a response fail with a server error. They are now
  converted to valid JSON (`null` for missing values).
- **Build context restricted.** A `.dockerignore` allow-list was added, mirroring
  `.gcloudignore`, so local and Hugging Face builds include only the application.
- **Dependency ranges bounded.** Every dependency is now capped at the major version the
  platform was tested with. `backend/requirements.txt` now points at the root file; it
  lacked `pyarrow`, so Parquet uploads failed after `quickstart.sh`.
- **Percentile convention corrected.** `EnvironmentalMetricsCalculator` computed Lx as the
  x-th percentile rather than the level exceeded x % of the time. The website's
  percentiles were already correct and did not use this module.

### Tests

- Tests that returned `False` instead of failing now use assertions and read the sample
  file from a fixed path.
- The server-dependent performance script has been replaced by `tests/test_api.py`. It runs
  in-process and covers:
  - upload, analysis and the cached re-run;
  - PDF generation;
  - upload isolation;
  - comparison row semantics;
  - JSON validity.

### Findings that did not hold on re-check

- **Footer contrast.** Footer text measures 7.1:1 or higher against its background, which
  meets WCAG AA and AAA.
- **Already fixed.** These findings were fixed in the earlier audit commits:
  - filters carried through to the comparison refresh and health assessment;
  - interval LAeq used as the indoor LAmax;
  - contradictory concern levels;
  - chart defaults;
  - missing values shown as 0.00.

## Earlier audit commits

- `b97ebac`: time-zone conversion (logger clock → report zone through UTC,
  daylight-saving aware), exact period boundaries, accuracy fixes, removal of unused code.
- `0300f18`: PDF charts restored, result accuracy fixes, interface redesign.
