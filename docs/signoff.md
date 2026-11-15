# Signoff review

The competition asks for "a clean run through the Tiny Tapeout flow and precheck" and recommends gate-level simulation. This page goes further and applies the usual signoff checks of a digital tape-out. For each check it gives the source of the practice, our result with its run, and what stays open. The design reviewed is branch `cmos5l` at commit 69bbf37: 8x4 tiles, 60 MHz, template main flow (`tt-gds-action@ttihp27a`), clock tree of [decisions.md](decisions.md) entry 17, layout run [43712217729]. The layout job prints the signoff values of all three corners in its run summary and as annotations (step `Signoff facts`, [.github/signoff_facts.py](../.github/signoff_facts.py)), so they can be read without downloading the log artifact.

How to read a value: open the run, artifact `GDS_logs`. Timing: `runs/wokwi/55-openroad-stapostpnr/<corner>/checks.rpt` and `summary.rpt`. Counts: `runs/wokwi/final/metrics.csv`. Corners: `nom_slow_1p08V_125C`, `nom_typ_1p20V_25C`, `nom_fast_1p32V_m40C`.

## 1 Checks and results

| Check | Source of the practice | Result (run [43712217729] unless stated) | State |
|---|---|---|---|
| Setup timing, all corners | signoff static timing of the LibreLane flow (step `OpenROAD.STAPostPNR`, checker step `Checker.SetupViolations`) | 0 violations at the 3 corners; flip-flop slack at 16.667 ns (group `flops`, [src/signoff.sdc](../src/signoff.sdc)): +3.841 ns slow, +8.458 ns typical, +11.202 ns fast; latch paths borrow time and report 0.000 (worst borrow at the slow corner 3.054 ns of the 8.060 ns allowed) | clean |
| Hold timing, all corners | same flow, `Checker.HoldViolations` | 0 violations; worst slack +0.099 ns (fast), +0.273 ns (typical), +0.586 ns (slow) | clean |
| Max transition (slew) | Liberty `max_transition` of the cells, `Checker.MaxSlewViolations` | 0 at the 3 corners | clean |
| Max capacitance | Liberty `max_capacitance`, `Checker.MaxCapViolations` | 0 at the 3 corners | clean |
| Max fanout | `default_max_fanout` 8 of the CMOS5L standard-cell library (the flow's own constraint `MAX_FANOUT_CONSTRAINT` is 10; STA uses the smaller value); the flow has no checker step for it | 8 pins at each corner, none on the clock tree: `place3079`, `place3083`, `place3115`, `place3126` (12 loads), `place3119` (10), `place3085`, `place3093`, `place3118` (9). Each of these nets drives at most 8 logic inputs; the other loads are antenna diodes that the router adds on long wires (step netlists traced in a research run of the same settings, [decisions.md](decisions.md) entry 17). Slew and capacitance, which include the diodes, are within limits at all corners. Before the clock-tree setting: 84 pins (run [43672052931]) | accepted with reason (entry 17) |
| Lint | Verilator lint of the flow (`Verilator.Lint`, checkers for errors, warnings, timing constructs) | 0 errors, 0 warnings, 0 timing constructs, 0 inferred latches (the latch cells are instantiated); 2 local waivers for the power pins of the two instantiated cells ([decisions.md](decisions.md) entry 14) | clean |
| DRC | foundry deck run by the Tiny Tapeout precheck (KLayout); Magic DRC in the flow | precheck KLayout SG13CMOS5L DRC: pass; route DRC 0 (detailed router); Magic DRC (full rule style) on this layout: 0 errors in a run of the same configuration with `RUN_MAGIC_DRC` 1 ([43731920994]: `62-magic-drc/reports/drc.magic.rpt` COUNT 0, `magic__drc_error__count` 0; every other signoff value equal to [43712217729]; the Magic step took 47 min). Development runs leave Magic DRC off; the submitted run has it on ([tapeout_checklist.md](tapeout_checklist.md) section 2) | clean; repeated in the submitted run |
| LVS | Netgen LVS of the flow (`Netgen.LVS`, `Checker.LVS`) | 0 errors, 0 unmatched devices, nets or pins | clean |
| Antenna | OpenROAD antenna check and repair of the flow | 0 violating nets and pins after 12 diodes; repair by diodes, not by layer jumpers only, which left 15 violating nets (run [43705472044], entry 17) | clean |
| Disconnected pins | `Checker.DisconnectedPins` of the flow | 1 disconnected pin, 0 critical (the unused `ena` input of the template) | clean |
| Power grid | `Checker.PowerGridViolations`; IR drop report | 0 grid violations; worst IR drop 0.2 mV at typical (VPWR 0.19 mV, VGND 0.22 mV), estimated without voltage-source locations (the flow warns that the value is approximate) | clean, estimate only |
| Precheck | Tiny Tapeout precheck (the competition's requirement) | 10 of 10 checks pass | clean |
| Gate-level simulation, functional | Tiny Tapeout `gl_test` job (recommended by the competition) | 47 pass, 28 skipped (model-only), 0 failures | clean |
| Gate-level simulation with layout delays | SDF back-annotated simulation, a standard post-layout check | `.github/workflows/sdf.yaml` on this run: section 3 | see section 3 |
| Clock domain crossing and reset | structural CDC and reset review: two-flip-flop synchronizer on every single-bit asynchronous input, no logic that combines two asynchronous inputs before a synchronizer, reset asserted asynchronously and released synchronously (C. E. Cummings, "Clock Domain Crossing (CDC) Design & Verification Techniques Using SystemVerilog", SNUG Boston 2008; C. E. Cummings, D. Mills, S. Golson, "Asynchronous & Synchronous Reset Design Techniques - Part Deux", SNUG Boston 2003) | section 2: reviewed by hand, and checked by tool with [formal/cdc.sby](../formal/cdc.sby) (Yosys structural check of the flattened RTL): pass | clean (structure); reconvergence of the three host bits is argued in section 2 |
| Formal proofs | property checking of each block | 13 jobs give their expected result ([verification.md](verification.md) section 3); run again on 14 November 2026 at commit a5a8003; the RTL changes since then are the model selection for the Verilator coverage build (`PE_RTL_SIM` in `src/pe_cells.v`, not defined in synthesis or in the formal jobs) and comments | clean, run locally |
| Code coverage | line, branch and toggle coverage of the RTL tests (Verilator coverage, [.github/workflows/coverage.yaml](../.github/workflows/coverage.yaml), every push) | run [43712217710]: line 173 of 173 (100 %), branch 208 of 208 (100 %), toggle 7,663 of 7,782 (98.5 %); details in section 4 | clean; toggle points left are explained (section 4) |
| Metastability | MTBF of the input synchronizers from the measured time constant | tau about 101 ps at the slow corner (SPICE, [verification.md](verification.md) section 7); at 60 MHz a synchronizer has about 16 ns to resolve, e^(16 / 0.101) is about 10^68 (calculation; the other MTBF factors are not measured) | clean by calculation |

## 2 CDC and reset review

One clock domain (`clk`). Asynchronous inputs and their treatment, from `src/project.v`, `src/pe_sync.v`, `src/pe_timed_in.v`. [formal/cdc.sby](../formal/cdc.sby) checks this table by tool on the flattened RTL (single-bit cells, single-bit nets): the 16 data inputs reach storage only at 16 synchronizer first stages; between the pins and the first stages there are exactly 17 logic cells (the CS_N inverter and two cells of the multiplexer of each uio input); each first stage drives only its second stage; `rst_n` reaches only the two reset-synchronizer flip-flops; and every asynchronous reset pin of a flip-flop is driven only by `rst_n` (reset synchronizer), the synchronized chip reset and `run_rst_n`. Pass in 14 s; it fails when one synchronizer is bypassed (504 storage cells reached) or has one stage (6,785 cells read the first stage).

| Input | Path | Synchronizer | Notes |
|---|---|---|---|
| `ui_in[7]` HOST_SCK, `ui_in[6]` HOST_CS_N, `ui_in[5]` HOST_MOSI | `pe_sync2` each (2 flip-flops with asynchronous reset); CS_N is inverted before its synchronizer | yes | the three bits are related (MOSI is taken at the SCK edge). All three synchronizers have the same delay, and the host rules keep MOSI stable for T + pin skew around the rising SCK edge and each SCK level for 2 T ([host_port.md](host_port.md) section 3), so a 1-cycle difference between the synchronizers cannot take a wrong MOSI bit. Checked in RTL at 10 host phases (`test_host_sck_limit`, `test_r2_host_sck_clocks`) and by `formal/host_port.sby` with free inputs |
| `ui_in[4:0]` inputs 8 to 12 | `pe_timed_in`: `pe_sync2` each | yes | single-bit protocol inputs; the firmware reads each pin alone (`SAMPLE`, `WAIT`) |
| `uio_in[7:0]` inputs 0 to 7 | `pe_timed_in`: the level is `(uio_in & ~OE) | (V & OE)`, then `pe_sync2` | yes | the multiplexer before the synchronizer combines one asynchronous input with two flip-flop outputs of the same clock domain (V, OE), not two asynchronous inputs; when OE changes the multiplexer output settles in the same cycle, before the next sampling edge. Inputs 6, 7 (`uio[6]`, `uio[7]`) have a 4-sample spike filter after the synchronizer |
| `rst_n` | `pe_reset_sync`: asserts at once, releases at the second clock edge | yes (reset synchronizer) | all flip-flops leave reset in the same cycle |
| `ena` | not used | - | always 1 on the shuttle (template) |

Reset tree:

- Chip reset `rst_n_int` (from `pe_reset_sync`): every flip-flop with a reset, including the input synchronizers and the host port.
- Processor reset `run_rst_n = rst_n_int & running`: the core, the host FIFOs, the event slots and the pin registers. Both inputs are flip-flop outputs, so the AND has no glitch and its release is at a clock edge. STOP (host command) asserts it; START releases it at a known cycle ([host_port.md](host_port.md) section 8).
- No reset: the memory latches and their write-data flip-flops ([decisions.md](decisions.md) entry 7; `formal/mem_full_reset.sby` proves the memory properties with a free reset in every step). The host loads the memory after every power-up and select (host rule 6).
- Gated clocks: only through the integrated clock-gate cells of the memory and FIFO latch arrays (`pe_cg`); the gate enable comes from flip-flops of the same domain and is checked by the clock-gating setup check of the timing run (slack +8.818 ns at the slow corner, `nom_slow_1p08V_125C/checks.rpt`).

## 3 Results that come after this page

| Item | Run | Result |
|---|---|---|
| Precheck | [43712217729] job `precheck` | pass, 10 of 10 checks (`precheck_reports/results.md`): KLayout pin labels, KLayout SG13CMOS5L DRC, KLayout zero area, KLayout checks, pin, boundary, layer, cell name, analog pin, Verilog syntax |
| Gate-level test | [43712217729] job `gl_test` | pass: 47 of 75 tests pass, 28 skipped (protocol tests that run only in the model, and `test_r2_host_sck_clocks`), 0 failures (`gatelevel_test_results/results.xml`); 23 of the 47 drive the netlist (`test.py` 9, `test_core.py` 14) |
| SDF simulation, this run | [43716786712] | all 6 jobs succeed: the main part (`test_r3_sdf`, `test.py`, `test_core.py`: 22 tests pass at each corner, 0 failures) and the counter part (`test_counter_sequence`, 256 back-to-back UART frames) at slow, typical and fast; 4 "SDF ERROR" lines at each corner (the header lines of the file, as expected); the slow counter job took 1 h 18 min |
| SDF simulation, earlier 8x4 runs | [43651078000] (commit fcf649d), [43642622272] (first 8x4 run) | all 6 jobs succeed in each |

## 4 Code coverage

The coverage job builds the same cocotb test bench with Verilator 5.042 (`SIM=verilator`, line, branch and toggle coverage; [test/coverage_summary.py](../test/coverage_summary.py) makes the table) and runs every test module. Only the tests that drive the RTL add coverage: `test.py` (10) and `test_core.py` (14); the other modules run the reference model or the assembler. The memory and clock-gate cells use their behavioural models there, as in the Icarus RTL run.

Run [43712217710] (commit 69bbf37): line 173 / 173, branch 208 / 208, toggle 7,663 / 7,782 (98.5 %). Two tests were added because the first coverage run showed gaps: the command codes of the valid form that are no command (one line of the host command decode was never reached) and `test_core_start_top` (thread start addresses at the top of the memory; most start-address bits never toggled). The 119 toggle points left are not functional gaps:

- constant bits by construction (zero-extended fields of the configuration and status words, unused upper bits of the owner and pin-enable vectors);
- `ena`, which is 1 during every test (template input, not used);
- bits 21 to 31 of the cycle counter (`pe_cycle_counter.v` 48 / 70): a test runs at most about 1.1 million cycles, so these bits never change in simulation;
- some bits of the shift masks, which change only for some shift amounts.

## 5 Open items

1. **Magic DRC** is on only in the submitted run; the precheck KLayout DRC runs on every push.
2. **Fast-corner hold slack** +0.099 ns (+0.010 ns before the clock-tree setting): watch it on each RTL change.
3. **IR drop** is an estimate without voltage-source locations.
4. **CDC by tool**: [formal/cdc.sby](../formal/cdc.sby) checks the synchronizer and reset structure (section 2). It does not judge reconvergence of synchronized bits (the three host bits, argued in section 2 and checked by `formal/host_port.sby`) and is not a commercial CDC tool with its full rule set.
5. **Max fanout**: 8 pins over the library limit, each a net with at most 8 logic inputs plus antenna diodes (section 1). A design-repair fanout limit of 8 would remove them, but on this flow version it triggers a clock tree synthesis defect ([decisions.md](decisions.md) entries 3 and 17).

[43622834317]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43622834317
[43642622272]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43642622272
[43647479367]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43647479367
[43651078000]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43651078000
[43672052931]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43672052931
[43705472044]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43705472044
[43712217710]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43712217710
[43712217729]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43712217729
[43716786712]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43716786712
