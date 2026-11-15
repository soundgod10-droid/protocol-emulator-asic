# Verification

Each method below gives what it checks, what it does not check, and its latest result. A reader can open each run link or re-run each job. The present design is branch `cmos5l`: 8x4 tiles, 60 MHz, layout run [43712217729], test run [43712217681] and coverage run [43712217710] (all commit 69bbf37). The signoff checks of the layout at all corners are in [signoff.md](signoff.md); the sections below that name older runs describe the method and keep their results for comparison.

## Summary

| Method | Checks | Latest result |
|---|---|---|
| RTL tests (cocotb, Icarus Verilog) | host port, firmware UART on the chip pins, processor against the reference model, assembler, full protocol firmware in the model, firmware for protocols outside the instruction-set design, hold-out trials | 75 tests, 0 failures, 0 skipped, run [43712217681] |
| Code coverage (Verilator) | line, branch and toggle coverage of the RTL tests | line 100 %, branch 100 %, toggle 98.5 %, run [43712217710] ([signoff.md](signoff.md) section 4) |
| Gate-level test (present layout) | the layout netlist with the RTL tests, no delays | 47 pass (23 drive the netlist), 28 skipped (model-only tests), run [43712217729] (section 4) |
| SDF gate-level simulation (present layout) | the netlist with layout cell delays at 3 corners | `test_r3_sdf`, `test.py`, `test_core.py` pass at slow, typical and fast (run [43716786712]); see [signoff.md](signoff.md) section 3 for the long counter test |
| Static timing and layout checks (present layout) | setup, hold, slew, capacitance, fanout, DRC, LVS, antenna, lint, precheck | 60 MHz: slow-corner setup margin +3.841 ns, no setup, hold, slew or capacitance violation at 3 corners; 8 max-fanout pins explained in [signoff.md](signoff.md); DRC, LVS, antenna, lint 0; precheck 10 of 10 |
| Lockstep with the reference model | every pin and every host word, in every clock cycle | part of the RTL and gate-level tests |
| Formal proofs (SymbiYosys, Z3) | barrel order, pin events, event slot, host port, latch memory; synchronizer and reset structure | 13 proof jobs and the structural CDC job pass (table below) |
| Synchronizer SPICE | metastability time constant of the input flip-flop | tau about 54 ps (typical), 101 ps (slow) (section 7) |
| Pad SPICE | the I/O pad cells: I2C output low voltage, output and input delay, load, 5 V tolerance, at 3 corners | Standard-mode and Fast-mode output low level met at every corner; Fast-mode Plus not met; not 5 V tolerant; output delay 1.13 to 3.32 ns, input 0.26 to 0.45 ns (section 9) |

## 1. RTL tests

`test/Makefile` runs six cocotb modules on the RTL in `src/`. The test job of every push runs them.

- `test/test_holdout.py` (6 tests): hold-out trials, protocols not named on the competition page or in the design documents, written from their public documents on the unchanged instruction set and assembler. AM2302 and PDI were chosen by a rule that a reader can repeat: the first two entries of the sigrok libsigrokdecode decoder list (`decoders/`, alphabetical) whose decoder reads logic channels, that link two devices, that are not in `fw/` and not named on the competition page, that 3.3 V pins with external parts can drive, that allow a bit time of 1 us or more, and that have a public specification (ac97 and aud fail the bit-time rule; the other entries before avr_pdi decode the output of the SPI, UART or I2C decoder). `test_holdout_am2302`: `fw/am2302.s` (56 words) against a sensor model from the AM2302 data sheet (pages 3 to 5) with all sensor times scaled 0.8 to 1.2, an answer after 20 to 40 us, both data sheet examples (one with a negative temperature), no sensor, and a sensor that stops after 20 bits, at 50 and 60 MHz. `test_holdout_pdi`: `fw/pdi.s` (52 words, AVR XMEGA Program and Debug Interface) against a device model from the XMEGA AU manual section 32 (enable sequence, 10 kHz inactivity rule, characters, guard time, BREAK and ERROR state, LDS, STS, LD, ST, LDCS, STCS, KEY, REPEAT), with reset pulse widths 0.1 to 40 us and device output delays of 1 cycle to HALF - 4, at 50 and 60 MHz (PDI_CLK 1 MHz). `test_holdout_pdi_rate`: the highest PDI_CLK is 1.25 MHz at 60 MHz. `test_holdout_am2302_pdi_words`: either program fits next to the two UART programs. The AM2302 trial found a fault in the first sensor model (it released the bus after a final 0 bit, which the data sheet figures do not allow), not in the firmware. `test_holdout_onewire`: `fw/onewire.s` (1-Wire controller, Analog Devices AN126) against a device model written from the DS18B20 data sheet (Maxim 19-7487 Rev 6, pages 3, 15, 16), at five device-timing corners, at 50 and 60 MHz; every controller low and high time is checked against the data sheet limits. `test_holdout_onewire_words`: 40 words; it fits next to any three of the four example programs, not next to all four. The trial found a real limit: the application note's 60 us write-0 time has no margin against a device that samples at 60 us, so the firmware uses 65 us.

