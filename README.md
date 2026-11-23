[![gds](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/gds.yaml/badge.svg?branch=cmos5l)](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/gds.yaml?query=branch%3Acmos5l)
[![test](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/test.yaml/badge.svg?branch=cmos5l)](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/test.yaml?query=branch%3Acmos5l)
[![formal](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/formal.yaml/badge.svg?branch=cmos5l)](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/formal.yaml?query=branch%3Acmos5l)
[![docs](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/docs.yaml/badge.svg?branch=cmos5l)](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/docs.yaml?query=branch%3Acmos5l)

# Programmable protocol emulator

This chip is a small 4-thread processor that makes serial protocols (UART, SPI, I2C, CAN, JTAG and more) in firmware, with each pin change at an exact clock cycle. Two line helpers do the bit-level work of USB low speed and 10BASE-T at line rate, so a thread can send and receive their packets and frames. It is a design for the TTIHP27a shuttle on the IHP SG13CMOS5L 130 nm process: 8x4 tiles, 60 MHz.

**In short:** every protocol is firmware on four threads whose pin changes happen at exact clock cycles; with its two line helpers the same chip is a USB low-speed device that enumerates on its own and a half-duplex 10BASE-T station with link pulses and the link integrity test, at its declared 60 MHz. The layout has no timing or design-rule violation at any corner, and every protocol is checked on the RTL and on the layout netlist against a model written from the standard, not from the design (logic simulation; the transceivers and line parts are board items, [info.md](docs/info.md)).

