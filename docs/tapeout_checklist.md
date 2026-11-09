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
- [ ] Keep the four keys above the line "DO NOT CHANGE ANYTHING BELOW THIS POINT", and the part below that line identical to the template src/config.json of branch cmos5l (compared 2026-11-07, decision D11). The question to Tiny Tapeout about these keys is answered by documents (decision D11): the shuttle uses the layout of the project's own run, and precheck.py reads no configuration (design note R3 2c). Ask the organizers again only if the final submission form says that they build the layout again. Fallback if a person forbids the margin key: fallback C, measured (default margin, latch D pins out of the PnR setup checks, gds 41813363136: 55 min 27 s, flip-flop slack +1.496 ns).

## 3 Before the tape-out run: template, tools and GDS action (decision D11, R3 T6)

- [ ] Read the newest commit of each branch below and compare it with the last check. The gds workflow uses the moving reference `@ihp-cmos5l`, so a new commit of the action can change the flow of the tape-out run. Last check (GitHub commits, read 2026-11-04):

  | Repository, branch | Newest commit at the last check |
  |---|---|
  | TinyTapeout/ttihp-verilog-template, cmos5l | "fix(test): add sg13cmos5l UDP models to gate-level simulation" (2026-10-19) |
  | TinyTapeout/tt-support-tools, ihp-sg13cmos5l | "feat(ihp): 8x4 tile size for ihp-sg13cmos5l" |
  | TinyTapeout/tt-gds-action, ihp-cmos5l | "fix: install ihp-sg13cmos5l from the merged IHP-Open-PDK tree #52; Pin PDK version and bump the default LibreLane to 3.1.0.dev3" |

- [ ] If a commit is new: read what it changes. The flow gives the same metrics for the same netlist and settings, so a change of `final/metrics.csv` between the last development run and a run of the same commit shows a tool change. Write the PDK and LibreLane versions of the tape-out run (resolved.json) in the design note.

## 4 Tape-out run, in this order

1. [ ] Commit the final RTL with `RUN_MAGIC_DRC` on (section 2) on cmos5l and push. The gds run of this commit is the tape-out run and the submitted run (E5, E6). About 1 h 47 min with Magic DRC (gds 41380161509), inside the 6 h job limit.
2. [ ] Read the result (section 5). If a criterion fails, fix it and make a new tape-out commit; do not submit a run of another commit.
3. [ ] Run the SDF job on the tape-out run (E9): Actions > sdf > Run workflow, with the id of the tape-out gds run. It checks out the commit of that run and loads its netlist and SDF files; no new gds run. About 5 h at the slow corner, in two parts per corner (main tests; test_counter_sequence alone). Pass: every test passes at slow, typ and fast, `test_r3_sdf_delay` gives a clock-to-MISO delay of more than 0.2 ns (the annotation is in effect), and the "SDF ERROR" count is 4 (the header lines of the file). A tag whose name starts with "tapeout" also starts the job by itself after the gds run of the tag ends with success; that checks the gds run of the tag, which is a second run of the same commit. Icarus Verilog applies the cell delays only and runs no timing checks: STA stays the setup and hold check (decision D13).
4. [ ] Give the tape-out run in the submission (commit_id.json workflow_url, E6).

## 5 Reading the result (decision D9)

- [ ] Read the timing from `55-openroad-stapostpnr/summary.rpt` at all three corners, not from the job status: the flow fails only on violations at the typical corner (`TIMING_VIOLATION_CORNERS` `*typ*`).
- [ ] Read the slow-corner flip-flop setup slack from the first path (group `flops`) of `55-openroad-stapostpnr/nom_slow_1p08V_125C/checks.rpt` (constraints in `src/signoff.sdc`).
- [ ] Check `final/metrics.csv`: route DRC 0, antenna violations 0, LVS errors 0, Magic DRC errors 0, max slew violations 0.
- [ ] Precheck: all checks pass (`precheck_reports/results.md`); gl_test passes.