- `test/test.py` (10 tests): loads `fw/uart_tx.s` through the host SPI port with the host model `test/host_spi.py` (written from [docs/host_port.md](host_port.md), not from the RTL), reads the memory back and decodes `uo_out[4]` with a UART receiver model that does not use the processor. It checks the bit time to 1 ps against 434 cycles, every edge position, back-to-back frames for 256 bytes (`test_counter_sequence`), the chip ID, the command rules and the invalid codes (every valid code with one wrong bit, and the six codes of the valid form that are no command), the SCK limit, STOP and START, and the reset during a frame.
- `test/test_core.py` (14 tests): lockstep, section 2.
- `test/test_isa.py` (21 tests): the assembler `asm/pe_asm.py` (every instruction form against the encoding of [docs/isa.md](isa.md) section 11.2, labels, directives, errors with line numbers) and the reference model `model/pe_model.py` against isa.md (barrel order, lead and late events, horizon, pin owner, open drain, SAMPLE and WAIT), with expected values worked out by hand from the document, not from the model.
- `test/test_fw_full.py` (17 tests): the full firmware in `fw/` in the reference model `model/pe_model.py`, against protocol models in the test file that do not use the firmware or the model: UART 300 bit/s to 1 Mbit/s with a sender bit-time error of ±3.3 %, SPI controller in all four modes, SPI target in all four modes (`test_m3_spi_tgt`), I2C Standard-mode and Fast-mode against the minimum times of NXP UM10204 Rev. 7.0, Table 11 (PDF page 44), with clock stretching, repeated START, NACK, arbitration loss, input spikes, bus clear and a stuck SCL, then all four protocols together from one 128-word memory (`test_fw_full_word_count`: 103 of 128 words).
- `test/test_flex.py` (7 tests): firmware for protocols that the instruction set was not designed for, in the reference model, against protocol models written from each specification (they import only the generic bench helpers of `test_fw_full.py`, not the firmware or the processor model). `test_flex_can`: CAN 2.0A frames both ways, arbitration loss and retry, ACK, the other node's clock +/-0.5 %, a transceiver loop delay, 4 start phases (Bosch CAN 2.0 Part A). `test_flex_jtag`: a two-device chain with IDCODE, IR and DR scans and Pause-DR, set-up, hold and TDO delay checks (TI SSYA002C). `test_flex_swd`: JTAG-to-SWD, line reset, DPIDR, SELECT, AP write with WAIT, posted AP read, a wrong write parity, the reset-state rule (Arm IHI0031H). `test_flex_ps2`: both sides at 10 to 16.7 kHz, inhibit before and after the 10th clock, command with acknowledge, 264 start phases (IBM PS/2 Technical Reference, Keyboards). `test_flex_trigger`: pulse latency and jitter from a UART start edge at 16 phases. `test_flex_i2c_char`: binary search of a target's SDA set-up limit with fw/i2c_ctl.s. `test_flex_word_count`: words of each program and the sets that fit in 128 words. Each test measures the highest correct rate and logs it; [README](../README.md) (Flexibility) and [docs/host_port.md](host_port.md) section 14 give the results.

