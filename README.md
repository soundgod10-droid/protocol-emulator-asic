[![gds](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/gds.yaml/badge.svg?branch=cmos5l)](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/gds.yaml?query=branch%3Acmos5l)
[![test](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/test.yaml/badge.svg?branch=cmos5l)](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/test.yaml?query=branch%3Acmos5l)
[![formal](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/formal.yaml/badge.svg?branch=cmos5l)](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/formal.yaml?query=branch%3Acmos5l)
[![docs](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/docs.yaml/badge.svg?branch=cmos5l)](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/workflows/docs.yaml?query=branch%3Acmos5l)

# Programmable protocol emulator

This chip is a small 4-thread processor that makes serial protocols (UART, SPI, I2C, CAN, JTAG and more) in firmware, with each pin change at an exact clock cycle. It is a Tiny Tapeout design for the IHP SG13CMOS5L 130 nm process: 8x4 tiles, 60 MHz.

Documentation site: **<https://soundgod10-droid.github.io/protocol-emulator-asic/>**

![Final layout of the chip](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/layout.png)

*The final layout of the 8x4 tile (1724 x 711 µm), from the GDS of the newest gds run. The white line is the tile outline. Made in CI by [figures/layout.py](figures/layout.py) (KLayout, PDK layer colours).*

![Block diagram of the chip](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/block_diagram.svg)

*Block diagram. Made in CI by [figures/blocks.py](figures/blocks.py) from the module hierarchy of the RTL; the counts come from the RTL.*

## What it does

- **Protocols are firmware.** The chip has no UART, SPI or I2C block. A host loads a program through an SPI port, and the program makes every pin waveform. A new protocol needs new firmware, not new silicon. `fw/` has 13 protocol programs.
- **Four threads in fixed slots.** Thread n executes one 16-bit instruction in each clock cycle c with c mod 4 = n. A waiting thread keeps its slot. Thus the timing of one thread does not change with the other three.
- **Timed output events.** `OUT` does not change the pin at once. It writes an event for a set cycle into the slot of that pin, and the pin changes in exactly that cycle. A late event still occurs and sets a LATE flag for the host.
- **Timed inputs.** `SAMPLE` reads a pin at a set cycle. `WAIT` stops the thread until an edge and gives the cycle of the edge.
- **Pins for real buses.** The host gives each output pin to one thread. Pins can be push-pull or open-drain. Two inputs have a spike filter for I2C.
- **Memory and host port.** 128 words of 16 bits hold the programs and data of all threads. The host SPI port (mode 0, up to 10 MHz) loads the memory, starts and stops the threads, and moves data through a 4-word FIFO in each direction for each thread.
- **Status.** The layout passes all Tiny Tapeout checks. No chip is made yet.

## Key results

| Check | Result | Run |
|---|---|---|
| Timing at 60 MHz, setup slack of the flip-flop paths, slow / typical / fast corner | +3.84 / +8.46 / +11.20 ns | [gds 43789433266][gds] |
| Hold slack, slow / typical / fast corner | +0.59 / +0.27 / +0.10 ns | [gds 43789433266][gds] |
| Setup, hold, max-slew and max-capacitance violations | 0 at all 3 corners | [gds 43789433266][gds] |
| Routing DRC, LVS, antenna | 0 / 0 / 0 errors | [gds 43789433266][gds] |
| Tiny Tapeout precheck | 10 of 10 checks pass | [gds 43789433266][gds] |
| RTL tests (cocotb, Icarus Verilog) | 90 of 90 pass | [test 43795007970][test] |
| Protocol programs on the RTL / on the gate-level netlist | 15 of 15 / 15 of 15 pass | [test 43795007970][test], [gds 43789433266][gds] |
| Code coverage of the RTL tests: line / branch / toggle | 100 % / 100 % / 98.6 % | [coverage 43795007944][cov] |
| Formal proofs and covers (SymbiYosys) | 14 of 14 jobs pass | [formal 43795007954][formal] |

