# Design decisions

Each entry gives the question, the options that were compared, the evidence, the decision and what would change it. Rejected options and negative results stay in the log. Evidence is limited to what a reader can check: GitHub Actions runs of this repository, files and tests in this repository, and numbered pages or clauses of public data sheets and standards.

How to check a run value: open the run link. The layout and timing reports are in the `GDS_logs` artifact. The flip-flop setup margin is the first path (group `flops`) of `runs/wokwi/55-openroad-stapostpnr/<corner>/checks.rpt`. Counts and areas are in `runs/wokwi/final/metrics.csv`. The slow corner is `nom_slow_1p08V_125C` (1.08 V, 125 °C). Runs on research branches measured one option each. The present design is branch `cmos5l`.

## 1. Processor: four threads and timed pin events

- **Question:** how does firmware get cycle-exact pin timing while several protocols run at the same time?
- **Options:** the choices of [docs/isa.md](isa.md) section 13, each with its rejected alternative. Examples: 4 or 8 data registers per thread (8 costs 17,185.3 µm² more register bits, by the cell areas of section 2.3); a late pin event happens 4 cycles after the instruction and sets a sticky LATE flag, or waits for the next 16-bit time match (a gap of 1.3 ms with no report), or is dropped, or stops the thread; one owner thread per output pin, or priority, or wired logic.
- **Evidence:** `formal/core.sby` proves the barrel order, the pin-event rules and the memory-write rules of `src/pe_core.v` by k-induction. `formal/timed_out.sby` proves the event slot for all leads. `test/test_core.py` compares the RTL with the reference model `model/pe_model.py` in every cycle, on directed and seeded random programs.
- **Decision:** four threads in a fixed barrel order (thread n runs in cycles c with c mod 4 = n), 4 data registers each, and pin changes as events at an absolute 16-bit cycle time. The minimum lead is 4 cycles. A late event still happens and is reported to the host.
- **Would change it:** a protocol that needs a lead of less than 4 cycles or more registers per thread.

## 2. Area: 6x4 tiles

