# Verification

Each method below gives what it checks, what it does not check, and its latest result. A reader can open each run link or re-run each job. The present design is branch `cmos5l`: 8x4 tiles, 60 MHz, layout run [43712217729], test run [43712217681] and coverage run [43712217710] (all commit 69bbf37). The signoff checks of the layout at all corners are in [signoff.md](signoff.md); the sections below that name older runs describe the method and keep their results for comparison.

## Summary

| Method | Checks | Latest result |
|---|---|---|
| RTL tests (cocotb, Icarus Verilog) | host port, firmware UART on the chip pins, processor against the reference model, assembler, full protocol firmware in the model, firmware for protocols outside the instruction-set design, hold-out trials | 75 tests, 0 failures, 0 skipped, run [43712217681] |
| Code coverage (Verilator) | line, branch and toggle coverage of the RTL tests | line 100 %, branch 100 %, toggle 98.5 %, run [43712217710] ([signoff.md](signoff.md) section 4) |
| Gate-level test (present layout) | the layout netlist with the RTL tests, no delays | 47 pass, 28 skipped (model-only tests), run [43712217729] |
| SDF gate-level simulation (present layout) | the netlist with layout cell delays at 3 corners | `test_r3_sdf`, `test.py`, `test_core.py` pass at slow, typical and fast (run [43716786712]); see [signoff.md](signoff.md) section 3 for the long counter test |
| Static timing and layout checks (present layout) | setup, hold, slew, capacitance, fanout, DRC, LVS, antenna, lint, precheck | 60 MHz: slow-corner setup margin +3.841 ns, no setup, hold, slew or capacitance violation at 3 corners; 8 max-fanout pins explained in [signoff.md](signoff.md); DRC, LVS, antenna, lint 0; precheck 10 of 10 |
| Lockstep with the reference model | every pin and every host word, in every clock cycle | part of the RTL and gate-level tests |
| Formal proofs (SymbiYosys, Z3) | barrel order, pin events, event slot, host port, latch memory | 13 jobs pass (table below) |
| Gate-level test | the layout netlist with the RTL tests, no delays | 44 pass, 17 skipped (model-only tests), run [42744631446] |
| SDF gate-level simulation | the netlist with layout cell delays at 3 corners | 22 tests pass at each corner, on an earlier netlist (runs [41871325339], [41924796459]) |
| Static timing at all corners | setup, hold, slew, fanout | no setup or hold violation at 3 corners; slow-corner setup margin +1.329 ns, run [42744631446] |
| Synchronizer SPICE | metastability time constant of the input flip-flop | tau about 55 ps (typical), 100 ps (slow) |
| Layout checks | DRC, LVS, antenna, lint, precheck | 0 errors; precheck 9 of 9, run [42744631446] |

## 1. RTL tests

`test/Makefile` runs six cocotb modules on the RTL in `src/`. The test job of every push runs them.

- `test/test_holdout.py` (6 tests): hold-out trials, protocols not named on the competition page or in the design documents, written from their public documents on the unchanged instruction set and assembler. AM2302 and PDI were chosen by a rule that a reader can repeat: the first two entries of the sigrok libsigrokdecode decoder list (`decoders/`, alphabetical) whose decoder reads logic channels, that link two devices, that are not in `fw/` and not named on the competition page, that 3.3 V pins with external parts can drive, that allow a bit time of 1 us or more, and that have a public specification (ac97 and aud fail the bit-time rule; the other entries before avr_pdi decode the output of the SPI, UART or I2C decoder). `test_holdout_am2302`: `fw/am2302.s` (56 words) against a sensor model from the AM2302 data sheet (pages 3 to 5) with all sensor times scaled 0.8 to 1.2, an answer after 20 to 40 us, both data sheet examples (one with a negative temperature), no sensor, and a sensor that stops after 20 bits, at 50 and 60 MHz. `test_holdout_pdi`: `fw/pdi.s` (52 words, AVR XMEGA Program and Debug Interface) against a device model from the XMEGA AU manual section 32 (enable sequence, 10 kHz inactivity rule, characters, guard time, BREAK and ERROR state, LDS, STS, LD, ST, LDCS, STCS, KEY, REPEAT), with reset pulse widths 0.1 to 40 us and device output delays of 1 cycle to HALF - 4, at 50 and 60 MHz (PDI_CLK 1 MHz). `test_holdout_pdi_rate`: the highest PDI_CLK is 1.25 MHz at 60 MHz. `test_holdout_am2302_pdi_words`: either program fits next to the two UART programs. The AM2302 trial found a fault in the first sensor model (it released the bus after a final 0 bit, which the data sheet figures do not allow), not in the firmware. `test_holdout_onewire`: `fw/onewire.s` (1-Wire controller, Analog Devices AN126) against a device model written from the DS18B20 data sheet (Maxim 19-7487 Rev 6, pages 3, 15, 16), at five device-timing corners, at 50 and 60 MHz; every controller low and high time is checked against the data sheet limits. `test_holdout_onewire_words`: 40 words; it fits next to any three of the four example programs, not next to all four. The trial found a real limit: the application note's 60 us write-0 time has no margin against a device that samples at 60 us, so the firmware uses 65 us.

