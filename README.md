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
| CAN 2.0A node, JTAG controller, SWD host, PS/2 host and device, timed trigger | firmware in `fw/`, tested in the reference model against protocol models written from each specification (`test/test_flex.py`); words, rates and limits in the Flexibility section below |
| I2C target timing characterization by the host (binary search on one firmware time constant) | tested in the model against targets with set limits (`test_flex_i2c_char`) |
| One of low-speed USB or 10BASE-T | planned |
| Tile size: 6x4 as built; the competition rules now allow 8x4 | open |
| Silicon | none yet |

## Flexibility: protocols the instruction set was not designed for

The instruction set ([docs/isa.md](docs/isa.md) section 13) was designed from UART, SPI and I2C firmware. The protocols below were written later, each from its public specification, with the same instruction set, assembler and host port, and no hardware change. Each test runs the firmware in the cycle-exact reference model against a protocol model written from the specification (not from the firmware). All four protocols are named on the competition page, so they were known when the instruction set was made. Rates at 50 MHz.

| Protocol | Specification | Firmware, words | Highest rate measured | Test | What the instruction set lacked (workaround) |
|---|---|---|---|---|---|
| CAN 2.0A node: send with arbitration, receive with CRC-15 check, ACK | Bosch CAN Specification 2.0 (1991), Part A | `fw/can.s`, 123 words, 2 threads | 250 kbit/s (sample point 50 to 55 %, other node +/-0.5 %); 500 kbit/s and 1 Mbit/s do not fit | `test_flex_can` | instruction rate (about 31 instructions of receive work per bit); one owner per pin (two open-drain pins joined on the board); the host makes stuffing and CRC for sending; one thread would need 171 words |
| JTAG (IEEE 1149.1) controller: any TAP path, chains | TI SSYA002C JTAG primer | `fw/jtag.s`, 31 words | TCK 1.25 MHz (1.157 MHz mean over a 64-bit scan) | `test_flex_jtag` | `SAMPLE` takes 2 thread slots, so the loop is 40 cycles, not 32 |
| SWD host: line reset, JTAG-to-SWD, DP and AP reads and writes, WAIT | Arm IHI0031H (ADIv5.2), B4, B5 | `fw/swd.s`, 62 words | SWCLK 1.5625 MHz; DPIDR read in 1,596 cycles | `test_flex_swd` | no parity instruction (the host makes the parity); a write fills the 4-word host FIFO |
| PS/2 host and device: device clock, odd parity, request to send, acknowledge, line contention | IBM PS/2 Hardware Interface Technical Reference (1988), Keyboards | `fw/ps2_host.s` 62 words, `fw/ps2_dev.s` 60 words | 10 to 16.7 kHz both sides; the host decodes device clocks up to 556 kHz | `test_flex_ps2` | a thread cannot wait for an edge and a host word at the same time (the host polls in 4 us windows) |
| Timed trigger: a pulse D cycles after a matching UART byte | - | `fw/trigger.s`, 30 words | delay from the start edge the same at all 16 start phases (0 cycles jitter in the firmware); D from 44 cycles | `test_flex_trigger` | - |

Which programs fit together in 128 words: `test_flex_word_count` and [docs/host_port.md](docs/host_port.md) section 14 (for example UART TX + UART RX + JTAG + SWD: 121 words). Host words of each program: the same section.

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
