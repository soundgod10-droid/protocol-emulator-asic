# Tape-out checklist

Items to do or check before the final (tape-out) gds run and the submission. Each item gives its reason. The decisions behind them are in docs/decisions.md (entries 5, 6, 12 and 13).

## 1 Exit criteria for tape-out

There is no FPGA step: the design does not fit the only FPGA of the template workflow (iCE40UP5K, 154 % of the logic cells, run 41817455061; docs/decisions.md entry 12).

| No. | Criterion | Reason | How to check |
|---|---|---|---|
| E1 | All RTL tests and formal jobs pass on the final RTL | the checks of docs/verification.md sections 1 to 3 must hold for the submitted RTL | test job (cocotb: test, test_isa, test_core, test_fw_full, test_flex) and the formal jobs in formal/ (core, core_cover, host_port, host_port_cover, host_port_mut_id, mem, mem_cover, mem_small, mem_full, mem_full_reset, mem_reset, timed_out, timed_out_cover) run on the tape-out commit |
| E2 | The template gate-level test passes | the competition FAQ recommends gate-level simulation | gl_test job of the submitted run |
| E3 | STA has no violation at the three flow corners | the flow fails only on typical-corner violations (section 5); docs/decisions.md entry 5 | section 5: summary.rpt and checks.rpt of all corners |
| E4 | Precheck passes, 9 of 9 checks | the competition FAQ asks for a passing GDS and precheck from the Tiny Tapeout flow | precheck job, precheck_reports/results.md |
| E5 | Magic DRC 0 in the submitted run itself | precheck runs only the KLayout deck on SG13CMOS5L (section 2); docs/decisions.md entry 6 | `magic__drc_error__count` = 0 in final/metrics.csv of the submitted run (section 2) |
| E6 | The submitted layout is one named GitHub Actions run, on the tape-out commit | the shuttle takes the layout of that run (section 2) | the run id in the submission (commit_id.json workflow_url) is the tape-out run of section 4 |
| E7 | The newest commits of the template, the tools and the GDS action are compared with the last check | the gds workflow uses moving action references (section 3) | section 3 |
| E8 | Max fanout "violations" are only the known clock-leaf set, and max slew is 0 | the present layout has 3 open max-slew violations at the slow corner (docs/verification.md section 6) | section 5 |
| E9 | The SDF gate-level job passes at the three corners on the final gds run | docs/decisions.md entry 13 | section 4 step 3 |

## 2 Flow configuration (src/config.json)

- [ ] Turn Magic DRC on in the tape-out commit itself, on the submitted branch, not in a side branch: remove the line `"RUN_MAGIC_DRC": 0` (or set it to 1). The shuttle takes the layout of one named run of the project's GDS action (Tiny Tapeout FAQ, read 2026-11-07: the GDS action artifact is "the final GDS file that will be added to the Tiny Tapeout submission", and "A project can't be submitted to a shuttle if its GDS action is failing"; the .github/workflows/gds.yaml files of the shuttle repositories TinyTapeout/tinytapeout-ihp-26b and TinyTapeout/tinytapeout-ihp-0p4 have no --harden step, read 2026-11-03). So the run with Magic DRC on must be the submitted run. Development runs leave it off to save run time; the precheck job runs only the KLayout SG13CMOS5L deck, so the final run must also give a Magic DRC result (`magic__drc_error__count` = 0 in `final/metrics.csv`). Measured with this configuration and Magic DRC on (gds 41380161509): gds job 1 h 47 min (the Magic DRC step 41 min 56 s, `drc.magic.rpt` COUNT 0), every other `final/metrics.csv` row equal to the development run gds 40955889046.
- [ ] Keep `"PL_RESIZER_SETUP_SLACK_MARGIN": 0` and `"OPENROAD_THREADS": 4`. With the default margin (0.05 ns) the post-CTS resizer loops for hours on the latch endpoints (borrow slack 0.000) and the gds job no longer finishes inside the 6 h GitHub job limit (gds 40846505319 cancelled at 6 h 01 min). Margin 0 gives the same final metrics as the default flow (gds 40955756051 against 40793488418).
- [ ] Keep the four keys above the line "DO NOT CHANGE ANYTHING BELOW THIS POINT", and the part below that line identical to the template src/config.json of branch cmos5l (compared 2026-11-07). The question to Tiny Tapeout about these keys is answered by documents: the shuttle uses the layout of the project's own run, and precheck.py reads no configuration (TinyTapeout/tt-support-tools, branch ihp-sg13cmos5l, precheck/precheck.py, read 2026-11-03). Ask the organizers again only if the final submission form says that they build the layout again. Fallback if a person forbids the margin key: fallback C, measured (default margin, latch D pins out of the PnR setup checks, gds 41813363136: 55 min 27 s, flip-flop slack +1.496 ns).