- `test/test.py` (10 tests): loads `fw/uart_tx.s` through the host SPI port with the host model `test/host_spi.py` (written from [docs/host_port.md](host_port.md), not from the RTL), reads the memory back and decodes `uo_out[4]` with a UART receiver model that does not use the processor. It checks the bit time to 1 ps against 434 cycles, every edge position, back-to-back frames for 256 bytes (`test_counter_sequence`), the chip ID, the command rules and the invalid codes (every valid code with one wrong bit, and the six codes of the valid form that are no command), the SCK limit, STOP and START, and the reset during a frame.
- `test/test_core.py` (14 tests): lockstep, section 2.
- `test/test_isa.py` (21 tests): the assembler `asm/pe_asm.py` (every instruction form against the encoding of [docs/isa.md](isa.md) section 11.2, labels, directives, errors with line numbers) and the reference model `model/pe_model.py` against isa.md (barrel order, lead and late events, horizon, pin owner, open drain, SAMPLE and WAIT), with expected values worked out by hand from the document, not from the model.
- `test/test_fw_full.py` (17 tests): the full firmware in `fw/` in the reference model `model/pe_model.py`, against protocol models in the test file that do not use the firmware or the model: UART 300 bit/s to 1 Mbit/s with a sender bit-time error of ±3.3 %, SPI controller in all four modes, SPI target in all four modes (`test_m3_spi_tgt`), I2C Standard-mode and Fast-mode against the minimum times of NXP UM10204 Rev. 7.0, Table 11 (PDF page 44), with clock stretching, repeated START, NACK, arbitration loss, input spikes, bus clear and a stuck SCL, then all four protocols together from one 128-word memory (`test_fw_full_word_count`: 103 of 128 words).
- `test/test_flex.py` (7 tests): firmware for protocols that the instruction set was not designed for, in the reference model, against protocol models written from each specification (they import only the generic bench helpers of `test_fw_full.py`, not the firmware or the processor model). `test_flex_can`: CAN 2.0A frames both ways, arbitration loss and retry, ACK, the other node's clock +/-0.5 %, a transceiver loop delay, 4 start phases (Bosch CAN 2.0 Part A). `test_flex_jtag`: a two-device chain with IDCODE, IR and DR scans and Pause-DR, set-up, hold and TDO delay checks (TI SSYA002C). `test_flex_swd`: JTAG-to-SWD, line reset, DPIDR, SELECT, AP write with WAIT, posted AP read, a wrong write parity, the reset-state rule (Arm IHI0031H). `test_flex_ps2`: both sides at 10 to 16.7 kHz, inhibit before and after the 10th clock, command with acknowledge, 264 start phases (IBM PS/2 Technical Reference, Keyboards). `test_flex_trigger`: pulse latency and jitter from a UART start edge at 16 phases. `test_flex_i2c_char`: binary search of a target's SDA set-up limit with fw/i2c_ctl.s. `test_flex_word_count`: words of each program and the sets that fit in 128 words. Each test measures the highest correct rate and logs it; [README](../README.md) (Flexibility) and [docs/host_port.md](host_port.md) section 14 give the results.

Result: 68 testcases, 0 failures, 0 skipped (job summary of run [43592752852], commit 33873db). The run before the flexibility tests: 61 testcases, 0 failures (run [42744631302]).

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

Does not check: the whole chip in one proof (the jobs prove blocks: core, event slot, host port, memory); the latch timing (the solver models the clock as a signal that changes at each step, so the latch window is checked by static timing, section 6); the formal jobs are not part of the push workflows and run on a local machine.

## 4. Gate-level test

The `gl_test` job of the gds workflow runs the same cocotb modules on the layout netlist with functional cell models (no delays). In run [42744631446]: 44 pass, 17 skipped. Of the 44, 22 drive the netlist (`test.py` 9, `test_core.py` 13 with 4 random seeds); the other 22 are assembler and model tests that do not use the netlist (`test_isa.py` 21, `test_fw_full_word_count`). The 17 skipped tests are the model-only protocol tests of `test_fw_full.py`.

Does not check: delays; see sections 5 and 6.

## 5. SDF gate-level simulation

`.github/workflows/sdf.yaml` runs `test.py`, `test_core.py` and `test/test_r3_sdf.py` on the netlist of a finished gds run with the cell delays of its SDF file, at the slow, typical and fast corners. `test_r3_sdf_delay` fails if no delay is applied. It takes about 5 h at the slow corner (`test_counter_sequence` alone 17,275 s), so it runs by hand or after a tape-out tag, not on every push.

Result: 22 tests pass at each of the three corners, with the netlist of gds run 41404293323 (runs [41871325339] and [41924796459]).

The 8x4 netlists have their own SDF runs ([signoff.md](signoff.md) section 3: runs [43651078000] and 43642622272, all jobs pass at the three corners); the SDF job runs again on the submitted layout run ([docs/tapeout_checklist.md](tapeout_checklist.md) section 4).