Result: 75 testcases, 0 failures, 0 skipped (`results.xml` of run [43712217681], commit 69bbf37). Earlier: 68 testcases before the hold-out trials (run [43592752852]), 61 before the flexibility tests (run [42744631302]).

Does not check: `test_fw_full.py` and `test_flex.py` run the firmware in the model, not in the RTL; the RTL agrees with the model through the lockstep tests (section 2), not through these protocol runs. The CAN, JTAG, SWD and PS/2 models follow the cited specification pages only (no error frames or error counters in CAN, no boundary-scan register test in JTAG, no SWD protocol version 2, no PS/2 Resend), and only the four example programs have run at the same time in one memory.

## 2. Reference model and lockstep

`model/pe_model.py` is a cycle-exact model of [docs/isa.md](isa.md). `test/test_core.py` loads each program into the RTL through the host SPI port and gives the same input pins to the RTL and the model in every clock cycle. It compares `uo_out[6:0]`, `uio_out`, `uio_oe` and every host word read on `HOST_MISO` in every cycle. In RTL simulation it also compares the state of the thread in its slot (r0 to r3, T, PC, flags, LATE, ERR, RUN). Programs: directed programs for each instruction group, and random instruction streams of 4,000 cycles with 24 fixed seeds (4 seeds at gate level). A random program that the model rejects (a read 1 to 3 cycles after a store, [docs/isa.md](isa.md) section 3.3) is replaced by the next program of the same generator.

Does not check: programs that break the store-and-read rule of isa section 3.3, because their result is not defined; the internal state at gate level (the netlist has other names), where only pins and host words are compared.

## 3. Formal proofs

The properties are in the RTL (`ifdef FORMAL`) and in the wrapper files `formal/*_fv.v`. Each proof job stops with an error if fewer property cells than written reach the solver (`select -assert-min`), so a pass cannot come from properties that synthesis removed. Run with `sby -f formal/<job>.sby`. Last run: commit a5a8003 (14 November 2026; its RTL is the RTL of the present layout run), SymbiYosys with Z3: every job gives the result in the table (mem_full 556 s, mem_full_reset 636 s, host_port_cover 722 s). The run before: commit 6e2a0fd.

| Job | What | Mode, depth | Result |
|---|---|---|---|
| `core.sby` | barrel order B1 to B4, pin events E1 to E8, memory writes W1 to W3 of `src/pe_core.v`, all core inputs free | prove, 4 | pass, k-induction |
| `core_cover.sby` | the properties can be reached | cover, 12 | 3 covers reached |
| `timed_out.sby` | event slot T1 to T6 of `src/pe_timed_out.v`, for all leads 1 to 65,536 cycles | prove, 4 | pass, k-induction |
| `timed_out_cover.sby` | reachability | cover, 12 | 2 covers reached |
| `host_port.sby` | host port H1 to H7 of `src/pe_host_port.v` against a monitor that decodes the SPI pins as [docs/host_port.md](host_port.md) states; all inputs free, later resets free | prove, 4 | pass, k-induction |
| `host_port_cover.sby` | reachability of complete transactions | cover, 90 | 4 covers reached (steps 63 to 79) |
| `host_port_mut_id.sby` | check of the check: a wrong chip ID must make H6 fail | bmc, 90, expect fail | counterexample at step 64, as expected |
| `mem.sby` | latch memory M1 to M4 of `src/pe_latch_ram.v`: 128 words, 2-bit word | prove, 8 | pass, k-induction |
| `mem_small.sby` | the same, 8 words of 16 bits | prove, 8 | pass, k-induction |
| `mem_cover.sby` | reachability, 8 x 16 | cover, 10 | cover reached |
| `mem_reset.sby` | 8 x 16 with a free reset in every step | bmc, 10 | pass |
| `mem_full.sby` | 128 x 16, the chip size | prove, 3 | pass, k-induction (555 s) |
| `mem_full_reset.sby` | 128 x 16 with a free reset in every step | prove, 3 | pass, k-induction (590 s) |
| `cdc.sby` | structural clock-domain-crossing and reset check of the whole chip: each asynchronous input reaches storage only at a two-stage synchronizer, no logic reads a first stage, only synchronized resets ([signoff.md](signoff.md) section 2); not a proof, a structure check by Yosys selections | structure, then bmc 1 with no property | pass (14 s, commit 0fccb0b, 15 November 2026); a bypassed or one-stage synchronizer makes it fail |

