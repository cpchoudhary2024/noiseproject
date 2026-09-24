# Weather screening audit — 24 September 2026

## Scope and accuracy

This is a conservative remote-station screen, not an instrument correction or a certification of site weather. It removes observations; it does not subtract estimated weather noise, reconstruct removed measurements, or establish annual exposure. No 100% accuracy or nationwide availability claim is supported. Calibration, integration periods, microphone placement, windscreen condition, source representativeness and contemporaneous site weather require independent evidence.

The changes below preserve existing uncommitted project work. No deployment was performed.

## Corrections implemented

- Removed the inference that air temperatures above 5 °C prove absence of ground snow. Missing snow depth remains unverified, including in warm conditions; this can make an entire record unusable.
- Retain positive precipitation evidence despite temperature anomalies or disagreement between the same station's archives. Suspect reports can cause conservative false exclusions; disagreement does not prove dry conditions. Agreement between these streams is not independent validation.
- Check maximum observed station wind and gust against the configured limit without height reduction, in addition to the estimated block mean at microphone height. This conservative platform rule is distinct from a standard that specifies an averaged wind measurement. A logarithmic profile cannot guarantee local microphone wind.
- Apply GHCN quality flags, reject negative/missing snow measurements, and prevent trace snowfall from establishing a snow-free missing day. Cache names changed so old responses without quality flags are not reused.
- Use site civil dates for daily snow matching even when the logger records UTC or fixed standard time. Test Alaska, Hawaii, Arizona and Pacific time alongside DST cases.
- Bind screens to a hash of the full ordered timestamp sequence, including missing times, instead of only row count and endpoints. Screen version 2 invalidates earlier saved screens; users must rerun them.
- Include unreadable timestamps in exclusion totals. Validate slot/buffer and roughness configuration.
- Correct associated snow-station distance reporting and enforce the snow search radius for that candidate too.
- Apply selected filters to advanced-chart exports and all daily/hourly/weekly spreadsheet and CSV export routes. Refuse invalid screens rather than exporting unfiltered data.
- Label screened figures, show retention shares, preserve screening methods and limitations in PDF/Word, and add weather metadata sheets to spreadsheets. Plot screened timestamp observations as points rather than connecting across removed periods. Missing hours remain missing in rolling exceedance calculations.

## Interpretation and unresolved limitations

- Airport observations and daily snow stations may not represent a residence, mountain site, coastal site, or local shower. More stations alone do not guarantee improvement. The most useful further input is synchronized, quality-controlled on-site wind, precipitation and surface-condition observations.
- A known wind/precipitation observation in each five-minute slot establishes archive coverage, not continuous confirmation of conditions at the microphone. Events between observations can be missed.
- The default 25.4 mm snow threshold is an operational rule, not a universal acoustic validity threshold. Lesser snow depth can alter propagation. Snow selection does not model terrain or elevation differences.
- Fixed precipitation buffers cannot determine when a windscreen or pavement has dried. Wet pavement, temperature gradients, wind direction and propagation effects remain outside this screen.
- Nationwide station lookup is supported, but usable observations are not guaranteed for every US location/date. Missing coverage is excluded; synthetic weather is not substituted.
- Retention percentages count samples, not elapsed-time coverage. Results after screening describe the retained subset and may be selection-biased.
- Existing acoustic/correction regression tests were run, but these changes do not constitute independent metrological validation of every platform calculation, instrument, correction or report.

## Sources reviewed

- [FHWA Noise Measurement Handbook](https://www.fhwa.dot.gov/ENVIRonment/noise/measurement/handbook.cfm): site-specific contemporaneous wind, precipitation/wet-pavement restrictions, wind effects and measurement documentation.
- [FHWA construction noise measurement guidance](https://www.fhwa.dot.gov/ENVIRONMENT/noise/construction_noise/handbook/handbook05.cfm): snow and prevailing conditions depend on the assessment purpose.
- [NOAA NCEI data-service documentation](https://www.ncei.noaa.gov/access/search/documentation/data-service/): attribute retrieval and units.
- [NWS snowmelt technical memorandum](https://www.weather.gov/media/owp/oh/hdsc/docs/TM29.pdf): snowpack energy and melt; warm air alone is not proof of immediate disappearance.

Tests use deterministic weather fixtures, including failures and source flags. They verify implementation, not live archive completeness or nationwide field accuracy. PDF and Word weather sections are generated in regression tests; full visual page-by-page review of every report variant remains outside this audit.

## Validation result

`python3 -m pytest tests/test_weather_screen.py tests/test_dashboard_regressions.py tests/test_acoustics_domain.py tests/test_phase1.py -q`: **100 passed**. Includes a saved-screen integration test comparing dashboard analysis, CSV output and spreadsheet screening metadata. JavaScript syntax (`node --check frontend/static/js/app.js`) and `git diff --check` passed. All-rejected screens now display exclusion/retention summaries without offering an invalid analysis action.