Does not check: wire delays (Icarus Verilog stops on the interconnect lines, so they are not loaded); timing checks (Icarus Verilog applies the delays but does not run the setup and hold checks of the cell models). Static timing stays the setup and hold check.

## 6. Static timing at all corners

The layout flow reports timing at `nom_slow_1p08V_125C`, `nom_typ_1p20V_25C` and `nom_fast_1p32V_m40C`. The flow fails only on violations at the typical corner, so every run is read in `55-openroad-stapostpnr/summary.rpt` and in `checks.rpt` of each corner (artifact `GDS_logs`), not from the job status. [src/signoff.sdc](../src/signoff.sdc) puts flip-flop paths and latch paths in separate groups: latch paths borrow time and report a slack of 0.000, which would hide the flip-flop margin.

Result, run [42744631446], 50 MHz:

- setup: no violation at the three corners; slow-corner flip-flop margin +1.329 ns (first path of group `flops`, `nom_slow_1p08V_125C/checks.rpt`);
- hold: no violation; worst hold slack 0.103 ns (fast), 0.283 ns (typical), 0.601 ns (slow);
- max slew: 3 violations at the slow corner, none at typical and fast. This is open: the reserve in [docs/decisions.md](decisions.md) entry 5 (d) had none (run [41856662241]);
- max fanout: 78 at each corner (`final/metrics.csv`).

Does not check: on-chip variation beyond the corner models; silicon.

## 7. Synchronizer SPICE

`spice/m2c_sync_tau.sp` (typical models, 1.2 V, 25 °C) and `spice/m2c_sync_tau_ss.sp` (slow models, 1.08 V, 125 °C) find the data-to-clock offset where the input flip-flop just fails to capture, then measure the clock-to-Q delay at offsets of 10 ps to 1 fs from that point. The slope of the delay against ln(offset) from 10 ps to 0.01 ps is the metastability time constant: 0.3728 ns over ln(1000), about 54 ps (typical), and 0.6958 ns over ln(1000), about 101 ps (slow). Below 0.01 ps the delay reaches the resolution of the simulation. Every input pin has two such stages.

Does not check: the pads, process variation (one corner each, no Monte Carlo).

## 8. Layout checks

Run [42744631446], `final/metrics.csv`: route DRC 0, antenna violations 0, LVS errors 0, lint warnings 0, utilisation 0.306. Precheck: 9 of 9 checks pass (`precheck_reports/results.md`). Magic DRC is off in development runs. The last run with Magic DRC on gave 0 errors (run [41380161509], an earlier commit). The submitted run will have it on ([docs/tapeout_checklist.md](tapeout_checklist.md) section 2).

## What is not verified

- No FPGA prototype: the design needs 154 % of the logic cells of the template FPGA (run [41817455061], [docs/decisions.md](decisions.md) entry 12).
- Pad electrical limits: transistor-level simulations of the pads are not in this repository. The chip claims I2C Standard-mode and Fast-mode only and makes no 5 V claim.
- The host port allowance of 27.4 ns for pads and the host input ([docs/host_port.md](host_port.md) section 6) is partly an estimate. It will be measured on the board.
- No silicon result exists.

## How AI tools were used

The chip was designed and verified by the engineer working with an AI assistant. This section says what the assistant did, how its results were checked, and what the engineer decided.

**What the assistant did.** It drafted RTL, firmware, the assembler, the reference model, the tests, the formal properties and the documents in this repository from the engineer's requirements and from [docs/isa.md](isa.md). It ran the open-source tools named in this file (Icarus Verilog with cocotb, Yosys, SymbiYosys with Z3, ngspice) and the Tiny Tapeout layout flow on GitHub Actions, and read their reports at all three timing corners. It read public standards, data sheets and the competition page, and compared design options.

**How the results were checked.** No result was accepted because the assistant said so. Each design choice needed evidence that a reader can check (a test or formal job in this repository, a GitHub Actions run, a synthesis area, or a numbered page of a public standard or data sheet) and a comparison with at least one alternative; [docs/decisions.md](decisions.md) keeps the rejected options and the failed runs. The tests compare the design with models written from the documents, not from the RTL: the protocol models in `test/` follow the standards, the host model follows [docs/host_port.md](host_port.md), and the expected values in `test_isa.py` were worked out by hand from docs/isa.md. Later reviews read each cited source again. Some first answers were wrong and were corrected this way: the I2C input filter needs 4 equal samples at 50 MHz, not 3 ([docs/decisions.md](decisions.md) entry 10); the SPI target firmware is rated 1 MHz, not the 5 MHz first estimated (entry 11); the 1-Wire write-0 time of the application note (60 us) has no margin against a DS18B20 that samples at 60 us, and the hold-out test found it (section 1). The final checks are those of the flow (static timing at three corners, DRC, LVS, precheck) and the gate-level tests, not the assistant's reading of them.

**What the engineer decided.** The requirements and the architecture, which risks to accept, each entry in [docs/decisions.md](decisions.md), and what is submitted. The assistant proposed and measured; the engineer decided.

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