**Start here:** the [verification start page](https://soundgod10-droid.github.io/protocol-emulator-asic/start/) gives every check with its newest run and what it does not check (generated from the CI runs on every site build); the [guide for reviewers](docs/guide.md) maps each claim to its evidence, the CI run that shows it and the command that repeats it.

Documentation site: **<https://soundgod10-droid.github.io/protocol-emulator-asic/>**

Simulator in the browser: **<https://soundgod10-droid.github.io/protocol-emulator-asic/sim/>**. It runs 23 example setups (UART, SPI, I2C, CAN, JTAG, SWD, PS/2, 1-Wire, AM2302, PDI, four protocols at once) on the chip's reference model, the Python model that the RTL is checked against in every clock cycle, with the device models of the tests. Edit the firmware, step through the four threads, and see the pins and the decoded waveforms. CI checks that every example gives the same trace in the browser as in the test environment. The recorded pins of the RTL and gate-level tests are on the [waveforms page](https://soundgod10-droid.github.io/protocol-emulator-asic/sim/wave/demo.html).

![Final layout of the chip](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/layout.png)

*The final layout of the 8x4 tile (1724 x 711 um), from the GDS of the newest gds run. The white line is the tile outline. The two SRAM macros (program and data memory) are near the bottom edge; the standard cells use 40.0 % of the tile, and filler cells under the power grid fill the rest. Made in CI by [figures/layout.py](figures/layout.py) (KLayout, PDK layer colours). The same layout in 3D: [layout viewer](https://soundgod10-droid.github.io/protocol-emulator-asic/viewer/).*

![Block diagram of the chip](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/block_diagram.svg)

*Block diagram. Made in CI by [figures/blocks.py](figures/blocks.py) from the module hierarchy of the RTL; the counts come from the RTL.*

## What it does

- **Protocols are firmware.** The chip has no UART, SPI or I2C block. A host loads a program through an SPI port, and the program makes every pin waveform. A new protocol needs new firmware, not new silicon. `fw/` has programs for UART, SPI, I2C, CAN, JTAG, SWD, PS/2, 1-Wire, the AM2302 sensor, AVR PDI, a timed trigger, a USB low-speed device (HID keyboard) and a half-duplex 10BASE-T station.
- **Four threads in fixed slots.** Thread n executes one 16-bit instruction in each clock cycle c with c mod 4 = n. A waiting thread keeps its slot. Thus the timing of one thread does not change with the other three.
- **Timed output events.** `OUT` does not change the pin at once. It writes an event for a set cycle into the slot of that pin, and the pin changes in exactly that cycle. A late event still occurs and sets a LATE flag for the host.
- **Timed inputs.** `SAMPLE` reads a pin at a set cycle. `WAIT` stops the thread until an edge and gives the cycle of the edge.
- **Pins for real buses.** The host gives each output pin to one thread or to a line helper. Pins can be push-pull or open-drain. Two inputs have a spike filter for I2C.
- **Line helpers for USB low speed and 10BASE-T.** A USB low-speed helper (NRZI, bit stuffing, CRC5 and CRC16, end of packet, bus reset and suspend timers) and a 10BASE-Te helper (Manchester, receive clock recovery on both clock edges, CRC-32, link pulses, squelch, collision and jabber) on I/O ports. A thread gives and takes 16-bit words and starts a frame at a cycle that it chooses ([helpers](docs/helpers.md)). On them, firmware does the rest of each standard on the chip: USB enumeration, control transfers and the HID class; the 10BASE-T link integrity test, link pulses, collision backoff and frame checks ([protocols](docs/protocols.md)).
- **Memory and host port.** Two SRAM macros: 512 words of 16 bits for the programs of all threads, 1,024 words for frames and tables. The host SPI port (mode 0, up to 10 MHz) loads the memory, starts and stops the threads, and moves data through a 4-word FIFO in each direction for each thread.
- **Status.** The layout passes all checks of the shuttle's layout precheck. No chip is made yet.

## Flexibility

Can the chip be reprogrammed for protocols it was not designed for? The evidence, and how it will grow:

| Evidence | What | Where |
|---|---|---|
| Counting rule | one protocol per specification (roles, rates and outline programs are not counted again); the strict count needs a program that fits the memory alone, a test model written from a named public document, and a pass on the RTL at 60 MHz and on the layout netlist | [counting.md](docs/counting.md) |
| Count today | the newest strict and broad counts with every program, its family, tier and evidence, and a check a reader can do in five minutes; generated from the count job of the newest test run, so no number is typed here | [Protocol count](https://soundgod10-droid.github.io/protocol-emulator-asic/count/) page; the run's annotation "protocol count table" |
| Written after the instruction set was fixed | CAN, JTAG, SWD, PS/2: named on the competition page, so known when the instruction set was made; each with what the instruction set lacked | [protocols.md](docs/protocols.md), Flexibility |
| Hold-out trials | 1-Wire, AM2302, AVR XMEGA PDI: not named on the page or in the design documents; AM2302 and PDI chosen by a rule a reader can repeat (the first two eligible entries of the sigrok decoder list); written on the unchanged chip | [protocols.md](docs/protocols.md), Hold-out trials |
| Outside decoders | sigrok decoders of a pinned build read the chip pins of the RTL tests: 19 of 19 scenarios agree, each with a negative control that must fail | [sigrok run 44557588966][sigrok] |
| Census on the frozen chip | every one of the 44 pin-level protocols of an outside catalogue (sigrok decoders minus stated exclusions), in an order set by a public random beacon (drand round 34763812, 2026-12-12 17:00 UTC), after the `rtl-freeze` tag (due 2026-12-11), with separate writers for the limits, the device model and the firmware, a 2 h time box per protocol, and a prediction written in advance (32 to 40 of 44 PASS) | [flex/PREREG.md](flex/PREREG.md) |
| Clocked buses without new RTL | the USB helper's clocked and NRZ modes run SPI, JTAG, the SWD clock and UART at up to 15 MHz under firmware control | [protocols.md](docs/protocols.md), Clocked and NRZ buses |

These 7 programs written after the instruction set (the four page-named protocols and the three hold-outs) are "tier 0" of the census (PREREG section 3): they run again, unchanged, on the frozen chip. Limits: one instruction per thread every 4 cycles, so firmware-only rates are modest (CAN 250 kbit/s, SPI target 1.2 MHz); 512 program words, so the USB device (476 words) and the 10BASE-T station (433 words) are loaded one at a time.

## Key results

| Check | Result | Run |
|---|---|---|
| Timing at 60 MHz, setup slack of the flip-flop paths, slow / typical / fast corner | +2.71 / +4.47 / +4.61 ns | [gds 45035665776][gds] |
| Hold slack, slow / typical / fast corner | +0.59 / +0.28 / +0.10 ns | [gds 45035665776][gds] |
| Setup, hold, max-slew, max-capacitance and max-fanout violations | 0 at all 3 corners (each of these checks stops the layout run at any corner) | [gds 45035665776][gds] |
| Routing DRC, LVS, antenna, power grid | 0 / 0 / 0 / 0 errors | [gds 44557589283][gds] |
| Layout precheck of the shuttle (KLayout DRC with the SRAM macros) | pass | [gds 44557589283][gds] |
| Protocols in the strict count ([counting rule](docs/counting.md)) | each on the RTL and on the layout netlist against a model from a named public document; the number is computed by the count job and shown on the [Protocol count](https://soundgod10-droid.github.io/protocol-emulator-asic/count/) page | newest test run (count job) |
| RTL tests (cocotb, Icarus Verilog) | 175 testcases in 14 modules, no failure | [test 44589189363][test] |
| Gate-level tests (layout netlist, functional cell models, no delays) | template test, the protocol programs, the line-helper chip tests, the USB device and the 10BASE-T station tests pass | [gds 44557589283][gds]; the chip-level UART tests: [gds 44589189324][gds2] (same RTL and layout values) |
| USB low-speed device (HID keyboard) and 10BASE-T station firmware with standard-based host and partner models, on the RTL / on the gate-level netlist | 5 + 5 tests / the same 10 tests pass. USB: enumeration with the host at -500, 0 and +500 ppm, reply 5.65 to 5.68 bit times after the host's end of packet (limit 6.5), no reply to 6 damaged packets. 10BASE-T: frames received at -100 and +100 ppm, link test pulses every 16.01 ms (8 to 24 ms), link integrity test, collision backoff | [test 44589189363][test], [gds 44557589283][gds] |
| Clocked and NRZ buses on the USB helper (logic simulation with pad and multiplexer delays as parameters) | through the chip pins, with threads feeding and storing the words: SPI controller 15 MHz (mode 0, MISO checked), JTAG 15 MHz (IDCODE scan), SWD 7.5 MHz (DPIDR read), UART 15 Mbaud both ways; on the RTL, the layout netlist and with SDF cell delays at three corners. SPI 30 MHz and modes 1 to 3: unit level. Every rate with its evidence level: [protocols](docs/protocols.md), Rates | [test 44589189363][test], [protocols](docs/protocols.md) |
| 10BASE-T receive margin of the helper's loop (1,000 frames of 1,518 bytes per case, model) | 1,000 of 1,000 at the 13.5 ns jitter limit, +1.34 ns margin with up to 3 ns pattern-dependent jitter | [helpers](docs/helpers.md) |
| Outside decoders (sigrok, pinned build) on the RTL pins | 19 of 19 scenarios agree, each with a control that must fail | [sigrok 44557588966][sigrok] |
| Code coverage of the RTL tests: line / branch / toggle | 80.4 % / 83.7 % / 79.6 % (every file 100 % line except the helpers; the missed points by helper copy in [signoff](docs/signoff.md) section 4) | [coverage 44589189245][cov] |
| Formal proofs and covers (SymbiYosys) | 27 of 27 jobs give their expected result (12 unbounded proofs, 15 planted faults found) | [formal 44421405029][formal] |

Conditions: static timing of the routed layout at the three corners of the flow (slow 1.08 V 125 °C, typical 1.20 V 25 °C, fast 1.32 V -40 °C). The tests are logic simulations. The gate-level tests use the netlist of the layout with functional cell models and no delays. There is no silicon result yet. The [Results](https://soundgod10-droid.github.io/protocol-emulator-asic/results/) page is made again from the newest runs on every push, with every value and its run.

[gds]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44557589283
[gds2]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44589189324
[test]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44589189363
[cov]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44589189245
[count]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44589189363
[sigrok]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44557588966
[formal]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44421405029

## See it run

These waveforms come from the pins of the RTL tests at 60 MHz (logic simulation, ideal pins). CI makes them on every push with [figures/waves.py](figures/waves.py). Red marks are the limits of the standard or the test; green marks are the measured values. Details are on the [Protocols](docs/protocols.md) page.

![UART transmitter at 115,200 bit/s](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/wave_uart.svg)

*UART transmitter, 115,200 bit/s: every bit is 521 cycles (rate error -0.032 %).*

![SPI controller in mode 0](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/wave_spi.svg)

*SPI controller, mode 0: SCK 1.20 MHz. MISO comes from an independent device model.*

![I2C controller in Fast-mode](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/wave_i2c.svg)

*I2C controller, Fast-mode: fSCL 389.6 kHz. Every bus time is checked against NXP UM10204 Table 11.*

![CAN node at 250 kbit/s](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/wave_can.svg)

*CAN 2.0A node, 250 kbit/s on two threads: every bit is 240 cycles, and the other node acknowledges the frame.*

## Run the tests

You need Python 3.11 and [Icarus Verilog](https://steveicarus.github.io/iverilog/). These commands run the UART transmitter test on the RTL:

```sh
git clone https://github.com/soundgod10-droid/protocol-emulator-asic.git && cd protocol-emulator-asic/test
pip install -r requirements.txt
make -B COCOTB_TEST_MODULES=test_rtl_fw COCOTB_TEST_FILTER=test_rtl_uart_tx
```

`make -B` runs all the tests (the CI test job took 1 h 30 min in run 44589189363 and 1 h 57 min in run 44557589083; runner speed varies). [test/README.md](test/README.md) tells how to run the gate-level tests and how to get the pin waveforms. A formal job: `sby -f formal/<job>.sby`.

## Folders

| Folder | Contents |
|---|---|
| [src/](src/) | RTL of the chip (Verilog); [info.yaml](info.yaml) lists the files of the layout |
| [fw/](fw/) | firmware: the programs for each protocol |
| [asm/](asm/) | assembler for the firmware |
| [model/](model/) | cycle-exact reference model of the chip (Python) |
| [test/](test/) | cocotb tests on the model, the RTL and the gate-level netlist |
| [formal/](formal/) | formal proofs and covers (SymbiYosys) |
| [spice/](spice/) | circuit simulations of the pads and the input synchronizers (ngspice) |
| [figures/](figures/) | scripts that make the figures and the Results page in CI |
| [sim/](sim/) | browser simulator: the page, the example setups, the waveform viewer and decoders, and its build and check scripts |
| [docs/](docs/) | the documents (the pages of the documentation site) |
| [flex/](flex/) | flexibility census: the rules, set before the census, and the protocol list |
| [.github/workflows/](.github/workflows/) | CI: tests, coverage, formal, layout (gds), timing simulation (sdf), FPGA, documentation, browser simulator (sim) |

## Documents

- [Guide for reviewers](docs/guide.md): each claim, its evidence, the run that shows it and how to repeat it
- [Architecture](docs/architecture.md): the blocks, the thread slots and the pipeline, the memory, the pins, clock and reset, and a comparison with other programmable I/O engines
- [Instruction set](docs/isa.md): every instruction and its encoding
- [Host port](docs/host_port.md): the SPI commands, the host rules and the firmware limits
- [Line helpers](docs/helpers.md): the USB low-speed and 10BASE-Te helpers, their ports, pins and measured results
- [Protocols](docs/protocols.md): every firmware program, how it is tested, and the waveforms
- [Verification](docs/verification.md): each verification method, what it checks and what it does not
- [Signoff](docs/signoff.md): each signoff check with its source and result, the open items, the CDC and reset review
- [Decisions](docs/decisions.md): each design decision with the options and the evidence
- [Rationale](docs/rationale.md): why each module, parameter, flow setting and instruction is as it is, with the alternative compared and its numbers
- [Flexibility census](flex/PREREG.md): the rules for the census of protocols on the frozen chip
- [Datasheet](docs/info.md): pins, how to use the chip step by step, and the external parts for each protocol
- [Tapeout checklist](docs/tapeout_checklist.md): the checks before the final layout run
