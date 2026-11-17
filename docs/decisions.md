# Design decisions

Each entry gives the question, the options that were compared, the evidence, the decision and what would change it. Rejected options and negative results stay in the log. Evidence is limited to what a reader can check: GitHub Actions runs of this repository, files and tests in this repository, and numbered pages or clauses of public data sheets and standards.

How to check a run value: open the run link. The layout and timing reports are in the `GDS_logs` artifact. The flip-flop setup margin is the first path (group `flops`) of `runs/wokwi/55-openroad-stapostpnr/<corner>/checks.rpt`. Counts and areas are in `runs/wokwi/final/metrics.csv`. The slow corner is `nom_slow_1p08V_125C` (1.08 V, 125 °C). Runs on research branches measured one option each. The present design is branch `cmos5l`.

The reason for each smaller choice (every module, parameter, flow setting and instruction form, with the alternative compared and its numbers) is in [rationale.md](rationale.md).

## 1. Processor: four threads and timed pin events

- **Question:** how does firmware get cycle-exact pin timing while several protocols run at the same time?
- **Options:** the choices of [docs/isa.md](isa.md) section 13, each with its rejected alternative. Examples: 4 or 8 data registers per thread (8 costs 17,185.3 µm² more register bits, by the cell areas of section 2.3); a late pin event happens 4 cycles after the instruction and sets a sticky LATE flag, or waits for the next 16-bit time match (a gap of 65,536 cycles, 1.09 ms at the declared 60 MHz, with no report), or is dropped, or stops the thread; one owner thread per output pin, or priority, or wired logic.
- **Evidence:** `formal/core.sby` proves the barrel order, the pin-event rules and the memory-write rules of `src/pe_core.v` by k-induction. `formal/timed_out.sby` proves the event slot for all leads. `test/test_core.py` compares the RTL with the reference model `model/pe_model.py` in every cycle, on directed and seeded random programs.
- **Decision:** four threads in a fixed barrel order (thread n runs in cycles c with c mod 4 = n), 4 data registers each, and pin changes as events at an absolute 16-bit cycle time. The minimum lead is 4 cycles. A late event still happens and is reported to the host.
- **Would change it:** a protocol that needs a lead of less than 4 cycles or more registers per thread.

## 2. Area: 8x4 tiles

