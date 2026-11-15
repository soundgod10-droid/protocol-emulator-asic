# Signoff review

The competition asks for "a clean run through the Tiny Tapeout flow and precheck" and recommends gate-level simulation. This page goes further and applies the usual signoff checks of a digital tape-out. For each check it gives the source of the practice, our result with its run, and what stays open. The design reviewed is branch `cmos5l` at commit 69bbf37: 8x4 tiles, 60 MHz, template main flow (`tt-gds-action@ttihp27a`), clock tree of [decisions.md](decisions.md) entry 17, layout run [43712217729]. The layout job prints the signoff values of all three corners in its run summary and as annotations (step `Signoff facts`, [.github/signoff_facts.py](../.github/signoff_facts.py)), so they can be read without downloading the log artifact.

How to read a value: open the run, artifact `GDS_logs`. Timing: `runs/wokwi/55-openroad-stapostpnr/<corner>/checks.rpt` and `summary.rpt`. Counts: `runs/wokwi/final/metrics.csv`. Corners: `nom_slow_1p08V_125C`, `nom_typ_1p20V_25C`, `nom_fast_1p32V_m40C`.

## 1 Checks and results

| Check | Source of the practice | Result (run [43712217729] unless stated) | State |
|---|---|---|---|
| Setup timing, all corners | signoff static timing of the LibreLane flow (step `OpenROAD.STAPostPNR`, checker step `Checker.SetupViolations`) | 0 violations at the 3 corners; flip-flop slack at 16.667 ns (group `flops`, [src/signoff.sdc](../src/signoff.sdc)): +3.841 ns slow, +8.458 ns typical, +11.202 ns fast; latch paths borrow time and report 0.000 | clean |
| Hold timing, all corners | same flow, `Checker.HoldViolations` | 0 violations; worst slack +0.099 ns (fast), +0.273 ns (typical), +0.586 ns (slow) | clean |
| Max transition (slew) | Liberty `max_transition` of the cells, `Checker.MaxSlewViolations` | 0 at the 3 corners | clean |
| Max capacitance | Liberty `max_capacitance`, `Checker.MaxCapViolations` | 0 at the 3 corners | clean |
| Max fanout | `default_max_fanout` 8 of the CMOS5L standard-cell library (the flow's own constraint `MAX_FANOUT_CONSTRAINT` is 10; STA uses the smaller value); the flow has no checker step for it | 8 pins at each corner, none on the clock tree: `place3079`, `place3083`, `place3115`, `place3126` (12 loads), `place3119` (10), `place3085`, `place3093`, `place3118` (9). Each of these nets drives at most 8 logic inputs; the other loads are antenna diodes that the router adds on long wires (step netlists traced in a research run of the same settings, [decisions.md](decisions.md) entry 17). Slew and capacitance, which include the diodes, are within limits at all corners. Before the clock-tree setting: 84 pins (run [43672052931]) | accepted with reason (entry 17) |
| Lint | Verilator lint of the flow (`Verilator.Lint`, checkers for errors, warnings, timing constructs) | 0 errors, 0 warnings, 0 timing constructs, 0 inferred latches (the latch cells are instantiated); 2 local waivers for the power pins of the two instantiated cells ([decisions.md](decisions.md) entry 14) | clean |
| DRC | foundry deck run by the Tiny Tapeout precheck (KLayout); Magic DRC in the flow | precheck KLayout SG13CMOS5L DRC: pass; route DRC 0 (detailed router); Magic DRC is off in development runs and on in the submitted run ([tapeout_checklist.md](tapeout_checklist.md) section 2) | open item 2 |
| LVS | Netgen LVS of the flow (`Netgen.LVS`, `Checker.LVS`) | 0 errors, 0 unmatched devices, nets or pins | clean |
| Antenna | OpenROAD antenna check and repair of the flow | 0 violating nets after 3 diodes | clean |
| Disconnected pins | `Checker.DisconnectedPins` of the flow | 1 disconnected pin, 0 critical (the unused `ena` input of the template) | clean |
| Power grid | `Checker.PowerGridViolations`; IR drop report | 0 grid violations; worst IR drop 0.2 mV at typical, estimated without voltage-source locations (the flow warns that the value is approximate) | clean, estimate only |
| Precheck | Tiny Tapeout precheck (the competition's requirement) | 10 of 10 checks pass | clean |
| Gate-level simulation, functional | Tiny Tapeout `gl_test` job (recommended by the competition) | 44 pass, 25 skipped (model-only), 0 failures | clean |
| Gate-level simulation with layout delays | SDF back-annotated simulation, a standard post-layout check | `.github/workflows/sdf.yaml` on this run: section 3; on the first 8x4 run (commit 9c38482, 50 MHz, `@ihp-cmos5l` flow, run [43622834317]; same RTL apart from the lint waivers): job [43642622272] | see section 3 |
| Clock domain crossing and reset | structural CDC and reset review: two-flip-flop synchronizer on every single-bit asynchronous input, no logic that combines two asynchronous inputs before a synchronizer, reset asserted asynchronously and released synchronously (C. E. Cummings, "Clock Domain Crossing (CDC) Design & Verification Techniques Using SystemVerilog", SNUG Boston 2008; C. E. Cummings, D. Mills, S. Golson, "Asynchronous & Synchronous Reset Design Techniques - Part Deux", SNUG Boston 2003) | section 2; no CDC tool was run | reviewed by hand; open item 6 |
| Formal proofs | property checking of each block | 13 jobs give their expected result ([verification.md](verification.md) section 3); run again on 14 November 2026 at commit a5a8003, whose RTL is the RTL of this layout run | clean, run locally |
| Code coverage | line and toggle coverage of the RTL tests | not measured: Icarus Verilog gives no coverage | open item 5 |
| Metastability | MTBF of the input synchronizers from the measured time constant | tau about 101 ps at the slow corner (SPICE, [verification.md](verification.md) section 7); at 60 MHz a synchronizer has about 16 ns to resolve, e^(16 / 0.101) is about 10^68 (calculation; the other MTBF factors are not measured) | clean by calculation |

## 2 CDC and reset review

One clock domain (`clk`). Asynchronous inputs and their treatment, from `src/project.v`, `src/pe_sync.v`, `src/pe_timed_in.v`:

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
- Gated clocks: only through the integrated clock-gate cells of the memory and FIFO latch arrays (`pe_cg`); the gate enable comes from flip-flops of the same domain and is checked by the clock-gating setup check of the timing run (slack +9.1 ns at the slow corner).

## 3 Results that come after this page

| Item | Run | Result |
|---|---|---|
| Precheck | [43647479367] job `precheck` | pass, 10 of 10 checks (`precheck_reports/results.md`): KLayout pin labels, KLayout SG13CMOS5L DRC, KLayout zero area, KLayout checks, pin, boundary, layer, cell name, analog pin, Verilog syntax |
| Gate-level test | [43647479367] job `gl_test` | pass: 44 tests pass, 25 skipped (protocol tests that run only in the model), 0 failures (`gatelevel_test_results/results.xml`) |
| SDF simulation, this run | [43651078000] | all 6 jobs succeed: the main part (`test_r3_sdf`, `test.py`, `test_core.py`) and the counter part (`test_counter_sequence`, 256 back-to-back UART frames) at slow, typical and fast; the slow counter job took 1 h 16 min |
| SDF simulation, first 8x4 run | [43642622272] | all 6 jobs succeed (main and counter parts at the 3 corners); the slow counter job took 4 h 30 min |

## 4 Open items

1. **Max fanout (84 pins).** 80 are clock-tree buffers that clock tree synthesis sizes by slew and capacitance, not by the fanout limit of 8; 4 are placement buffers with fanout 9 to 12. All 84 meet the slew and capacitance limits at the 3 corners. Option to close: a lower `MAX_FANOUT_CONSTRAINT` or clock-tree fanout setting in a research run, compared by slack and slew.
2. **Magic DRC** is on only in the submitted run; the precheck KLayout DRC runs on every push.
3. **Fast-corner hold slack** +0.010 ns: positive, below the 0.05 ns margin the hold repair aims for. Watch it on each RTL change; an option is a larger hold margin in a research run.
4. **IR drop** is an estimate without voltage-source locations.
5. **Coverage** is not measured. Verilator can give line and toggle coverage with cocotb; it needs a second simulator build of the test bench and a CI job.
6. **CDC by tool**: no structural CDC tool was run; section 2 is a review by hand of 17 asynchronous inputs and the reset tree.

[43622834317]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43622834317
[43642622272]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43642622272
[43647479367]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43647479367
[43651078000]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43651078000
