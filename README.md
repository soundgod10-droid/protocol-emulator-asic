[![gds](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/gds.yaml/badge.svg?branch=cmos5l)](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/gds.yaml?query=branch%3Acmos5l)
[![test](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/test.yaml/badge.svg?branch=cmos5l)](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/test.yaml?query=branch%3Acmos5l)
[![formal](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/formal.yaml/badge.svg?branch=cmos5l)](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/formal.yaml?query=branch%3Acmos5l)
[![docs](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/docs.yaml/badge.svg?branch=cmos5l)](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/docs.yaml?query=branch%3Acmos5l)

# Programmable protocol emulator

This chip is a small 4-thread processor that makes serial protocols (UART, SPI, I2C, CAN, JTAG and more) in firmware, with each pin change at an exact clock cycle. Two line helpers do the bit-level work of USB low speed and 10BASE-T at line rate, so a thread can send and receive their packets and frames. It is a design for the TTIHP27a shuttle on the IHP SG13CMOS5L 130 nm process: 8x4 tiles, 60 MHz.

Documentation site: **<https://soundgod10-droid.github.io/protocol-emulator-asic/>**

Simulator in the browser: **<https://soundgod10-droid.github.io/protocol-emulator-asic/sim/>**. It runs 23 example setups (UART, SPI, I2C, CAN, JTAG, SWD, PS/2, 1-Wire, AM2302, PDI, four protocols at once) on the chip's reference model, the Python model that the RTL is checked against in every clock cycle, with the device models of the tests. Edit the firmware, step through the four threads, and see the pins and the decoded waveforms. CI checks that every example gives the same trace in the browser as in the test environment. The recorded pins of the RTL and gate-level tests are on the [waveforms page](https://soundgod10-droid.github.io/protocol-emulator-asic/sim/wave/demo.html).

![Final layout of the chip](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/layout.png)

