# Website accuracy audit — work in progress

Started 2026-09-21; paused for separate Covidence setup request 2026-09-22.
Scope: website code, synthetic test inputs and newly generated synthetic reports only.
No deployment. Existing research files and reports were not audited or modified.
Pre-existing working changes preserved; baseline diff saved at /private/tmp/noise-audit-preexisting.patch.

## Implemented so far
- Session-bound, signed upload references and collision-resistant upload/report names.
- Strict JSON serialization: NumPy scalars converted; missing/nonfinite values become null.
- Filter propagation added to cached-frame assessment/chart routes; filtered report summary cache bypass.
- Shared metric selection for initial/refreshed comparison matrices; L-Max never replaced by LEQ maxima.
- Current Maryland reference: COMAR 26.02.03.02B(1), Table 1; removed historical residential Ldn goal.
- Source/period/placement-qualified indicative comparisons instead of unsupported legal pass/fail verdicts.
- Shared monitoring concern categories for health cards and narrative; explicitly application-defined, not clinical risk grades.
- Removed inferred instrument/calibration claims from technical and resident reports.
- Removed percentile guideline badges and misleading advanced-chart zoning defaults.
- Missing-value-safe browser number conversion; report comparison data signed server-side.
- Added HTML rendering from the common technical report story (not yet validated).
- Added application-only Docker build-context allowlist; direct dependencies pinned.
- Added synthetic regression tests and repaired tests that returned False without failing pytest.

## Verification completed before pause
- 71 pytest tests passed in 1.40 seconds.
- Browser JS syntax check passed.
- Synthetic tests reproduce and verify filtering, peak selection, strict JSON, missing-period withholding,
  upload isolation, current indicative criteria, comparison payload integrity, and cache isolation.
- Temporary environment /private/tmp/noise-audit-venv has pinned dependencies installed.

## Authoritative sources checked
- Maryland current limits: https://regs.maryland.gov/us/md/exec/comar/26.02.03.02
- Maryland current definitions: https://regs.maryland.gov/us/md/exec/comar/26.02.03.01
- WHO 2018: https://www.who.int/publications/i/item/9789289053563
- WHO 2009 target: https://www.who.int/europe/publications/i/item/9789289041737
- WHO 1999: https://www.who.int/publications/i/item/a68672
- NIOSH exposure duration: https://www.cdc.gov/niosh/noise/about/noise.html
- EPA retained authority: https://www.epa.gov/history/epa-history-noise-and-noise-control-act

## Outstanding — not production approved
- Finish filter propagation on exports reading input directly; add regression tests for all export formats.
- Validate technical/resident PDF and Word plus new common-story HTML after changes.
- Re-run browser interactions, charts and responsive layout on changed code.
- Finish stale explanatory copy in dashboard, educational API and chart captions.
- Finish requirements.lock and update Docker for Kaleido 1.x/Chromium; container currently unverified.
- Verify retention filename patterns, request validation, supported formats and concurrency.
- Verify remaining numerical/coverage/metric edge cases and record final test evidence.
