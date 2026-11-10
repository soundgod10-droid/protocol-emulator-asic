![](../../workflows/gds/badge.svg) ![](../../workflows/docs/badge.svg) ![](../../workflows/test/badge.svg)

# Programmable protocol emulator

A protocol emulator is a small processor with an instruction set made for reading pins, writing pins, counting cycles and exact timing, so that a protocol is firmware and not fixed logic ([competition definition](https://blog.janestreet.com/protocol-emulator-asic-competition/)). This chip has no UART, SPI or I2C block. A host loads the firmware through an SPI port, and the firmware makes every pin waveform. A new protocol needs new firmware, not new silicon.

Process IHP SG13CMOS5L (130 nm), 6x4 tiles, 50 MHz.

## How it works

- **Four threads, fixed slots.** Thread n executes one 16-bit instruction in each clock cycle c with c mod 4 = n. A stopped or waiting thread keeps its slot, so the timing of one thread does not depend on the other three. ([docs/isa.md](docs/isa.md) sections 1, 3)
- **Pin changes are timed events.** `OUT pin, value, offset` does not change the pin at once. It writes an event for cycle T + offset into the slot of that pin, and the pin flip-flop changes in exactly that cycle. T is a 16-bit time register of the thread. The event must be written 4 to 32,767 cycles ahead. A late event still happens, 4 cycles after the instruction, and sets a LATE flag that the host can read. `SAMPLE` and `WAIT` read inputs at a given cycle, and `WAIT` gives the cycle of an input edge. (sections 4, 6)
- **One owner per pin.** The host gives each output pin to one thread. `OUT` from another thread changes nothing and sets ERR. Pins can be push-pull or open-drain. (section 5)
- **Memory.** 128 words of 16 bits hold the programs and data of all threads (a latch array, no macro).
- **Host port.** SPI mode 0, up to 10 MHz: STOP, START, write and read the memory and the configuration, read the ID and status, and 4-word FIFOs in each direction for each thread. ([docs/host_port.md](docs/host_port.md))

Example, `fw/uart_tx.s` (12 words): the bit time is `TADD 434` between two `OUT` events, so it is exactly 434 cycles whatever the instructions in between take.

## Status

| Item | State |
|---|---|
| Processor, timed pin events, 128 x 16 memory, host SPI port, I2C input spike filter | built; RTL tests, formal proofs, gate-level test pass ([docs/verification.md](docs/verification.md)) |
| Layout at 50 MHz, 6x4 tiles | passes; slow-corner setup margin +1.329 ns, no setup or hold violation at three corners; 3 max-slew violations at the slow corner are open ([run 42744631446](https://github.com/soundgod10-droid/protocol-emulator-asic/actions/runs/42744631446)) |
| UART transmitter firmware `fw/uart_tx.s` | runs on the RTL and on the gate-level netlist (`test/test.py`) |
| UART TX and RX 300 bit/s to 1 Mbit/s, SPI controller (4 modes), SPI target (4 modes, up to 1 MHz), I2C controller (Standard-mode and Fast-mode) | firmware in `fw/`, tested in the cycle-exact reference model against independent protocol models (`test/test_fw_full.py`); UART TX, UART RX, SPI controller and I2C controller fit together in 103 of 128 words (`test_fw_full_word_count`) |
| JTAG, SWD, PS/2 and CAN firmware; a timed trigger (a pulse at an exact cycle after a protocol event) | planned |
| One of low-speed USB or 10BASE-T | planned |
| Tile size: 6x4 as built; the competition rules now allow 8x4 | open |
| Silicon | none yet |

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

- [docs/info.md](docs/info.md): datasheet page, pins and the test procedure
- [docs/isa.md](docs/isa.md): instruction set and encoding
- [docs/host_port.md](docs/host_port.md): host SPI port, host rules, firmware limits
- [docs/verification.md](docs/verification.md): each verification method, what it checks and what it does not, latest results
- [docs/decisions.md](docs/decisions.md): each design decision with the options compared and the evidence
- [docs/tapeout_checklist.md](docs/tapeout_checklist.md): checks before the final layout run
