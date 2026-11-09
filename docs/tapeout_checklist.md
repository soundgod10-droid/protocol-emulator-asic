# Tape-out checklist

Items to do or check before the final (tape-out) gds run and the submission. Each item gives its reason. Decisions D8, D9, D11 and D13 are in the design notes of the project (DECISIONS.md).

## 1 Exit criteria for tape-out

The exit criteria of the architecture note (A1 8.3) with the 5 additions of design note R3 (row S7), without the FPGA line L5, and with the SDF job (decision D13). L5 is removed: there is no FPGA board, and the design does not fit the only FPGA of the template workflow (iCE40UP5K, 154 % of the logic cells, run 41817455061).

| No. | Criterion | Source | How to check |
|---|---|---|---|
| E1 | All L2, L3 and L4 tests pass on the final RTL | A1 8.3 | test job (cocotb: test, test_isa, test_core, test_fw_full) and the formal jobs in formal/ (core, core_cover, host_port, host_port_cover, host_port_mut_id, mem, mem_cover, mem_small, mem_full, mem_full_reset, mem_reset, timed_out, timed_out_cover) run on the tape-out commit |
| E2 | The template gate-level test passes | A1 8.3 | gl_test job of the submitted run |
| E3 | STA has no violation at the three flow corners | A1 8.3, D9 | section 5: summary.rpt and checks.rpt of all corners |
| E4 | Precheck passes, 9 of 9 checks | R3 S7 addition 1 | precheck job, precheck_reports/results.md |
| E5 | Magic DRC 0 in the submitted run itself | R3 S7 addition 2, D11 | `magic__drc_error__count` = 0 in final/metrics.csv of the submitted run (section 2) |
| E6 | The submitted layout is one named GitHub Actions run, on the tape-out commit | R3 S7 addition 3, D11, R3 T1 | the run id in the submission (commit_id.json workflow_url) is the tape-out run of section 4 |
| E7 | The newest commits of the template, the tools and the GDS action are compared with the last check | R3 S7 addition 4, D11, R3 T6 | section 3 |
| E8 | Max fanout "violations" are only the known clock-leaf set, and max slew is 0 | R3 S7 addition 5 | section 5 |
| E9 | The SDF gate-level job passes at the three corners on the final gds run | D13, R3 S4 | section 4 step 3 |

## 2 Flow configuration (src/config.json)

- [ ] Turn Magic DRC on in the tape-out commit itself, on the submitted branch, not in a side branch: remove the line `"RUN_MAGIC_DRC": 0` (or set it to 1). The shuttle takes the layout of one named run of the project's GDS action (decision D11; Tiny Tapeout FAQ, read 2026-11-07: the GDS action artifact is "the final GDS file that will be added to the Tiny Tapeout submission", and "A project can't be submitted to a shuttle if its GDS action is failing"; the shuttle gds.yaml files have no --harden step, design note R3 2a). So the run with Magic DRC on must be the submitted run. Development runs leave it off to save run time; the precheck job runs only the KLayout SG13CMOS5L deck, so the final run must also give a Magic DRC result (`magic__drc_error__count` = 0 in `final/metrics.csv`). Measured with this configuration and Magic DRC on (gds 41380161509): gds job 1 h 47 min (the Magic DRC step 41 min 56 s, `drc.magic.rpt` COUNT 0), every other `final/metrics.csv` row equal to the development run gds 40955889046.
- [ ] Keep `"PL_RESIZER_SETUP_SLACK_MARGIN": 0` and `"OPENROAD_THREADS": 4`. With the default margin (0.05 ns) the post-CTS resizer loops for hours on the latch endpoints (borrow slack 0.000) and the gds job no longer finishes inside the 6 h GitHub job limit (gds 40846505319 cancelled at 6 h 01 min). Margin 0 gives the same final metrics as the default flow (gds 40955756051 against 40793488418).
- [ ] Confirm with Tiny Tapeout that a submission hardened with these flow variables is accepted. No Tiny Tapeout document found says it (the shuttle tool can re-harden a project with its own golden configuration: tt-support-tools configure.py `--harden`).

## Reading the result

- [ ] Read the timing from `55-openroad-stapostpnr/summary.rpt` at all three corners, not from the job status: the flow fails only on violations at the typical corner (`TIMING_VIOLATION_CORNERS` `*typ*`).
- [ ] Read the slow-corner flip-flop setup slack from the first path (group `flops`) of `55-openroad-stapostpnr/nom_slow_1p08V_125C/checks.rpt` (constraints in `src/signoff.sdc`).
- [ ] Check `final/metrics.csv`: route DRC 0, antenna violations 0, LVS errors 0, Magic DRC errors 0, max slew violations 0.
- [ ] Precheck: all checks pass (`precheck_reports/results.md`); gl_test passes.