## 3 Before the tape-out run: template, tools and GDS action

- [ ] Read the newest commit of each branch below and compare it with the last check. The gds workflow uses the moving reference `@ihp-cmos5l`, so a new commit of the action can change the flow of the tape-out run. Last check (GitHub commits, read 2026-11-04):

  | Repository, branch | Newest commit at the last check |
  |---|---|
  | TinyTapeout/ttihp-verilog-template, cmos5l | "fix(test): add sg13cmos5l UDP models to gate-level simulation" (2026-10-19) |
  | TinyTapeout/tt-support-tools, ihp-sg13cmos5l | "feat(ihp): 8x4 tile size for ihp-sg13cmos5l" |
  | TinyTapeout/tt-gds-action, ihp-cmos5l | "fix: install ihp-sg13cmos5l from the merged IHP-Open-PDK tree #52; Pin PDK version and bump the default LibreLane to 3.1.0.dev3" |

- [ ] If a commit is new: read what it changes. The flow gives the same metrics for the same netlist and settings, so a change of `final/metrics.csv` between the last development run and a run of the same commit shows a tool change. Write the PDK and LibreLane versions of the tape-out run (resolved.json) in docs/verification.md.

## 4 Tape-out run, in this order

1. [ ] Commit the final RTL with `RUN_MAGIC_DRC` on (section 2) on cmos5l and push. The gds run of this commit is the tape-out run and the submitted run (E5, E6). About 1 h 47 min with Magic DRC (gds 41380161509), inside the 6 h job limit.
2. [ ] Read the result (section 5). If a criterion fails, fix it and make a new tape-out commit; do not submit a run of another commit.
3. [ ] Run the SDF job on the tape-out run (E9): Actions > sdf > Run workflow, with the id of the tape-out gds run. It checks out the commit of that run and loads its netlist and SDF files; no new gds run. About 5 h at the slow corner, in two parts per corner (main tests; test_counter_sequence alone). Pass: every test passes at slow, typ and fast, `test_r3_sdf_delay` gives a clock-to-MISO delay of more than 0.2 ns (the annotation is in effect), and the "SDF ERROR" count is 4 (the header lines of the file). A tag whose name starts with "tapeout" also starts the job by itself after the gds run of the tag ends with success; that checks the gds run of the tag, which is a second run of the same commit. Icarus Verilog applies the cell delays only and runs no timing checks: STA stays the setup and hold check (docs/decisions.md entry 13).
4. [ ] Give the tape-out run in the submission (commit_id.json workflow_url, E6).

## 5 Reading the result

- [ ] Read the timing from `55-openroad-stapostpnr/summary.rpt` and from `checks.rpt` of each of the three corners (`nom_slow_1p08V_125C`, `nom_typ_1p20V_25C`, `nom_fast_1p32V_m40C`), not from the job status: the flow fails only on violations at the typical corner (`TIMING_VIOLATION_CORNERS` `*typ*`), so a slow-corner setup violation or a fast-corner hold violation gives a green job (E3).
- [ ] Read the slow-corner flip-flop setup slack from the first path (group `flops`) of `55-openroad-stapostpnr/nom_slow_1p08V_125C/checks.rpt` (constraints in `src/signoff.sdc`), and the worst hold slack at the fast corner. Reserve rule (decisions D8, D13): if the slow-corner flip-flop slack is less than the measured spread of the flow (0.55 to 1.1 ns for the same RTL), use the reserve with no slew violation: timing-driven placement with `set_max_transition 1.2` in the PnR SDC (gds 41856662241: +2.620 ns, hold +0.0722 ns, 0 slew violations); the stronger reserve is setup margin 8 with the PnR SDC (gds 41910131846), and its max-slew pins must be read on that netlist.
- [ ] Check `final/metrics.csv`: route DRC 0, antenna violations 0, LVS errors 0, Magic DRC errors 0, max slew violations 0 (E5, E8).
- [ ] Max fanout "violations" (E8): they must be the clock-leaf set only (gds 41404293323: 73 at each corner, 71 of them clock leaf buffers buf_8 with fanout 12 to 19 against 8; not a timing fault). Read the list if the count changes.
- [ ] Lint: `01-verilator-lint/verilator-lint.log` has no warning.
- [ ] Global placement: read `GPL-0047` (weighted routing congestion) in the `openroad-globalplacement` log; routing, not cell area, is the first limit (gds 41404293323: 1.0490 at the start, 1.0095 after 6 routability loops). Use the final `design__instance__utilization` (30.6 % in gds 41404293323) for area headroom, not the summary value of GPL-0019 (decision D13, design note R3 S3).
- [ ] Precheck: all checks pass (`precheck_reports/results.md`, E4); gl_test passes (E2).