- **Question:** which tile size?
- **Options:** 6x4 (the rule when the design was first laid out); 8x4 (the size the [competition rules](https://blog.janestreet.com/protocol-emulator-asic-competition/) state since 9 November 2026).
- **Evidence:** same RTL and settings, only the tile size changed: 6x4 run [42869614360]: slow-corner flip-flop setup margin +1.329 ns, 3 max-slew pins at the slow corner, global placement needed 4 routability iterations (weighted congestion 1.0529); 8x4 run [43501440116]: +2.658 ns, 0 max-slew pins, 1 iteration (0.9653), placement utilisation 0.229. The two 6x4 runs of the same RTL gave identical values, so the difference is the tile, not run-to-run noise.
- **Decision:** 8x4 (`info.yaml`).
- **Would change it:** nothing in the rules; 8x4 is the maximum.

## 3. Program and data memory: two SRAM macros, 512 x 16 program and 1,024 x 16 data

- **Question:** the USB device and 10BASE-T stretch goals need a USB device program with its descriptors and room for one 1,518-byte Ethernet frame (759 words). What holds the program and the data of the four threads?
- **Options:** the 128 x 16 latch array of the earlier decision (below); a larger latch array (256 x 16 was measured: +50 % synthesis area and a layout run stopped by the clock-gate tool defect, below); the IHP SRAM macros of the PDK: 512 x 16 (program) + 1,024 x 16 (data), or 2 x 512 x 16.
- **Evidence:** layout runs of the two macros at their planned places with every input from a flip-flop (research branch `a1-sram-smoke-b`): with the stripe pitch of entry 19 the power-grid check passes with the checker fatal, LVS 0, routing DRC 0, antenna 0 (run [43898966408]); the macro input pins at first had 29 slow-corner max-slew violations and one A_DOUT max-capacitance violation (liberty limits 0.595 ns and 0.064 pF), which the resizer's slew and capacitance margins did not change (run [43925649736]); a 150 um wire-length limit in both design repairs removed them at all corners (run [43941919810]); with the data macro at x = 348.24 um the layout precheck passes (run [43962985880]). 2 x 512 x 16 gave the same grid and LVS results with 10 slew pins (run [43899055122]). In the chip (research branch `a1-int`): `src/pe_sram.v`, one access per cycle, the request from flip-flops 3 cycles before the slot ([docs/isa.md](isa.md) 3.3); ST and STX take 2 slots, the store-read rule is 5 cycles; `formal/sram.sby` checks that a read returns the last word written to its location through both macros and the address alias; `formal/core.sby` proves the fetch timing (B3, B6) and the store request (W1-W3) by k-induction; every RTL, model and firmware test passes, among them the lockstep tests and the 16 protocol programs on the RTL. Synthesis: the 97,044 um² latch memory is replaced by 124,983 um² of macros (LEF areas); 512 FIFO latches stay. The 4-word host FIFOs keep the latch cells of `src/pe_latch_ram.v`, which `formal/mem*.sby` still proves.
- **Decision:** 512 x 16 program memory and 1,024 x 16 data memory (macros `RM_IHPSG13_1P_512x16_c2_bm_bist` and `RM_IHPSG13_1P_1024x16_c2_bm_bist`).
- **Would change it:** a failing precheck or power-grid check of the full chip with the macros; then 2 x 512 x 16 (no 1,518-byte frame buffer).

### 3 (earlier). Program and data memory: 128 x 16 latch array

- **Question:** what holds the program and the data of the four threads?
- **Options:** a latch array of standard cells; an SRAM macro; flip-flops.
- **Evidence:** `src/pe_latch_ram.v`. Synthesis: 2,560 latches (2,048 memory bits and 512 host FIFO bits, which use the same latch array) and 160 clock gates. `formal/mem_full.sby` proves the memory properties at 128 x 16 by k-induction. The four full protocol programs use 103 of the 128 words (`test_fw_full_word_count`: UART TX 15, UART RX 13, SPI controller 26, I2C controller 49), or 114 words with the slow UART receiver.
- **Size, measured after the rule change (8x4 tiles):** 256 x 16 latches (research branch `m3c-mem256`): synthesis 325,747 um2 against 216,632 um2 (+50 %), placement utilisation 0.357; the layout run fails at the disconnected-pins check (the clock input of 2 of the 288 clock-gate cells is not connected) after 2 h 17 min (run [43623912585]). The step netlists show where: the clock-gate cell keeps its clock connection up to the floorplan and loses it in the clock tree synthesis step (`35-openroad-cts`). This matches the public OpenROAD defect [#9816](https://github.com/The-OpenROAD-Project/OpenROAD/issues/9816) (clock tree synthesis leaves clock-gate clock pins unconnected when cloned clock gates share a location; fixed upstream by [PR #9864](https://github.com/The-OpenROAD-Project/OpenROAD/pull/9864), 27 March 2026). A 128-word research run with a design-repair fanout limit of 8 stopped the same way (run [43688791463]), so the failure is a tool defect that some netlists trigger, not an RTL fault; the flow's disconnected-pin check stops every such run; the memory proof at 256 x 16 did not finish in 1,800 s (128 x 16: 587 s). The IHP SRAM macro `RM_IHPSG13_1P_256x16_c2_bm_bist` (IHP-Open-PDK, LEF) is 236.8 x 118.78 um = 28,127 um2 for 256 x 16, with VDD, VDDARRAY and VSS on Metal4 and a BIST port; no run with it exists. A public walkthrough for these macros on a CMOS5L tile (published by another entry, 29 September 2026) lists the integration work: the read data is registered (one cycle of latency, while the barrel fetches an instruction in every cycle); a black box for synthesis and the behavioural model in RTL and gate-level simulation; a `MACROS` block with the fast library characterized at -55 °C mapped onto the flow's -40 °C corner; power stripes moved onto the macro's Metal4 columns (`FP_PDN_VPITCH` 44.96 um against the template's 50.0, a key below the template's "DO NOT CHANGE" line, and a custom stripe step); Magic DRC overlaps waived with KLayout DRC on; antenna diodes; and two marker layers that the precheck must accept. Every program in `fw/` fits alone in 128 words; the largest is the CAN node (123 words, `test_flex_word_count`). Running more programs at the same time is "a plus but not required" (competition FAQ).
- **Decision:** 128 x 16 latch array; no macro in the flow.
- **Would change it:** a demonstration that needs more than 128 words at the same time, with a passing layout run of the larger memory (the macro is the smaller option by area).

## 4. System clock: 60 MHz

- **Question (again, 8x4 and the template main flow):** the competition FAQ says 60 MHz is accepted by the board. Does the design close at 60 MHz?
- **Evidence:** 8x4, template main flow, timing-driven placement with the place-and-route constraints of entry 5 (d), CLOCK_PERIOD 16.667 ns: slow-corner flip-flop setup margin +3.687 ns, no setup, hold, max-slew or max-capacitance violation at the three corners, precheck and gate-level test pass (run [43623356570]). With the default placement at 60 MHz the margin was -0.675 ns (run [43506715397]). The limit for the change was +1.1 ns, the largest margin change measured between synthesis runs of the same design. Protocol limits at 60 MHz (`test_r2_clock_margins`, `test_r2_host_sck_clocks`): UART receiver at 1 Mbit/s -4.0 / +3.7 %, I2C Fast-mode 389.6 kHz, all four example protocols together at a UART sender error of +/-3.3 %, host SCK 10 MHz with 22.6 ns margin ([docs/host_port.md](host_port.md) section 11). At 60 MHz a low-speed USB bit is 40 cycles and a 10BASE-T half bit 3 cycles, both exact. With the SRAM memory (entry 3) and the line helpers (entry 18): first -1.449 ns at the slow corner, on a path from the phase register of the 10BASE-Te receive loop through its two loop steps of one cycle (run [43960974109]); with at most one input edge used per both-edge cycle (a pulse of half a cycle, 8.3 ns, is no 10BASE-T symbol; [docs/helpers.md](helpers.md)) the second step's edge logic runs in parallel with the first, and the margin is +1.308 ns slow, +4.054 ns typical, +4.323 ns fast, with no violation at the three corners (run [43966684593]).
- **Decision:** 60 MHz declared and timed. 50 and 40 MHz stay host options with their own constants.
- **Would change it:** a slow-corner margin under +1.1 ns or any violation on a later RTL version at 60 MHz.

The record below is the earlier decision (6x4, before the rule change).

### 4 (earlier). System clock: 50 MHz

- **Question:** which clock is declared and timed?
- **Options:** 40, 48, 50 and 60 MHz, or one timed clock with a host table of firmware constants for other clocks.
- **Evidence:** at 50 MHz the slow-corner flip-flop setup margin of the present design is +1.329 ns (run [42744631446]). At 60 MHz, on the RTL of October 2026: −1.773 ns with 34 failing endpoints (run [40967811920]); +0.344 ns with timing-driven placement, but with max-slew violations (run [41032654188]). Every clock-dependent firmware value is a host constant ([docs/host_port.md](host_port.md) section 11). `test_r2_clock_margins` and `test_r2_host_sck_clocks` check the limits at 40 and 50 MHz. A 10BASE-T half bit (50 ns) is 2 clock cycles at 40 MHz and 2.5 cycles at 50 MHz (calculation).
- **Decision:** 50 MHz declared and timed. A host may run the chip at 40 MHz for 10BASE-T with its own constants, within the limits of docs/host_port.md section 11.
- **Would change it:** a slow-corner margin at 60 MHz that is positive with the default placement and has no slew violation.

## 5. Slow-corner setup margin

- **Question:** how do the longest flip-flop paths meet setup at the slow corner?
- **Options:** (a) register selects decoded one cycle early, default placement; (b) synthesis delay strategy (`SYNTH_STRATEGY` "DELAY 1"); (c) timing-driven placement; (d) timing-driven placement with a 1.2 ns max-transition limit in the place-and-route constraints.
- **Evidence:** (a) +1.329 ns on the present design (run [42744631446]). On the RTL of October 2026: (b) −0.862 ns, 67 failing endpoints (run [40967845275]); (c) +3.478 ns, but 12 max-slew violations (run [40967879447]); (d) +2.620 ns, hold +0.072 ns, no slew violation (run [41856662241]; the run is marked failed only because of its viewer job). The signoff constraints [src/signoff.sdc](../src/signoff.sdc) report flip-flop paths and latch paths in separate groups. Latch paths borrow time and show a slack of 0.000, so without the groups the flip-flop margin is not visible.
- **Decision (6x4, 50 MHz):** (a). (d) is the reserve, with the trigger rule in [docs/tapeout_checklist.md](tapeout_checklist.md) section 5.
- **Decision (8x4, template main flow, 60 MHz):** (d) is now the placement ([src/config.json](../src/config.json) `PL_TIMING_DRIVEN`, [src/pnr.sdc](../src/pnr.sdc)). On the template main flow without it, 4 max-capacitance pins remain at the slow corner (worst 0.342 pF on a 0.300 pF limit, run [43623251757]); a design repair after global routing leaves 2 (run [43623313712]); with (d): none, and +3.687 ns at 60 MHz (run [43623356570]).
- **Would change it:** a slow-corner flip-flop margin below the threshold of that rule, or max-slew or max-capacitance violations with (d).

## 6. Flow settings and run time

- **Question:** the default flow on this design took 5 h 52 min (run [40793488418]), and a second run was cancelled at the 6 h job limit after 6 h 01 min (run [40846505319]). How does the layout run finish reliably?
- **Options:** default settings; setup slack margin 0 and 4 OpenROAD threads, with Magic DRC off in development runs only.
- **Evidence:** with margin 0, 4 threads and Magic DRC on, the layout job took 1 h 47 min (run [41380161509]). Its `final/metrics.csv` has the same values as the default-flow run 40793488418 in every row: cell count, area, utilisation, wire length, vias, hold buffers, hold slack, power. Magic DRC 0, LVS 0. Only two flow warnings differ. The 6x4 design took 47 min 53 s with Magic DRC off (run [42744631446]). The present 8x4 design: 44 min with Magic DRC off (run [43712217729]); 1 h 25 min with it on, Magic DRC 0 and every other signoff value the same (run [43731920994]).
- **Decision:** `PL_RESIZER_SETUP_SLACK_MARGIN` 0 and `OPENROAD_THREADS` 4 in [src/config.json](../src/config.json). Magic DRC stays off in development runs. It was planned for the submitted run; with the SRAM macros the DRC signoff is the precheck's KLayout deck instead (entry 20).
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

## 10. I2C input spike filter: 5 samples, on the I2C pins only

- **Question:** I2C Fast-mode inputs must suppress spikes shorter than 50 ns (NXP UM10204 Rev. 7.0, Table 10, tSP, page 43, and its note [8], page 44). How many equal samples, and on which pins?
- **Options:** 3, 4 or 5 equal samples; a filter on `uio[6]`, `uio[7]` only, or on all 13 inputs.
- **Evidence:** a filter of K equal samples removes every spike shorter than (K - 1) clock periods. At the declared 60 MHz: K = 4 removes spikes shorter than 50.0 ns, exactly tSP with no margin; K = 5 removes spikes shorter than 66.7 ns, 16.7 ns of margin (at 50 MHz: 80 ns; [docs/isa.md](isa.md) section 6.1). `test_core_spike_filter` shows in RTL, cycle by cycle against the model, that pulses of 1 to 4 cycles have no effect and that longer pulses reach the input 7 cycles after the pin edge. `test_fw_full_i2c_spikes` checks correct Fast-mode transfers with spikes of 1 to 4 samples on SCL and on SDA at 50 and 40 MHz; `test_rtl_i2c` runs `fw/i2c_ctl.s` on the RTL at 60 MHz with 3-sample spikes on SCL and SDA. `test_m3_i2c_filter_k` compares 1, 3 and 4 samples in the model (3 samples let a 3-sample spike through). The earlier 4-sample decision was made at 50 MHz, where it gave 60 ns; the move to 60 MHz (entry 4) removed its margin. A filter on every input would add 5 cycles of delay to the UART and SPI inputs, which do not need it.
- **Decision:** 5 samples, on `uio[6]` and `uio[7]` only.
- **Would change it:** a protocol on `uio[6]` or `uio[7]` with pulses shorter than 5 clock cycles.

## 11. SPI target firmware: one wait per bit

- **Question:** how fast can a thread act as an SPI target?
- **Options:** wait for the sample edge only and set the next MISO bit after it; wait for both SCK edges.
- **Evidence:** `test_m3_spi_tgt` runs `fw/spi_tgt.s` in all four modes against an SPI controller model that does not use the firmware or the processor model. Smallest SCK half period: 25 cycles, so up to 1.2 MHz at the declared 60 MHz (1 MHz at 50 MHz). Waiting for both edges needs 49 cycles, 612 kHz at 60 MHz ([docs/host_port.md](host_port.md) section 13). `test_rtl_spi_tgt` runs the firmware on the RTL at 60 MHz in all four modes at 1 MHz (half period 30) with the host port loading it. The limit comes from the instruction rate (one instruction every 4 cycles, lead 4), not from the pins. An earlier estimate of about 5 MHz did not hold.
- **Decision:** one wait per bit, rated 1.2 MHz at 60 MHz.
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
- **Evidence:** 22 tests pass at the slow, typical and fast corners with cell delays from the netlist of gds run 41404293323 (runs [41871325339] and [41924796459]). That netlist is the 6x4 design of October 2026 timed at 50 MHz; the SDF job has not been run on an 8x4 netlist at 60 MHz, so this entry is evidence for that earlier design only. `test_r3_sdf_delay` fails if no delay is applied. Icarus Verilog applies the delays but runs no timing checks, and it stops on the wire-delay lines, so those are not loaded. `test_counter_sequence` alone takes 17,275 s at the slow corner.
- **Decision:** the SDF job ([.github/workflows/sdf.yaml](../.github/workflows/sdf.yaml)) runs on the final gds run, by hand or after a tape-out tag, not on every push. Static timing stays the setup and hold check.
- **Would change it:** a simulator that runs the timing checks of the cell models.

## 14. Layout flow: the template main branch

- **Question:** the competition rules now name the template main branch, which targets CMOS5L with `tt-gds-action@ttihp27a` (LibreLane 3.1). Keep the earlier `@ihp-cmos5l` flow or move?
- **Options:** keep `@ihp-cmos5l`; move to `@ttihp27a` as is; move with the lint and capacitance items fixed.
- **Evidence:** on the same RTL the main flow gives +3.673 ns at 50 MHz (run [43623251757], against +2.658 ns on `@ihp-cmos5l`, run [43501440116]) but reports 4 lint warnings (PINMISSING: the power pins of the two instantiated cells in `src/pe_cells.v`, which the power grid connects) and 4 max-capacitance pins (run [43506844154]). With a lint waiver local to the two cell instances: 0 lint warnings (run [43623251757]). The capacitance pins: entry 5. The part of `src/config.json` below "DO NOT CHANGE ANYTHING BELOW THIS POINT" is identical on the template main branch and the earlier template branch (compared 13 November 2026); our keys are above that line. The SDF job installs the PDK revision of the layout run (`tt_submission/pdk.json`), as the main-branch gate-level test does.
- **Decision:** main flow (`@ttihp27a`) for every job, with the lint waiver and the placement of entry 5 (d).
- **Would change it:** a later template change; compare before the submitted run ([docs/tapeout_checklist.md](tapeout_checklist.md) section 3).

## 15. Hold-out trials: protocols outside the design

- **Question:** the judges ask first whether the chip "can be reprogrammed to support protocols it wasn't designed for". How is that shown, not only claimed?
- **Options:** firmware for protocols named on the competition page (CAN, JTAG, SWD, PS/2: written after the instruction set, but known when it was made); hold-out trials: protocols not named on the page or in the design documents, written from their public documents on the unchanged instruction set, assembler and host port, tested against device models written from the same documents.
- **Evidence:** 1-Wire controller (`fw/onewire.s`, 40 words), from Analog Devices AN126 and the DS18B20 data sheet: correct at all five device-timing corners at 50 and 60 MHz (`test_holdout_onewire`). The trial found two real limits: the application note's write-0 low time (60 us) ends exactly at the end of the DS18B20 sample window, so it was set to 65 us; and at 60 MHz the reset needs the presence sample to be issued after the release, because the event lead is limited to 32,767 cycles (546 us at 60 MHz). Words: next to any three of the four example programs, not next to all four (143 > 128, `test_holdout_onewire_words`). Two more trials, chosen by a rule a reader can repeat (the first two entries of the sigrok libsigrokdecode decoder list that decode a logic-level link between two devices, are not in `fw/` and not on the competition page, can be driven from 3.3 V pins with external parts, allow a bit time of 1 us or more, and have a public specification): AM2302 sensor reader (`fw/am2302.s`, 56 words, AM2302 data sheet), correct with all sensor times 0.8 to 1.2 times the data sheet at 50 and 60 MHz (`test_holdout_am2302`); AVR XMEGA PDI programmer (`fw/pdi.s`, 52 words, XMEGA AU manual section 32), correct at 6 device corners with KEY, control registers, data access, REPEAT, a parity error and BREAK recovery (`test_holdout_pdi`), PDI_CLK up to 1.25 MHz at 60 MHz (`test_holdout_pdi_rate`). Neither needed a hardware change; the gaps were the 4,095-cycle `TADD` immediate (two `TADD`) and the missing parity instruction (the host makes the parity bit).
- **Decision:** hold-out trials are part of the flexibility evidence. The trial protocols are chosen by a stated rule and recorded with words, rate, limits met and effort.
- **Would change it:** a trial that cannot be done without a hardware change; that is the evidence the trial exists to find.

## 16. Instruction-set gaps found by the protocol firmware: no change in this version

- **Question:** the CAN, JTAG, SWD and PS/2 firmware found gaps: no 16-bit XOR immediate (CRC), no parity instruction, `TADD` adds only, `SAMPLE` takes 2 slots, no shift by a register, no `WAIT` that also ends on a host word. Add instructions?
- **Evidence (benefit by calculation from the measured cycle and word counts):** XOR immediate: CAN -3 words and -12 cycles on half of the CRC bits, still not 500 kbit/s; parity and shift by a register: 0 words (the host does the work today); `TADD` subtract: 0 words (the firmware works around it); one-slot `SAMPLE`: JTAG 1.25 to 1.39 MHz, SWD 1.56 to 1.79 MHz at 50 MHz, the same cycle counts give JTAG 1.5 to 1.67 MHz and SWD 1.875 to 2.14 MHz at the declared 60 MHz (`test_rtl_jtag` and `test_rtl_swd` run the present firmware on the RTL at 60 MHz at 1.5 MHz and 1.875 MHz); host-word `WAIT`: removes the PS/2 4.6 us set-up limit. Cost of each: instruction encoding, assembler, model, RTL, lockstep tests, formal proofs and a new layout run.
- **Decision:** no instruction-set change in this version. The page sets no speed requirement, and each benefit is small against a full verification cycle.
- **Would change it:** a protocol that cannot run at a standard setting without one of these; the one-slot `SAMPLE` is the first candidate.

## 17. Clock tree and max fanout

- **Question:** the signoff timing reports 84 pins above the max fanout of 8 at each corner (run [43672052931]): 80 leaf clock buffers with 10 to 17 sinks, the clock root with 16, and 4 placement buffers with 9 to 12 loads. The limit 8 is `default_max_fanout` of the CMOS5L standard-cell library; the flow's own constraint is 10 (`MAX_FANOUT_CONSTRAINT`). Slew and capacitance of all 84 pins were within limits. Clean it, or record why not?
- **Options and evidence (same RTL, 60 MHz, one setting changed per run):**
  - (a) default clock tree: 84 pins; fast-corner hold slack +0.010 ns; slow-corner flip-flop setup +3.687 ns; 6.66 mW; antenna 0 (run [43672052931]).
  - (b) at most 8 sinks per leaf clock buffer (`CTS_SINK_CLUSTERING_SIZE` 8): 7 pins (clock root 16 and 6 data nets); hold +0.097 ns; +3.990 ns (run [43688768543]).
  - (c) (b) and a clock trunk buffer every 200 um (`CTS_DISTANCE_BETWEEN_BUFFERS` 200): 8 pins, none on the clock tree; hold +0.099 ns; +3.841 ns; 7.55 mW (+0.90 mW); standard-cell area +5,015 um2 (+1.7 %); antenna 0; precheck and gate-level test pass (run [43693862885]). Each of the 8 nets drives at most 8 logic inputs; the extra loads are antenna diodes that the router adds on long wires (step netlists of the run traced on the runner). The diode capacitance is part of the max-capacitance and max-slew checks, which pass.
  - (d) (c) with antenna repair by layer jumpers only, no diodes: 0 fanout pins, but 15 nets violate the antenna rule (run [43705472044]).
  - (e) a design-repair fanout limit of 8 (`MAX_FANOUT_CONSTRAINT` 8), with (b) or (c): three runs stop at the disconnected-pins check; clock tree synthesis leaves the clock input of one clock-gate cell unconnected (runs [43688791463], [43698799514]; the tool defect of entry 3).
- **Decision:** (c), in [src/config.json](../src/config.json). The antenna rule protects gate oxide during manufacturing and is not traded for a fanout count; the 8 counted pins are listed in [docs/signoff.md](signoff.md) with their logic fanout.
- **Would change it:** a flow version without the clock-gate defect, so that (e) can run; or a slew or capacitance violation on one of the 8 nets.

## 18. Line helpers: a dedicated USB low-speed helper and a dedicated 10BASE-Te helper

- **Question:** a thread executes one instruction every 4 cycles, too slow for the bit-level work of USB low speed (NRZI, stuffing, CRC at 1.5 Mbit/s) and 10BASE-T (Manchester at 10 Mbit/s, CRC-32, clock recovery on both clock edges). What does that work?
- **Options:** two general serial line units (one RTL with a writable configuration for USB, 10BASE-T, UART, clocked lines); two dedicated helpers (the same RTL with the configuration fixed for USB low speed and for 10BASE-Te; only the pin choice writable).
- **Evidence:** synthesis at the typical corner (Yosys, `stat -liberty`): general unit 102,201 um² (96,345 um² without the 10BASE-T blocks), dedicated USB helper 39,808 um², dedicated 10BASE-Te helper 52,161 um²; the receive loop alone is 30,061 um², because both-edge sampling needs two loop steps per cycle. The acceptance limit written before the measurement was 25,000 um² per general unit, and above 50,000 um² the dedicated form. The general units would have given faster firmware protocols (UART, SPI, JTAG, CAN rates by calculation), which are therefore not taken. Function: [docs/helpers.md](helpers.md) section 4 (unit tests against references written from USB 2.0 and IEEE 802.3, and chip tests with firmware through the I/O ports, the pins and the data memory). The receive loop equals a bit-exact model in every cycle; that model gives the same decided bits as the clock-recovery study's loop on the study's sampled streams.
- **Decision:** the two dedicated helpers, on I/O ports 16 to 47.
- **Would change it:** a general unit with the same functions at 50,000 um² or less.

## 19. Power grid over the SRAM macros: stripe pitch 67.44 um

- **Question:** a tile has one stripe layer (Metal4), the macros have their supply pins as Metal4 columns 11.24 um apart (VDD and VSS alternate), and the precheck forbids the layer above. How do the stripes reach the macros?
- **Options:** keep the template pitch (50.0 um, a key below the template's "DO NOT CHANGE" line) with a later step that moves or removes the stripes over the macros and the power-grid check off; set the pitch to 6 x 11.24 = 67.44 um so that every stripe over a macro lies in a supply column of its own net, with a stripe step that keeps the stripes full height and a check of every stripe over a macro.
- **Evidence:** 50 um is not a multiple of 11.24 um (50 mod 11.24 = 5.04, calculation), so with the template pitch successive stripes land on columns of the other net; no macro position puts every crossing stripe on a column of its own net. With 67.44 um and `src/pdn_cfg.tcl`: the power-grid checker passes with its error fatal in every SRAM run (runs [43898966408], [43941919810], [43962985880]), LVS 0, and the precheck passes once no channel stripe is left between the macros (run [43962985880]; with the data macro at 280.80 um a short channel stripe failed the precheck's supply-port rule, run [43958374639]). The precheck reads the GDS, the LEF and the netlist, not the configuration.
- **Decision:** `FP_PDN_VPITCH` 67.44 with `FP_PDN_VSPACING` 3.52, `FP_PDN_VOFFSET` 25.40 and the stripe step of `src/pdn_cfg.tcl`; macros at x = 11.04 + 67.44 k um.
- **Would change it:** a template rule that the pitch key must not change; then the template pitch with a stripe-removal step written for this project.

## 20. DRC with the SRAM macros: the precheck's KLayout deck

- **Question:** the tape-out checklist turned Magic DRC on for the submitted run. Does Magic DRC work with the SRAM macros?
- **Evidence:** with the macros, Magic reports 173,163 and 173,165 errors (runs [43925649736], [43941919810]); the listed shapes are of the rule "N-Diffusion spacing to N-well in SRAM < 54 (NW.d exception)" at the macro edges, inside the IHP macro layout. The precheck's KLayout DRC with the PDK deck (sg13cmos5l, table main) passes on the same layout (run [43941919810], precheck log) and on the later smoke layout (run [43962985880]).
- **Decision:** the KLayout DRC of the layout precheck is the DRC signoff; Magic DRC stays off ([docs/tapeout_checklist.md](tapeout_checklist.md)).
- **Would change it:** a Magic rule set that reads the SRAM exception layers.

## 21. Setup margin of the resizer with the SRAM layout

- **Question:** three layout runs of nearly the same netlist gave a slow-corner flip-flop setup margin of +1.308, +0.740 and +0.487 ns at 60 MHz (runs [43966684593], [43983798247], [43989356298]), below the +1.1 ns of entry 4. In each run the worst path starts at the core's register select (`u_core.sa`), and the hold repair has put a 0.69 ns delay cell right after that flip-flop, on the net that also feeds the register-read multiplexers of the ALU path. How does the margin come back?
- **Options:** keep the flow and accept the margin; change the RTL so that the short paths and the ALU path start at different select flip-flops; give the post-CTS resizer a setup margin (`PL_RESIZER_SETUP_SLACK_MARGIN`), so that its hold repair keeps that much setup slack on the paths it delays. A margin above 0 made the resizer loop for hours on the latch endpoints when the memory was a latch array (2,560 endpoints that borrow and report 0.000, run [40793488418]); the 512 FIFO latches would do the same, so their D pins leave the place-and-route setup checks ([src/pnr.sdc](../src/pnr.sdc)), while the signoff constraints check them as before.
- **Evidence:** the same RTL (commit 9d3532d) with margin 1.5 ns and the latch D pins out of the place-and-route setup checks: +1.682 ns slow, +4.311 ns typical, +4.491 ns fast; hold +0.582 / +0.272 / +0.099 ns; no setup, hold, slew, capacitance or fanout violation at the three corners; latch paths 0.000 by borrowing as before; layout job 40 min; precheck pass (research run [43998187307]). The run without the change, the same RTL: +0.487 ns (run [43989356298]).
- **Decision:** `PL_RESIZER_SETUP_SLACK_MARGIN` 1.5 with the latch D pins out of the place-and-route setup checks.
- **Would change it:** a run that loops in the resizer, or a margin under +1.1 ns again; then the RTL change of the second option.

## Claims with no public evidence yet

- Pad electrical limits (I2C low-level output voltage, 5 V tolerance, Fast-mode Plus): the evidence is now public. The pad SPICE netlists are in `spice/` and the results in [verification.md](verification.md) section 9: Standard-mode and Fast-mode output low level met at every corner, Fast-mode Plus not met, not 5 V tolerant. The chip claims I2C Standard-mode and Fast-mode only and makes no 5 V claim.
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
[42869614360]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/42869614360
[43501440116]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43501440116
[43506715397]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43506715397
[43506844154]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43506844154
[43623251757]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43623251757
[43623313712]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43623313712
[43623356570]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43623356570
[43623912585]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43623912585
[43672052931]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43672052931
[43688768543]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43688768543
[43688791463]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43688791463
[43693862885]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43693862885
[43698799514]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43698799514
[43705472044]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43705472044
[43712217729]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43712217729
[43731920994]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43731920994
[43898966408]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43898966408
[43899055122]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43899055122
[43925649736]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43925649736
[43941919810]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43941919810
[43958374639]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43958374639
[43962985880]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43962985880
[43960974109]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43960974109
[43966684593]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43966684593
[43983798247]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43983798247
[43989356298]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43989356298
[43998187307]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43998187307
