# Signoff review

This page applies the usual signoff checks of a digital tape-out to the layout and gives, for each check, the source of the practice, our result with its run, and what stays open. The competition asks for a clean run through the layout flow and the layout precheck and recommends gate-level simulation; the checks below go further. The design reviewed is the integration of the SRAM memory and the line helpers ([decisions.md](decisions.md) entries 3, 18 to 20) with the setup-margin setting of entry 21: 8x4 tiles, 60 MHz, template main flow (`tt-gds-action@ttihp27a`), layout run [44005215480] (commit 33e087b). The layout job prints the signoff values of all three corners in its run summary and as annotations (steps `Signoff facts` and `Path facts`, [.github/signoff_facts.py](../.github/signoff_facts.py)), so they can be read without downloading the log artifact.

How to read a value: open the run, artifact `GDS_logs`. Timing: `runs/wokwi/<nn>-openroad-stapostpnr/<corner>/checks.rpt` and `summary.rpt`. Counts: `runs/wokwi/final/metrics.csv`. Corners: `nom_slow_1p08V_125C`, `nom_typ_1p20V_25C`, `nom_fast_1p32V_m40C`.

## 1 Checks and results

| Check | Source of the practice | Result (run [44005215480] unless stated) | State |
|---|---|---|---|
| Setup timing, all corners | signoff static timing of the LibreLane flow (`OpenROAD.STAPostPNR`, `Checker.SetupViolations`) | 0 violations at the 3 corners; flip-flop slack at 16.667 ns (group `flops`, [src/signoff.sdc](../src/signoff.sdc)): +1.682 ns slow, +4.311 ns typical, +4.491 ns fast; the FIFO latch paths borrow time and report 0.000; clock-gating checks +9.220 ns (slow) | clean; above the +1.1 ns target ([decisions.md](decisions.md) entries 4 and 21) |
| Hold timing, all corners | same flow, `Checker.HoldViolations` | 0 violations; worst slack +0.099 ns (fast), +0.272 ns (typical), +0.582 ns (slow); 3,030 hold buffers | clean |
| Max transition (slew) and max capacitance | Liberty `max_transition` and `max_capacitance` of the cells and of the SRAM macro pins (input 0.595 ns, A_DOUT 0.064 pF); with the macros a violation at any corner stops the run (`MAX_SLEW_VIOLATION_CORNERS`, `MAX_CAP_VIOLATION_CORNERS` "*") | 0 at the 3 corners; the macro-pin violations of the first SRAM layouts were removed by a 150 um wire-length limit in both design repairs ([decisions.md](decisions.md) entry 3) | clean |
| Max fanout | `default_max_fanout` 8 of the CMOS5L library (smaller than the flow's `MAX_FANOUT_CONSTRAINT` 10) | 0 at the 3 corners (earlier layouts had 8 pins with antenna-diode loads, entry 17) | clean |
| Lint | Verilator lint of the flow (`Verilator.Lint`) | 0 warnings; earlier runs of this version had 13 and then 1 width and unused-signal warnings in the helper RTL, all fixed without a change of behaviour; 2 local waivers for the power pins of the two instantiated cells (entry 14) | clean |
| DRC | the PDK KLayout deck run by the layout precheck; Magic DRC of the flow | precheck KLayout SG13CMOS5L DRC: pass; route DRC 0 (detailed router). Magic DRC is off: with the SRAM macros it reports about 173,000 shapes of one rule exception inside the IHP macro layout ("N-Diffusion spacing to N-well in SRAM < 54 (NW.d exception)"), while the KLayout deck passes on the same layout ([decisions.md](decisions.md) entry 20) | clean (KLayout deck) |
| LVS | Netgen LVS of the flow (`Netgen.LVS`, `Checker.LVS`); the macros are abstract cells (`MAGIC_EXT_ABSTRACT_CELLS`) | 0 errors | clean |
| Antenna | OpenROAD antenna check and repair | 0 violating nets | clean |
| Disconnected pins | `Checker.DisconnectedPins` | 1 disconnected pin, 0 critical (the unused `ena` input of the template) | clean |
| Power grid | `Checker.PowerGridViolations`, fatal (`ERROR_ON_PDN_VIOLATIONS`); macro grid of `src/pdn_cfg.tcl` | 0 grid violations; every vertical stripe over a macro lies on a supply column of its own net (stripe pitch 67.44 um, entry 19). The IR-drop report is a tile estimate without voltage-source locations; the flow warns that this may be ignored when the design is not a top-level chip | clean at tile level |
| Precheck | the shuttle's layout precheck (the competition's requirement) | 10 of 10 checks pass: KLayout pin labels, KLayout SG13CMOS5L DRC, KLayout zero area, KLayout checks, pin, boundary, layer, cell name, analog pin, Verilog syntax | clean |
| Gate-level simulation, functional | template `gl_test` job and our `gl_protocols` jobs (recommended by the competition) | `gl_test`, `gl_protocols` parts 1 and 2 (the protocol programs) and part 3 (the line-helper chip tests) pass on the netlist of run [44005215480]; test counts on the [Results](https://soundgod10-droid.github.io/protocol-emulator-asic/results/) page | clean (no delays) |
| Gate-level simulation with layout delays | SDF back-annotated simulation | `.github/workflows/sdf.yaml` on layout run [44046855681] (the same layout values as run [44005215480]): `test_r3_sdf` (60 MHz, read-back through both macros), `test.py` and `test_core.py` pass at the slow, typical and fast corners, SDF run [44055275323]. The macro entries are left out of the SDF file (their models have no delay paths), so the macro read time is checked by static timing only; wire delays are not loaded (section 5 item 4) | pass (cell delays) |
| Clock domain crossing and reset | structural CDC and reset review: two-flip-flop synchronizer on every single-bit asynchronous input, no logic that combines two asynchronous inputs before a synchronizer, reset asserted asynchronously and released synchronously (C. E. Cummings, "Clock Domain Crossing (CDC) Design & Verification Techniques Using SystemVerilog", SNUG Boston 2008; C. E. Cummings, D. Mills, S. Golson, "Asynchronous & Synchronous Reset Design Techniques - Part Deux", SNUG Boston 2003) | section 2: reviewed by hand, and checked by tool with [formal/cdc.sby](../formal/cdc.sby) on every push that changes `src/` or `formal/`: pass | clean (structure) |
| Formal proofs | property checking of each block | 27 of 27 jobs give their expected result (formal run [44421405029], commit 82082a1; [verification.md](verification.md) section 3) | clean |
| Code coverage | line, branch and toggle coverage of the RTL tests (Verilator, [.github/workflows/coverage.yaml](../.github/workflows/coverage.yaml)) | section 4 | explained |
| Metastability | MTBF of the input synchronizers from the measured time constant | tau about 101 ps at the slow corner (SPICE, [verification.md](verification.md) section 7). MTBF = e^(tr / tau) / (T0 f_clk f_data). A two-stage synchronizer at 60 MHz has a resolution time tr of about 15 ns (16.667 ns less clock-to-Q, setup and hold-buffer delay): e^(15 / 0.103) is above 10^63, so the MTBF is above 10^59 s for any T0 f_clk f_data up to 10^4 per second (T0 of the order of 10 ps, 60 MHz clock, 30 MHz data). The falling-edge first stage of a helper data input has half a cycle less: e^(6.6 / 0.103) is above 10^27, MTBF above 10^23 s on the same terms (calculation; T0 is not measured) | clean by calculation |

## 2 CDC and reset review

One clock domain (`clk`); the helper data inputs also use its falling edge. Asynchronous inputs and their treatment, from `src/project.v`, `src/pe_sync.v`, `src/pe_timed_in.v` and `src/pe_slu.v`. [formal/cdc.sby](../formal/cdc.sby) checks this table on the flattened RTL (single-bit cells and nets): the data inputs reach storage only at 20 first stages (16 `pe_sync2` first stages and 4 helper data-input stages, of which 2 on the falling edge); 47 logic cells lie between the pins and the first stages (the CS_N inverter, the multiplexer of each uio input, and the pin-select multiplexers of the helper data inputs); each first stage drives only its second stage; `rst_n` reaches only the two reset-synchronizer flip-flops; and every asynchronous reset pin is driven only by `rst_n` (reset synchronizer), the synchronized chip reset and `run_rst_n`.

| Input | Path | Synchronizer | Notes |
|---|---|---|---|
| `ui_in[7]` HOST_SCK, `ui_in[6]` HOST_CS_N, `ui_in[5]` HOST_MOSI | `pe_sync2` each; CS_N is inverted before its synchronizer | yes | the three bits are related (MOSI is taken at the SCK edge); all three synchronizers have the same delay, and the host rules keep MOSI stable for T + pin skew around the rising SCK edge and each SCK level for 2 T ([host_port.md](host_port.md) section 3), so a 1-cycle difference between the synchronizers cannot take a wrong MOSI bit; checked in RTL at 10 host phases and by `formal/host_port.sby` with free inputs |
| `ui_in[4:0]` inputs 8 to 12 | `pe_timed_in`: `pe_sync2` each | yes | single-bit protocol inputs |
| `uio_in[7:0]` inputs 0 to 7 | `pe_timed_in`: the level is `(uio_in & ~OE) | (V & OE)`, then `pe_sync2` | yes | the multiplexer combines one asynchronous input with two flip-flop outputs of the same domain, not two asynchronous inputs; inputs 6, 7 have a 5-sample spike filter after the synchronizer |
| Helper data input DIN (any of the 13 pin levels, chosen by the helper's SEL word) | pin-select multiplexer, then one first stage on the falling clock edge (`din_f1`, then `din_f2`, `din_f3`) and one on the rising edge (`din_r1`, then `din_r2`) | yes, two stages each | the select is a configuration flip-flop that changes only when a thread writes SEL, so the multiplexer passes the one selected asynchronous input and combines none; both samples enter the receive loop two stages after the pad, like the other inputs |
| Helper AUX inputs (VP, VM, squelch) | the chip synchronizer outputs `s(c)` | yes | no own stage |
| `rst_n` | `pe_reset_sync`: asserts at once, releases at the second clock edge | yes (reset synchronizer) | all flip-flops leave reset in the same cycle |
| `ena` | not used | - | always 1 on the shuttle (template) |

Reset tree:

- Chip reset `rst_n_int` (from `pe_reset_sync`): every flip-flop with a reset, including the input synchronizers, the host port and the SRAM read-select flip-flop.
- Processor reset `run_rst_n = rst_n_int & running`: the core, the host FIFOs, the event slots, the pin registers and the two line helpers. Both inputs are flip-flop outputs, so the AND has no glitch and its release is at a clock edge. STOP asserts it; START releases it at a known cycle ([host_port.md](host_port.md) section 8). A helper pin is an output only while the threads run.
- No reset: the SRAM contents (the host loads them after every power-up and select, host rule 6), and the FIFO latches and their write-data flip-flops ([decisions.md](decisions.md) entry 7; `formal/mem_full_reset.sby`).
- Gated clocks: only through the integrated clock-gate cells of the FIFO latch arrays (`pe_cg`); their enables come from flip-flops of the same domain and are checked by the clock-gating setup check (+9.448 ns at the slow corner).

## 3 SRAM macros

| Item | Value | Source |
|---|---|---|
| Macros | `RM_IHPSG13_1P_512x16_c2_bm_bist` (program) at (11.04, 40) FS, `RM_IHPSG13_1P_1024x16_c2_bm_bist` (data) at (348.24, 40) FS | [src/config.json](../src/config.json) `MACROS` |
| Liberty corners | typical 1.20 V 25 C, slow 1.08 V 125 C, fast 1.32 V -55 C (mapped on the flow's -40 C fast corner) | same |
| Request path | address, write enable and data from flip-flops; the word is read about 7.4 ns after the clock edge (slow, macro liberty) and taken into the instruction register two cycles after the request | [src/pe_sram.v](../src/pe_sram.v), [docs/isa.md](isa.md) 3.3 |
| Supplies | VDD! and VDDARRAY! on VPWR, VSS! on VGND, through full-height Metal4 stripes on the macro supply columns | `PDN_MACRO_CONNECTIONS`, [src/pdn_cfg.tcl](../src/pdn_cfg.tcl) |
| Simulation | functional models written from the macro data sheet (`src/sram_sim.v`), black boxes for synthesis and lint (`src/sram_bb.v`); `formal/sram.sby` checks the address map and the read-after-write of both macros | [verification.md](verification.md) section 3 |

## 4 Code coverage

The coverage job builds the cocotb test bench with Verilator (`SIM=verilator`, line, branch and toggle coverage; [test/coverage_summary.py](../test/coverage_summary.py)) and runs every test module. Run [44265588374] (commit f147d6c; `src/` the same as the reviewed layout's): line 425 of 550 (77.3 %), branch 909 of 1,096 (82.9 %), toggle 14,564 of 18,540 (78.6 %). Every RTL file has 100 % line coverage except `src/pe_slu.v` (214 of 336) and `src/pe_slu_loop.v` (13 of 16). Earlier: line 416 of 550 (75.6 %) in run [44005215379] (commit 33e087b), 73.6 % before the helper tests (run [43966684440]).

**Where the missed points are.** Verilator counts each parameter set of a module as a separate copy, and `coverage.dat` records the instance of every point (key `h`). The same run, counted per instance with the parser of `test/coverage_summary.py`:

| Instance of `src/pe_slu.v` | SPEC | Line | Branch | Toggle |
|---|---|---|---|---|
| `tb.u_slu_ut`: the general unit, unit tests only | 0 | 91 of 112 (81.2 %) | 239 of 266 (89.8 %) | 1,802 of 2,498 (72.1 %) |
| `tb.user_project.u_proc.u_usb`: the chip's USB helper | 1 | 63 of 112 (56.2 %) | 184 of 266 (69.2 %) | 1,339 of 2,498 (53.6 %) |
| `tb.user_project.u_proc.u_eth`: the chip's 10BASE-Te helper | 2 | 60 of 112 (53.6 %) | 194 of 266 (72.9 %) | 1,464 of 2,498 (58.6 %) |

All chip instances (`tb.user_project`, every file): line 321 of 422 (76.1 %), branch 620 of 778 (79.7 %), toggle 11,562 of 14,446 (80.0 %).

So 101 of the 125 missed line points are in the two chip helper copies, not in the unit-test copy (21). The cause is the fixed configuration of the chip copies: the configuration registers are written only when SPEC is 0 (`src/pe_slu.v`, the `p_addr == 13 && SPEC == 0` case; a chip copy can write only the pin select `c_sel`), so in each chip copy they keep their reset values. That case alone is 22 missed lines in each chip copy, and most of the others are branches of modes that the fixed configuration of that copy cannot enter: the clocked and NRZ codings, loopback, stuffing in the 10BASE-Te copy, jam, jabber and squelch in the USB copy. Synthesis removes the same logic. The general unit's own misses (21 lines: clocked mode, NRZ, loopback, the reads of EVMASK, CRCA, CRCB and the line register, TXCTRL "start now", defaults) are modes that only the unit tests can reach. In `src/pe_slu_loop.v`, two lines (the phase wrap below minus half a bit and the integrator limit at +2,047) are not driven by the tests.

**Points that the chip can reach and no test drives** (by reading the source; each will get a test, or a proof that it cannot be reached): receive overflow and a transmit start with an empty buffer in the 10BASE-Te copy, a transmit underflow in the middle of a frame, a link test pulse requested on the USB helper, and jabber in the 10BASE-Te copy (2^22 cycles of transmit, about 26 minutes of RTL simulation). Until then the raw figure above is the published figure, and no point is excluded from it.

## 5 Open items

1. **Slow-corner setup margin** (closed). Three layout runs of nearly the same netlist gave +1.308 ns (run [43966684593]), +0.740 ns and +0.487 ns (run [43989356298]); in each, the worst path started at the core's register select `sa` with a 0.69 ns hold-repair delay cell right after it. The post-CTS setup margin of 1.5 ns, with the FIFO latches taken out of the placement timing, gives +1.682 ns in research run [43998187307] and again in run [44005215480] ([decisions.md](decisions.md) entry 21), above the +1.1 ns target of entry 4.
2. **Lint** (closed): run [44005215480] reports 0 warnings.
3. **IR drop** is a tile-level estimate without voltage-source locations.
4. **SDF simulation** on the SRAM layout (closed, with limits). SDF run [44055275323] passes at three corners. Its first run on this layout ([44038072461]) showed two faults of the test set-up, both fixed in the tests:
   - vvp's SDF parser stopped at the first SRAM macro entry (bus-bit path names, `IOPATH A_CLK A_DOUT[0]`), so the 3,642 cells after it in the file had no delay while the tests passed. `test/sdf_names.py` now leaves the macro entries out, and the job fails on more than the 4 header annotation errors.
   - The UART bit time was measured over rising and falling edges, so the rise/fall delay difference of the pin read as a 5.35 ps bit-time error at the slow corner. It is now measured on falling edges.

   Limits: the macro read time and the wire delays are not in the simulation (static timing checks them); `test.py` and `test_core.py` keep their 50 MHz clock, because the UART receiver model of `test.py` is written for 50 MHz.
5. **CDC by tool**: [formal/cdc.sby](../formal/cdc.sby) checks the synchronizer and reset structure; it does not judge reconvergence of synchronized bits (the three host bits, argued in section 2) and is not a commercial CDC tool with its full rule set.

[43966684440]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43966684440
[43966684593]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43966684593
[43983798260]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43983798260
[43989356298]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43989356298
[43989356338]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43989356338
[43998187307]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43998187307
[44005215379]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44005215379
[44265588374]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44265588374
[44038072461]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44038072461
[44046855681]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44046855681
[44055275323]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44055275323
[44005215480]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44005215480
[44005215486]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44005215486