*The final layout of the 8x4 tile (1724 x 711 um), from the GDS of the newest gds run. The white line is the tile outline. The two SRAM macros (program and data memory) are near the bottom edge; the standard cells use 39.0 % of the tile, and filler cells under the power grid fill the rest. Made in CI by [figures/layout.py](figures/layout.py) (KLayout, PDK layer colours). The same layout in 3D: [layout viewer](https://soundgod10-droid.github.io/protocol-emulator-asic/viewer/).*

![Block diagram of the chip](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/block_diagram.svg)

*Block diagram. Made in CI by [figures/blocks.py](figures/blocks.py) from the module hierarchy of the RTL; the counts come from the RTL.*

## What it does

- **Protocols are firmware.** The chip has no UART, SPI or I2C block. A host loads a program through an SPI port, and the program makes every pin waveform. A new protocol needs new firmware, not new silicon. `fw/` has programs for UART, SPI, I2C, CAN, JTAG, SWD, PS/2, 1-Wire, the AM2302 sensor, AVR PDI and a timed trigger.
- **Four threads in fixed slots.** Thread n executes one 16-bit instruction in each clock cycle c with c mod 4 = n. A waiting thread keeps its slot. Thus the timing of one thread does not change with the other three.
- **Timed output events.** `OUT` does not change the pin at once. It writes an event for a set cycle into the slot of that pin, and the pin changes in exactly that cycle. A late event still occurs and sets a LATE flag for the host.
- **Timed inputs.** `SAMPLE` reads a pin at a set cycle. `WAIT` stops the thread until an edge and gives the cycle of the edge.
- **Pins for real buses.** The host gives each output pin to one thread or to a line helper. Pins can be push-pull or open-drain. Two inputs have a spike filter for I2C.
- **Line helpers for USB low speed and 10BASE-T.** A USB low-speed helper (NRZI, bit stuffing, CRC5 and CRC16, end of packet, bus reset and suspend timers) and a 10BASE-Te helper (Manchester, receive clock recovery on both clock edges, CRC-32, link pulses, squelch, collision and jabber) on I/O ports. A thread gives and takes 16-bit words and starts a frame at a cycle that it chooses ([helpers](docs/helpers.md)).
- **Memory and host port.** Two SRAM macros: 512 words of 16 bits for the programs of all threads, 1,024 words for frames and tables. The host SPI port (mode 0, up to 10 MHz) loads the memory, starts and stops the threads, and moves data through a 4-word FIFO in each direction for each thread.
- **Status.** The layout passes all checks of the shuttle's layout precheck. No chip is made yet.

## Key results

| Check | Result | Run |
|---|---|---|
| Timing at 60 MHz, setup slack of the flip-flop paths, slow / typical / fast corner | +1.68 / +4.31 / +4.49 ns | [gds 44005215480][gds] |
| Hold slack, slow / typical / fast corner | +0.58 / +0.27 / +0.10 ns | [gds 44005215480][gds] |
| Setup, hold, max-slew, max-capacitance and max-fanout violations | 0 at all 3 corners | [gds 44005215480][gds] |
| Routing DRC, LVS, antenna, power grid | 0 / 0 / 0 / 0 errors | [gds 44005215480][gds] |
| Layout precheck of the shuttle (KLayout DRC with the SRAM macros) | 10 of 10 checks pass | [gds 44005215480][gds] |
| RTL tests (cocotb, Icarus Verilog) | 105 tests, job success | [test 44005215479][test] |
| Line helpers: USB low speed and 10BASE-Te on the RTL / on the gate-level netlist | 15 tests (9 unit, 6 chip) / the 6 chip tests pass | [test 44005215479][test], [gds 44005215480][gds] |
| 10BASE-T receive margin of the helper's loop (1,000 frames of 1,518 bytes per case, model) | 1,000 of 1,000 at the 13.5 ns jitter limit, +1.34 ns margin with up to 3 ns pattern-dependent jitter | [helpers](docs/helpers.md) |
| Code coverage of the RTL tests: line / branch / toggle | 75.6 % / 81.3 % / 77.1 % (every file 100 % line except the helpers, [signoff](docs/signoff.md)) | [coverage 44005215379][cov] |
| Formal proofs and covers (SymbiYosys) | 16 of 16 jobs pass | [formal 44005215486][formal] |

Conditions: static timing of the routed layout at the three corners of the flow (slow 1.08 V 125 °C, typical 1.20 V 25 °C, fast 1.32 V -40 °C). The tests are logic simulations. The gate-level tests use the netlist of the layout with functional cell models and no delays. There is no silicon result yet. The [Results](https://soundgod10-droid.github.io/protocol-emulator-asic/results/) page is made again from the newest runs on every push, with every value and its run.

[gds]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44005215480
[test]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44005215479
[cov]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44005215379
[formal]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/44005215486

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

`make -B` runs all the tests (about 1 hour; the CI test job took 64 min). [test/README.md](test/README.md) tells how to run the gate-level tests and how to get the pin waveforms. A formal job: `sby -f formal/<job>.sby`.

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

- [Architecture](docs/architecture.md): the blocks, the thread slots and the pipeline, the memory, the pins, clock and reset, and a comparison with other programmable I/O engines
- [Instruction set](docs/isa.md): every instruction and its encoding
- [Host port](docs/host_port.md): the SPI commands, the host rules and the firmware limits
- [Line helpers](docs/helpers.md): the USB low-speed and 10BASE-Te helpers, their ports, pins and measured results
- [Protocols](docs/protocols.md): every firmware program, how it is tested, and the waveforms
- [Verification](docs/verification.md): each verification method, what it checks and what it does not
- [Signoff](docs/signoff.md): each signoff check with its source and result, the open items, the CDC and reset review
- [Decisions](docs/decisions.md): each design decision with the options and the evidence
- [Flexibility census](flex/PREREG.md): the rules for the census of protocols on the frozen chip
- [Datasheet](docs/info.md): pins, how to use the chip step by step, and the external parts for each protocol
- [Tapeout checklist](docs/tapeout_checklist.md): the checks before the final layout run