- **Question:** which tile size?
- **Options:** 6x4 (the rule when the design was laid out); 8x4.
- **Evidence:** the placed design uses 30.6 % of the 6x4 core (`design__instance__utilization` 0.306052, run [42744631446]). Synthesis at the typical corner gives 216,632 µm² of cells, 23.6 % of the 6x4 outline (Yosys 0.70). On 9 November 2026 the [competition rules](https://blog.janestreet.com/protocol-emulator-asic-competition/) changed: 8x4 is now the maximum and the stated setting.
- **Decision:** 6x4, as built. Area is not the present limit. The choice of 8x4 after the rule change is open.
- **Would change it:** firmware or helper logic that does not fit, for example more than 128 memory words or a stretch-goal helper.

## 3. Program and data memory: 128 x 16 latch array

- **Question:** what holds the program and the data of the four threads?
- **Options:** a latch array of standard cells; an SRAM macro; flip-flops.
- **Evidence:** `src/pe_latch_ram.v`. Synthesis: 2,560 latches (2,048 memory bits and 512 host FIFO bits, which use the same latch array) and 160 clock gates. `formal/mem_full.sby` proves the memory properties at 128 x 16 by k-induction. The four full protocol programs use 103 of the 128 words (`test_fw_full_word_count`: UART TX 15, UART RX 13, SPI controller 26, I2C controller 49), or 114 words with the slow UART receiver.
- **Decision:** latch array; no macro in the flow. The comparison was by risk to the first layout runs, not by a measured macro layout: no run with a macro exists.
- **Would change it:** firmware for new protocols that needs more than 128 words. Then a macro layout would be measured.

## 4. System clock: 50 MHz

- **Question:** which clock is declared and timed?
- **Options:** 40, 48, 50 and 60 MHz, or one timed clock with a host table of firmware constants for other clocks.
- **Evidence:** at 50 MHz the slow-corner flip-flop setup margin of the present design is +1.329 ns (run [42744631446]). At 60 MHz, on the RTL of October 2026: −1.773 ns with 34 failing endpoints (run [40967811920]); +0.344 ns with timing-driven placement, but with max-slew violations (run [41032654188]). Every clock-dependent firmware value is a host constant ([docs/host_port.md](host_port.md) section 11). `test_r2_clock_margins` and `test_r2_host_sck_clocks` check the limits at 40 and 50 MHz. A 10BASE-T half bit (50 ns) is 2 clock cycles at 40 MHz and 2.5 cycles at 50 MHz (calculation).
- **Decision:** 50 MHz declared and timed. A host may run the chip at 40 MHz for 10BASE-T with its own constants, within the limits of docs/host_port.md section 11.
- **Would change it:** a slow-corner margin at 60 MHz that is positive with the default placement and has no slew violation.

## 5. Slow-corner setup margin

- **Question:** how do the longest flip-flop paths meet setup at the slow corner?
- **Options:** (a) register selects decoded one cycle early, default placement; (b) synthesis delay strategy (`SYNTH_STRATEGY` "DELAY 1"); (c) timing-driven placement; (d) timing-driven placement with a 1.2 ns max-transition limit in the place-and-route constraints.
- **Evidence:** (a) +1.329 ns on the present design (run [42744631446]). On the RTL of October 2026: (b) −0.862 ns, 67 failing endpoints (run [40967845275]); (c) +3.478 ns, but 12 max-slew violations (run [40967879447]); (d) +2.620 ns, hold +0.072 ns, no slew violation (run [41856662241]; the run is marked failed only because of its viewer job). The signoff constraints [src/signoff.sdc](../src/signoff.sdc) report flip-flop paths and latch paths in separate groups. Latch paths borrow time and show a slack of 0.000, so without the groups the flip-flop margin is not visible.
- **Decision:** (a). (d) is the reserve, with the trigger rule in [docs/tapeout_checklist.md](tapeout_checklist.md) section 5.
- **Would change it:** a slow-corner flip-flop margin below the threshold of that rule.

## 6. Flow settings and run time

- **Question:** the default flow on this design took 5 h 52 min (run [40793488418]), and a second run was cancelled at the 6 h job limit after 6 h 01 min (run [40846505319]). How does the layout run finish reliably?
- **Options:** default settings; setup slack margin 0 and 4 OpenROAD threads, with Magic DRC off in development runs only.
- **Evidence:** with margin 0, 4 threads and Magic DRC on, the layout job took 1 h 47 min (run [41380161509]). Its `final/metrics.csv` has the same values as the default-flow run 40793488418 in every row: cell count, area, utilisation, wire length, vias, hold buffers, hold slack, power. Magic DRC 0, LVS 0. Only two flow warnings differ. The present design takes 47 min 53 s with Magic DRC off (run [42744631446]).
- **Decision:** `PL_RESIZER_SETUP_SLACK_MARGIN` 0 and `OPENROAD_THREADS` 4 in [src/config.json](../src/config.json). Magic DRC stays off in development runs and is on in the submitted run ([docs/tapeout_checklist.md](tapeout_checklist.md) section 2).
- **Would change it:** a submission rule that requires the unchanged template configuration.

## 7. Memory write data without reset

- **Question:** a store opens the word latch in the first half of the next cycle. A reset in that half cycle could clear the write data while the word is open.
- **Options:** write-data flip-flops without reset; with synchronous reset; reset that closes the word clock; reset that blocks the write enable; asynchronous reset with a host reload rule.
- **Evidence:** `formal/mem_reset.sby` (8 x 16, bounded to 10 steps) and `formal/mem_full_reset.sby` (128 x 16, k-induction) pass with a free reset in every step. The formal jobs of the other options are not in this repository.
- **Decision:** no reset on the write-data flip-flops.
- **Would change it:** a memory property that fails with a free reset.

## 8. STOP holds the processor in reset

- **Question:** what do the pins do when the host stops the threads?
- **Options:** hold the processor in reset (all outputs released at once; memory and configuration kept); let the chip drive defined stop levels.
- **Evidence:** `test_r2_stop_pins` passes in RTL (run [42744631302]) and at gate level (run [42744631446]). After STOP all `uio` output enables are 0, `uo_out[6:0]` is 1, and no pin changes in the next 2,000 cycles. The host rules for STOP (end the transfer first; hold each SPI chip select high with a pull-up while stopped) are in [docs/host_port.md](host_port.md) section 8.
- **Decision:** the processor is held in reset while stopped. The host rules are part of the interface.
- **Would change it:** a board on which the host cannot pull the chip-select lines high.

## 9. Host port bandwidth: no hardware change

- **Question:** can the SPI host port carry all four example protocols at their highest rates at the same time?
- **Options:** keep the port; a faster input synchronizer (more area); two bytes per host word (firmware only).
- **Evidence:** [docs/host_port.md](host_port.md) section 10. All four at full rate need 116.5 % of the port at a 10 MHz host clock, and any three fit. These numbers come from RTL and model tests on the research branch `r2-timing` (`test_r2_host_throughput`, `test_r2_host_need`), not re-run for this page. The competition rules call simultaneous protocols a plus, not a requirement.
- **Decision:** keep the port hardware. The documented limit is any three protocols at full rate. Two-byte firmware formats are optional loads.
- **Would change it:** a requirement for all four protocols at full rate together.

## 10. I2C input spike filter: 4 samples, on the I2C pins only

- **Question:** I2C Fast-mode inputs must suppress spikes shorter than 50 ns (NXP UM10204 Rev. 7.0, Table 10, tSP, page 43, and its note [8], page 44). How many equal samples, and on which pins?
- **Options:** 3 or 4 equal samples; a filter on `uio[6]`, `uio[7]` only, or on all 13 inputs.
- **Evidence:** at 50 MHz a spike of 41 to 49 ns can give 3 samples, so 3 equal samples are not enough; 4 samples remove every spike shorter than 60 ns ([docs/isa.md](isa.md) section 6.1). `test_m3_i2c_filter_k` compares 1, 3 and 4 samples in the model. `test_core_spike_filter` shows in RTL that pulses of 1 to 3 cycles have no effect. `test_fw_full_i2c_spikes` checks correct transfers with spikes. Area: 215.9 µm² per filtered pin (3 flip-flops, synthesis, typical corner). A filter on every input would add 4 cycles of delay to the UART and SPI inputs, which do not need it.
- **Decision:** 4 samples, on `uio[6]` and `uio[7]` only.
- **Would change it:** a protocol on `uio[6]` or `uio[7]` with pulses shorter than 4 clock cycles.

## 11. SPI target firmware: one wait per bit

- **Question:** how fast can a thread act as an SPI target?
- **Options:** wait for the sample edge only and set the next MISO bit after it; wait for both SCK edges.
- **Evidence:** `test_m3_spi_tgt` runs `fw/spi_tgt.s` in all four modes against an SPI controller model that does not use the firmware or the processor model. Smallest SCK half period: 25 cycles, so up to 1 MHz at a 50 MHz clock. Waiting for both edges needs 49 cycles, 510 kHz ([docs/host_port.md](host_port.md) section 13). The limit comes from the instruction rate (one instruction every 4 cycles, lead 4), not from the pins. An earlier estimate of about 5 MHz did not hold.
- **Decision:** one wait per bit, rated 1 MHz at 50 MHz.
- **Would change it:** a helper in hardware for SPI target timing.

## 12. No FPGA prototype

- **Question:** should the RTL run on an FPGA before the layout flow?
- **Options:** the FPGA of the template workflow (iCE40UP5K); no FPGA step.
- **Evidence:** the design needs 8,173 of the 5,280 iCE40UP5K logic cells (154 %), and placement fails (run [41817455061], `02-nextpnr.log`). The competition page gives FPGA testing as advice for entrants who have an FPGA.
- **Decision:** no FPGA step. Verification is by simulation, formal proofs and gate-level runs ([docs/verification.md](verification.md)).
- **Would change it:** a board with a larger FPGA, for a demonstration before silicon.

## 13. Timing-annotated gate-level simulation as an extra check

- **Question:** does the netlist work with the cell delays of the layout, and should this replace static timing?
- **Options:** none; cell delays from the SDF files; cell and wire delays.
- **Evidence:** 22 tests pass at the slow, typical and fast corners with cell delays from the netlist of gds run 41404293323 (runs [41871325339] and [41924796459]). `test_r3_sdf_delay` fails if no delay is applied. Icarus Verilog applies the delays but runs no timing checks, and it stops on the wire-delay lines, so those are not loaded. `test_counter_sequence` alone takes 17,275 s at the slow corner.
- **Decision:** the SDF job ([.github/workflows/sdf.yaml](../.github/workflows/sdf.yaml)) runs on the final gds run, by hand or after a tape-out tag, not on every push. Static timing stays the setup and hold check.
- **Would change it:** a simulator that runs the timing checks of the cell models.

## Claims with no public evidence yet

- Pad electrical limits (I2C low-level output voltage, 5 V tolerance, Fast-mode Plus): transistor-level simulations of the pads exist, but their netlists are not in this repository. Until they are, the chip claims I2C Standard-mode and Fast-mode only, and makes no 5 V claim.
- The rejected options of entry 7 and a 2-cycle event lead were measured with formal jobs and synthesis that are not in this repository. The entries name these options without their numbers.

[40793488418]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/40793488418
[40846505319]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/40846505319
[40967811920]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/40967811920
[40967845275]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/40967845275
[40967879447]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/40967879447
[41032654188]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41032654188
[41380161509]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41380161509
[41817455061]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41817455061
[41856662241]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41856662241
[41871325339]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41871325339
[41924796459]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/41924796459
[42744631302]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/42744631302
[42744631446]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/42744631446
