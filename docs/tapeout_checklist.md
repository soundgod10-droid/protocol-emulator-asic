# Tape-out checklist

Items to do or check before the final (tape-out) gds run and the submission. Each item gives its reason.

## Flow configuration (src/config.json)

- [ ] Turn Magic DRC on: remove the line `"RUN_MAGIC_DRC": 0` (or set it to 1). Development runs leave it off to save run time; the precheck job runs only the KLayout SG13CMOS5L deck, so the final run must also give a Magic DRC result (`magic__drc_error__count` = 0 in `final/metrics.csv`). Measured with this configuration and Magic DRC on (gds 41380161509): gds job 1 h 47 min (the Magic DRC step 41 min 56 s, `drc.magic.rpt` COUNT 0), every other `final/metrics.csv` row equal to the development run gds 40955889046.
- [ ] Keep `"PL_RESIZER_SETUP_SLACK_MARGIN": 0` and `"OPENROAD_THREADS": 4`. With the default margin (0.05 ns) the post-CTS resizer loops for hours on the latch endpoints (borrow slack 0.000) and the gds job no longer finishes inside the 6 h GitHub job limit (gds 40846505319 cancelled at 6 h 01 min). Margin 0 gives the same final metrics as the default flow (gds 40955756051 against 40793488418).
- [ ] Confirm with Tiny Tapeout that a submission hardened with these flow variables is accepted. No Tiny Tapeout document found says it (the shuttle tool can re-harden a project with its own golden configuration: tt-support-tools configure.py `--harden`).

## Reading the result

- [ ] Read the timing from `55-openroad-stapostpnr/summary.rpt` at all three corners, not from the job status: the flow fails only on violations at the typical corner (`TIMING_VIOLATION_CORNERS` `*typ*`).
- [ ] Read the slow-corner flip-flop setup slack from the first path (group `flops`) of `55-openroad-stapostpnr/nom_slow_1p08V_125C/checks.rpt` (constraints in `src/signoff.sdc`).
- [ ] Check `final/metrics.csv`: route DRC 0, antenna violations 0, LVS errors 0, Magic DRC errors 0, max slew violations 0.
- [ ] Precheck: all checks pass (`precheck_reports/results.md`); gl_test passes.