Conditions: static timing of the routed layout at the three corners of the flow (slow 1.08 V 125 °C, typical 1.20 V 25 °C, fast 1.32 V -40 °C). The tests are logic simulations. The gate-level tests use the netlist of the layout with functional cell models and no delays. There is no silicon result yet. The [Results](https://soundgod10-droid.github.io/protocol-emulator-asic/results/) page is made again from the newest runs on every push, with every value and its run.

[gds]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43789433266
[test]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43795007970
[cov]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43795007944
[formal]: https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/43795007954

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

## Compared with other programmable I/O engines

| | Engines and memory | Timing of a pin change |
|---|---|---|
| RP2040 PIO | per block 4 state machines, one 32-instruction memory, 9 instructions, two 32-bit shift and two scratch registers each ([RP2040 datasheet](https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf) build 2025-02-20, pp. 311 to 313) | one instruction per clock, each with a delay of up to 31 cycles (p. 313): the pin changes when the instruction executes |
| TI PRU-ICSS (AM335x) | two independent 32-bit RISC cores at 200 MHz, 8 KB data RAM each and 12 KB shared ([TIDUB30](https://www.ti.com/lit/ug/tidub30/tidub30.pdf), p. 4) | single-cycle instructions, deterministic execution (p. 4): the pin changes when the instruction executes |
| XMOS XS1 | up to 8 threads per core, four instructions in flight from four threads, one memory for program and data ([XS1 background](https://media.futureelectronics.com/semiconductors/microprocessors/embedded-processors/XMOS%20Architecture%20Background.pdf), p. 1); the scheduler issues from the next thread that is not waiting (p. 1) | a port can drive a value when its 16-bit port counter reaches a given count ([AN10059](https://www.xmos.com/download/AN10059:-How-to-perform-timed-output-on-a-port(1.0.0rc3).pdf/), p. 2) |
| This chip | 4 threads in fixed slots, 128 x 16 shared memory, 4 data registers per thread | every output pin change is an event at an absolute 16-bit cycle time, one event slot per pin; a late event still happens and is reported to the host |

The closest of these is the XMOS timed port. The differences here are the fixed slots (a waiting thread does not give its slot away), the event slot on every output pin, and the LATE report. The cost is the instruction rate: one instruction every 4 cycles per thread limits, for example, the SPI target firmware to 1 MHz at 50 MHz ([docs/host_port.md](docs/host_port.md) section 13).

## Pins

| Pins | Use |
|---|---|
| `ui_in[7:5]`, `uo_out[7]` | host SPI: SCK, CS_N, MOSI, MISO |
| `ui_in[4:0]` | processor inputs 8 to 12 |
| `uo_out[6:0]` | processor outputs 8 to 14 (high after reset) |
| `uio[7:0]` | processor pins 0 to 7, in or out; `uio[6]`, `uio[7]` have the I2C spike filter |

After reset no thread runs, `uo_out[6:0]` is high and all `uio` pins are inputs.

## How to use it

1. Assemble a firmware file with `asm/pe_asm.py` ([docs/isa.md](docs/isa.md) section 11).
2. Over the host port: write the words with WRITE_MEM, give the output pins to their threads with WRITE_CFG, then START. Give host words to the threads with PUSH and read their words with PULL. The step-by-step UART example is in [docs/info.md](docs/info.md).
3. Clock-dependent values (bit times, SPI half period, I2C times) are host words or assembler constants, so one firmware file works at other clock frequencies ([docs/host_port.md](docs/host_port.md) section 11).

Run the tests: `cd test && make -B` (cocotb with Icarus Verilog). Formal proofs: `sby -f formal/<job>.sby`.

## Documents

- [docs/architecture.md](docs/architecture.md): block diagram, slots and pipeline, memory, host port, pins, clock and reset
- [docs/signoff.md](docs/signoff.md): each signoff check of the field with its source, our result and the open items; CDC and reset review
- [docs/info.md](docs/info.md): datasheet page, pins, the test procedure and the external parts for each protocol
- [docs/isa.md](docs/isa.md): instruction set and encoding
- [docs/host_port.md](docs/host_port.md): host SPI port, host rules, firmware limits
- [docs/verification.md](docs/verification.md): each verification method, what it checks and what it does not, latest results
- [docs/decisions.md](docs/decisions.md): each design decision with the options compared and the evidence
- [docs/tapeout_checklist.md](docs/tapeout_checklist.md): checks before the final layout run