Does not check: the whole chip in one proof (the jobs prove blocks: core, event slot, host port, memory); the latch timing (the solver models the clock as a signal that changes at each step, so the latch window is checked by static timing, section 6); the bounded jobs (bmc, cover) check only their stated depth.

Every push runs all 14 jobs of the table in parallel (`.github/workflows/formal.yaml`, a job matrix with OSS CAD Suite 2026-11-14; a job passes when SymbiYosys returns 0, so `host_port_mut_id.sby` passes only with its counterexample, "expect fail"). First run: [43759085016] (commit dbab159), all 14 jobs as in the table: `mem_full` PASS in 750 s, `mem_full_reset` PASS in 680 s, `host_port_cover` all covers in 384 s, `host_port_mut_id` DONE (FAIL, rc=0), `cdc` PASS in 8 s; about 13 min of wall time.

[43759085016]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43759085016

## 4. Gate-level test

The `gl_test` job of the gds workflow runs the same cocotb modules on the layout netlist with functional cell models (no delays). In run [43712217729] (present layout): 75 testcases, 47 pass, 28 skipped. Of the 47, 23 drive the netlist (`test.py` 9, `test_core.py` 14 with 4 random seeds); the other 24 are assembler and model tests that do not use the netlist (`test_isa.py` 21 and the three word counts `test_fw_full_word_count`, `test_holdout_onewire_words`, `test_holdout_am2302_pdi_words`). The 28 skipped tests are the model-only protocol tests (`test_fw_full.py` 16, `test_flex.py` 7, `test_holdout.py` 4) and `test_r2_host_sck_clocks`. The job takes 41 min; `test_counter_sequence` alone takes 1,787 s of it.

Does not check: delays; see sections 5 and 6.

## 5. SDF gate-level simulation

`.github/workflows/sdf.yaml` runs `test.py`, `test_core.py` and `test/test_r3_sdf.py` on the netlist of a finished gds run with the cell delays of its SDF file, at the slow, typical and fast corners. `test_r3_sdf_delay` fails if no delay is applied. It takes about 5 h at the slow corner (`test_counter_sequence` alone 17,275 s), so it runs by hand or after a tape-out tag, not on every push.

Result on the present layout: every job passes at the three corners, run [43716786712] (netlist of gds run [43712217729]; [signoff.md](signoff.md) section 3). The SDF job runs again on the submitted layout run ([docs/tapeout_checklist.md](tapeout_checklist.md) section 4).

Does not check: wire delays (Icarus Verilog stops on the interconnect lines, so they are not loaded); timing checks (Icarus Verilog applies the delays but does not run the setup and hold checks of the cell models). Static timing stays the setup and hold check.

## 6. Static timing at all corners

The layout flow reports timing at `nom_slow_1p08V_125C`, `nom_typ_1p20V_25C` and `nom_fast_1p32V_m40C`. The flow fails only on violations at the typical corner, so every run is read in `55-openroad-stapostpnr/summary.rpt` and in `checks.rpt` of each corner (artifact `GDS_logs`), not from the job status. [src/signoff.sdc](../src/signoff.sdc) puts flip-flop paths and latch paths in separate groups: latch paths borrow time and report a slack of 0.000, which would hide the flip-flop margin.

Result, present layout, run [43712217729], 60 MHz (16.667 ns):

