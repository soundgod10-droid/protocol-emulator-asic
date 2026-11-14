# Verification

Each method below gives what it checks, what it does not check, and its latest result. A reader can open each run link or re-run each job. The present design is branch `cmos5l`, commit 6e2a0fd (test run [42744631302], layout run [42744631446]).

## Summary

| Method | Checks | Latest result |
|---|---|---|
| RTL tests (cocotb, Icarus Verilog) | host port, firmware UART on the chip pins, processor against the reference model, assembler, full protocol firmware in the model | 61 tests, 0 failures, run [42744631302] |
| Lockstep with the reference model | every pin and every host word, in every clock cycle | part of the RTL and gate-level tests |
| Formal proofs (SymbiYosys, Z3) | barrel order, pin events, event slot, host port, latch memory | 13 jobs pass (table below) |
| Gate-level test | the layout netlist with the RTL tests, no delays | 44 pass, 17 skipped (model-only tests), run [42744631446] |
| SDF gate-level simulation | the netlist with layout cell delays at 3 corners | 22 tests pass at each corner, on an earlier netlist (runs [41871325339], [41924796459]) |
| Static timing at all corners | setup, hold, slew, fanout | no setup or hold violation at 3 corners; slow-corner setup margin +1.329 ns, run [42744631446] |
| Synchronizer SPICE | metastability time constant of the input flip-flop | tau about 55 ps (typical), 100 ps (slow) |
| Layout checks | DRC, LVS, antenna, lint, precheck | 0 errors; precheck 9 of 9, run [42744631446] |

## 1. RTL tests

`test/Makefile` runs five cocotb modules on the RTL in `src/`. The test job of every push runs them.

- `test/test.py` (9 tests): loads `fw/uart_tx.s` through the host SPI port with the host model `test/host_spi.py` (written from [docs/host_port.md](host_port.md), not from the RTL), reads the memory back and decodes `uo_out[4]` with a UART receiver model that does not use the processor. It checks the bit time to 1 ps against 434 cycles, every edge position, back-to-back frames for 256 bytes (`test_counter_sequence`), the chip ID, the command rules and the invalid codes (every valid code with one wrong bit), the SCK limit, STOP and START, and the reset during a frame.
- `test/test_core.py` (13 tests): lockstep, section 2.
- `test/test_isa.py` (21 tests): the assembler `asm/pe_asm.py` (every instruction form against the encoding of [docs/isa.md](isa.md) section 11.2, labels, directives, errors with line numbers) and the reference model `model/pe_model.py` against isa.md (barrel order, lead and late events, horizon, pin owner, open drain, SAMPLE and WAIT), with expected values worked out by hand from the document, not from the model.
- `test/test_fw_full.py` (18 tests): the full firmware in `fw/` in the reference model `model/pe_model.py`, against protocol models in the test file that do not use the firmware or the model: UART 300 bit/s to 1 Mbit/s with a sender bit-time error of ±3.3 %, SPI controller in all four modes, SPI target in all four modes (`test_m3_spi_tgt`), I2C Standard-mode and Fast-mode against the minimum times of NXP UM10204 Rev. 7.0, Table 11 (PDF page 44), with clock stretching, repeated START, NACK, arbitration loss, input spikes, bus clear and a stuck SCL, then all four protocols together from one 128-word memory (`test_fw_full_word_count`: 103 of 128 words).
- `test/test_flex.py` (7 tests): firmware for protocols that the instruction set was not designed for, in the reference model, against protocol models written from each specification (they import only the generic bench helpers of `test_fw_full.py`, not the firmware or the processor model). `test_flex_can`: CAN 2.0A frames both ways, arbitration loss and retry, ACK, the other node's clock +/-0.5 %, a transceiver loop delay, 4 start phases (Bosch CAN 2.0 Part A). `test_flex_jtag`: a two-device chain with IDCODE, IR and DR scans and Pause-DR, set-up, hold and TDO delay checks (TI SSYA002C). `test_flex_swd`: JTAG-to-SWD, line reset, DPIDR, SELECT, AP write with WAIT, posted AP read, a wrong write parity, the reset-state rule (Arm IHI0031H). `test_flex_ps2`: both sides at 10 to 16.7 kHz, inhibit before and after the 10th clock, command with acknowledge, 264 start phases (IBM PS/2 Technical Reference, Keyboards). `test_flex_trigger`: pulse latency and jitter from a UART start edge at 16 phases. `test_flex_i2c_char`: binary search of a target's SDA set-up limit with fw/i2c_ctl.s. `test_flex_word_count`: words of each program and the sets that fit in 128 words. Each test measures the highest correct rate and logs it; [README](../README.md) (Flexibility) and [docs/host_port.md](host_port.md) section 14 give the results.

Result: 61 testcases, 0 failures, 0 skipped (`results.xml` of run [42744631302]).

Does not check: `test_fw_full.py` and `test_flex.py` run the firmware in the model, not in the RTL; the RTL agrees with the model through the lockstep tests (section 2), not through these protocol runs. The CAN, JTAG, SWD and PS/2 models follow the cited specification pages only (no error frames or error counters in CAN, no boundary-scan register test in JTAG, no SWD protocol version 2, no PS/2 Resend), and only the four example programs have run at the same time in one memory.

## 2. Reference model and lockstep

`model/pe_model.py` is a cycle-exact model of [docs/isa.md](isa.md). `test/test_core.py` loads each program into the RTL through the host SPI port and gives the same input pins to the RTL and the model in every clock cycle. It compares `uo_out[6:0]`, `uio_out`, `uio_oe` and every host word read on `HOST_MISO` in every cycle. In RTL simulation it also compares the state of the thread in its slot (r0 to r3, T, PC, flags, LATE, ERR, RUN). Programs: directed programs for each instruction group, and random instruction streams of 4,000 cycles with 24 fixed seeds (4 seeds at gate level). A random program that the model rejects (a read 1 to 3 cycles after a store, [docs/isa.md](isa.md) section 3.3) is replaced by the next program of the same generator.

Does not check: programs that break the store-and-read rule of isa section 3.3, because their result is not defined; the internal state at gate level (the netlist has other names), where only pins and host words are compared.

## 3. Formal proofs

The properties are in the RTL (`ifdef FORMAL`) and in the wrapper files `formal/*_fv.v`. Each proof job stops with an error if fewer property cells than written reach the solver (`select -assert-min`), so a pass cannot come from properties that synthesis removed. Run with `sby -f formal/<job>.sby`. Last run: commit 6e2a0fd, SymbiYosys with Z3.

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

Does not check: the present netlist (run [42744631446]) has no SDF run yet, it is planned on the final layout run ([docs/tapeout_checklist.md](tapeout_checklist.md) section 4); wire delays (Icarus Verilog stops on the interconnect lines, so they are not loaded); timing checks (Icarus Verilog applies the delays but does not run the setup and hold checks of the cell models). Static timing stays the setup and hold check.

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

## Method

The design and the verification were AI-assisted, under the direction of the engineer. Every decision needed a ground that a reader can check (a measurement, a run, a standard or a data sheet page) and a comparison with at least one alternative. Later reviews checked the grounds again against their sources. The results are in this file and in [docs/decisions.md](decisions.md).

[41380161509]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41380161509
[41817455061]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41817455061
[41856662241]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41856662241
[41871325339]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41871325339
[41924796459]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41924796459
[42744631302]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/42744631302
[42744631446]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/42744631446