- setup: no violation at the three corners; flip-flop margin +3.841 ns (slow), +8.458 ns (typical), +11.202 ns (fast) (first path of group `flops` in each corner's `checks.rpt`);
- hold: no violation; worst hold slack 0.0987 ns (fast), 0.2732 ns (typical), 0.5862 ns (slow) (`summary.rpt`);
- max slew and max capacitance: 0 violations at each corner;
- max fanout: 8 at each corner (`final/metrics.csv`), the same 8 buffer outputs with 9 to 12 loads against the library limit 8; explained in [signoff.md](signoff.md).

Does not check: on-chip variation beyond the corner models; silicon.

## 7. Synchronizer SPICE

`spice/m2c_sync_tau.sp` (typical models, 1.2 V, 25 °C) and `spice/m2c_sync_tau_ss.sp` (slow models, 1.08 V, 125 °C) find the data-to-clock offset where the input flip-flop just fails to capture, then measure the clock-to-Q delay at offsets of 10 ps to 1 fs from that point. The slope of the delay against ln(offset) from 10 ps to 0.01 ps is the metastability time constant: 0.3728 ns over ln(1000), about 54 ps (typical), and 0.6958 ns over ln(1000), about 101 ps (slow). Below 0.01 ps the delay reaches the resolution of the simulation. Every input pin has two such stages.

Does not check: the pads, process variation (one corner each, no Monte Carlo).

## 8. Layout checks

Run [43712217729] (present layout), `final/metrics.csv`: route DRC 0, antenna violations 0, LVS errors 0, lint errors and warnings 0, utilisation 0.246. Precheck: 10 of 10 checks pass (`precheck_reports/results.md`). Magic DRC is off in development runs. The present 8x4 layout with Magic DRC on gave 0 errors and the same signoff values as run [43712217729] (run [43731920994]); the earlier 6x4 layout also gave 0 (run [41380161509]). The submitted run will have it on ([docs/tapeout_checklist.md](tapeout_checklist.md) section 2).

## 9. Pad SPICE

The I/O cells of the CMOS5L Tiny Tapeout chip are `sg13cmos5l_IOPadInOut30mA` on `uio`, `sg13cmos5l_IOPadOut30mA` on `uo_out` and `sg13cmos5l_IOPadIn` on `ui_in` (cell names in the foundry netlist of the earlier CMOS5L shuttle TTIHP0p4, `tt_ihp_wrapper.nl.v`). The netlists in `spice/` simulate these cells with ngspice, the IHP-Open-PDK CMOS5L models and the schematic netlist `sg13cmos5l_io.spice` (no layout extraction), at the three signoff corners: slow `pad_ss.sp` (mos_ss, 1.08 V core, 3.0 V IO, 125 °C), typical `pad_tt.sp` (mos_tt, 1.20 V, 3.3 V, 25 °C), fast `pad_ff.sp` (mos_ff, 1.32 V, 3.6 V, -40 °C). Output load 5 pF; input pad edges 2 ns.

| Value | Slow | Typical | Fast | Limit (source) |
|---|---|---|---|---|
| Output low voltage of the 30 mA pad sinking 3 mA | 0.142 V | 0.087 V | 0.059 V | 0.4 V for Standard-mode and Fast-mode I2C (UM10204 Rev. 7.0 Table 10) |
| Sink current at 0.4 V | 7.6 mA | not run | 18.4 mA | 20 mA for Fast-mode Plus (same table) |
| Output low voltage sinking 10 mA / 20 mA | 0.568 V / no drive (3.72 V) | not run / 0.756 V | 0.204 V / 0.441 V | - |
| Output delay, core input 50 % to pad 50 %, rise / fall | 3.32 / 3.18 ns | 2.03 / 1.74 ns | 1.46 / 1.13 ns | - |
| Pad edge 10 to 90 %, rise / fall | 1.49 / 1.75 ns | 1.00 / 1.16 ns | 0.74 / 0.86 ns | - |
| Input pad delay, pad 50 % to core 50 %, rise / fall | 0.45 / 0.42 ns | 0.32 / 0.34 ns | 0.26 / 0.28 ns | - |

`pad_load_ss.sp`, slow corner, output into 5 / 15 / 30 pF: delay rise 3.32 / 4.36 / 5.73 ns, fall 3.18 / 4.06 / 5.28 ns; edge rise 1.49 / 3.21 / 5.86 ns, fall 1.75 / 3.15 / 5.57 ns (about 0.1 ns of delay per pF). The input pad with a 50 MHz and a 100 MHz clock on the pad gives core-side high and low times of 9.97 / 10.03 ns and 4.97 / 5.03 ns with a full swing (-0.02 to 1.10 V), so the pad does not limit the clock up to at least 100 MHz; the core does ([signoff.md](signoff.md)).

`pad_5v.sp`, typical corner, IOVDD 3.3 V, current into the pad at 3.3 / 3.6 / 4.0 / 5.0 V: input pad 30 pA / 36 pA / 2.2 uA / 0.42 mA; bidirectional pad with its output off 34 pA / 0.46 uA / 1.27 mA / 20.8 mA. The pads are not 5 V tolerant: keep every pin at IOVDD or below.

Results: I2C Standard-mode and Fast-mode output levels are met at every corner (0.142 V at 3 mA, slow); Fast-mode Plus is not (7.6 mA at 0.4 V slow, 18.4 mA fast, against 20 mA), so the chip claims Standard-mode and Fast-mode only.

Does not check: layout parasitics of the pad ring and package; the Tiny Tapeout multiplexer between the pads and the tile (inside the host port allowance, [host_port.md](host_port.md) section 6); the diodes stay at their typical model (`cornerDIO.lib` has only `dio_tt`); one sample per corner, no Monte Carlo; silicon.

## What is not verified

- No FPGA prototype: the design needs 154 % of the logic cells of the template FPGA (run [41817455061], [docs/decisions.md](decisions.md) entry 12).
- Pad electrical limits are simulated on the schematic pad cells only (section 9), not on the extracted pad ring or in silicon. The chip claims I2C Standard-mode and Fast-mode only and makes no 5 V claim.
- The host port allowance of 27.4 ns for pads and the host input ([docs/host_port.md](host_port.md) section 6) is partly an estimate. It will be measured on the board.
- No silicon result exists.

## How AI tools were used

The engineer was assisted by AI agents. The agents worked through a harness that lets them operate professional engineering software through its normal user interface, as an engineer does: the waveform viewer (GTKWave), the layout viewer (KLayout), a PDF reader for standards and data sheets, and the open-source design tools named in this file. They were not limited to a command line. After a change, an agent opened the result in the same software and checked it on the screen, so that it saw what a reviewer sees, for example the waveforms of a protocol test or the layout of a gds run. The result line of a tool was not taken as the check on its own. The visual check is there to catch errors that a language model can make when it only reads and writes text.

**How the results were checked.** No result was accepted because an AI tool said so. Each design choice needed evidence that a reader can check (a test or formal job in this repository, a GitHub Actions run, a synthesis area, or a numbered page of a public standard or data sheet) and a comparison with at least one alternative; [docs/decisions.md](decisions.md) keeps the rejected options and the failed runs. The tests compare the design with models written from the documents, not from the RTL: the protocol models in `test/` follow the standards, the host model follows [docs/host_port.md](host_port.md), and the expected values in `test_isa.py` were worked out by hand from docs/isa.md. Later reviews read each cited source again. Some first answers were wrong and were corrected this way: the I2C input filter needs 4 equal samples at 50 MHz, not 3 ([docs/decisions.md](decisions.md) entry 10); the SPI target firmware is rated 1 MHz, not the 5 MHz first estimated (entry 11); the 1-Wire write-0 time of the application note (60 us) has no margin against a DS18B20 that samples at 60 us, and the hold-out test found it (section 1). The final checks are those of the flow (static timing at three corners, DRC, LVS, precheck) and the gate-level tests, not an AI tool's reading of them.

[41380161509]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41380161509
[41817455061]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41817455061
[41856662241]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41856662241
[41871325339]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41871325339
[41924796459]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41924796459
[42744631302]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/42744631302
[42744631446]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/42744631446
[43592752852]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43592752852
[43647479367]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43647479367
[43651078000]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43651078000
[43653069782]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43653069782
[43672052915]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43672052915
[43712217681]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43712217681
[43712217710]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43712217710
[43712217729]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43712217729
[43716786712]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43716786712
[43731920994]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43731920994
