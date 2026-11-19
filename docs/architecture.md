# Architecture

This document describes the blocks of the chip, how they connect, and the numbers that size them. The instruction set is in [isa.md](isa.md), the host port in [host_port.md](host_port.md), the checks and their results in [signoff.md](signoff.md) and [verification.md](verification.md), the reasons for each choice in [decisions.md](decisions.md).

## 1 Block diagram

![Block diagram of the chip, drawn from the RTL hierarchy](https://soundgod10-droid.github.io/protocol-emulator-asic/figures/block_diagram.svg)

*The docs workflow draws this figure on every push from the module hierarchy that Yosys reads from the RTL of `info.yaml` ([figures/blocks.py](../figures/blocks.py)); the counts and sizes in it come from that hierarchy, and the script fails if the RTL gets a module that the figure does not show.*

The structure follows the RTL hierarchy (`src/`). One clock domain (`clk`, declared 60 MHz); every block below is clocked by it. The text diagram gives the same blocks with their main signals.

```
                   ui_in[7:5] (SCK, CS_N, MOSI)                 uo_out[7] (MISO)
                         |                                           ^
                  pe_sync2 x 3 (2 flip-flops each)                   |
                         v                                           |
 rst_n --> pe_reset_sync --+--> rst_n_int (asynchronous assert, release at a clock edge)
                           |
        +------------------v-------------------------------------------+------+
        | pe_host_port: SPI mode 0 target, command decoder, STOP/START |      |
        |   (running), memory and configuration writes, READ_ID,       |      |
        |   READ_STATUS, PUSH/PULL to the host FIFOs                    |------+
        +---+--------------+---------------+---------------+------------+
            | start        | cfg writes    | mem write/read | FIFO push/pop
            v              v               |                |
   pe_cycle_counter    pe_config           |                |
   (32-bit; START      (start address x4,  |                |
    clears it;          RUN mask, owner     |                |
    now = bits 15:0)    and open-drain bit  |                |
            |           of 15 output pins)  |                |
            |              |               |                |
        +---v--------------v---------------v----------------v-------------+
        | pe_proc  (held in reset by run_rst_n = rst_n_int AND running)   |
        |                                                                 |
        |   pe_core: 4-thread barrel, slot n in cycles c mod 4 = n        |
        |     per thread: PC, T (16-bit time), r0..r3, flags Z C N E,     |
        |     RUN, LATE, ERR                                              |
        |        |  one request per cycle: fetch, LD/LDX, ST/STX          |
        |        v                                                        |
        |   pe_sram: 512 x 16 program memory + 1,024 x 16 data memory     |
        |     (two IHP SRAM macros, one access per cycle)                 |
        |                                                                 |
        |   pe_host_fifo: per thread 4-word in-FIFO and 4-word out-FIFO   |
        |                                                                 |
        |   pe_timed_out x 15: one event slot per output pin              |
        |     (event time, value, OE); the pin flip-flop changes when     |
        |     now = event time                                            |
        |                                                                 |
        |   pe_timed_in: 13 inputs, pe_sync2 each; spike filter (K = 5)   |
        |     on inputs 6, 7 (uio[6], uio[7]); 5-cycle input history      |
        |                                                                 |
        |   pe_slu u_usb: USB low-speed helper, I/O ports 16 to 31        |
        |   pe_slu u_eth: 10BASE-Te helper, I/O ports 32 to 47; signals   |
        |     to pins through one flip-flop; events as inputs 13, 14      |
        +-----------------------------+-----------------------------------+
                                      |
              uo_out[6:0] = V of pins 14..8;  uio_out / uio_oe = V / OE of pins 7..0
              inputs: ui_in[4:0] (inputs 8..12), uio_in[7:0] (inputs 0..7)
```

## 2 Threads, slots and the pipeline

- **Fixed slots.** Thread n issues one 16-bit instruction in each clock cycle c with c mod 4 = n ([isa.md](isa.md) 3.1). A thread that waits (WAIT, SAMPLE, a full event slot, an empty FIFO) keeps its slot, so the timing of one thread does not depend on the other three. At 60 MHz each thread issues 15 million instructions per second.
- **Pipeline.** The core shares one datapath between the threads. The register selects of the next instruction are decoded in the cycle before its slot (prefetch), so the slot cycle holds the register read, the ALU and the write-back ([decisions.md](decisions.md) entry on the slow-corner path). The memory has one port: LD, LDX, ST and STX use it in the next slot of the thread and take 2 slots ([isa.md](isa.md) 3.3).
- **Time.** Each thread has a 16-bit time register T. `OUT pin, value, offset` writes an event for cycle T + offset (0 to 7) into the slot of that pin. The event must be written 4 cycles (LEAD) to 32,767 cycles ahead ([isa.md](isa.md) 4.2). A late event still happens, 4 cycles after the instruction, and sets the sticky LATE flag. `TADD` adds 0 to 4,095 cycles (immediate) or a register to T, so bit times are exact whatever the instructions in between take.
- **Inputs.** `SAMPLE` reads the synchronized level of a given cycle t (the pin level of t - 2) from the 5-cycle input history; `WAIT` returns the cycle of an input edge, with a timeout ([isa.md](isa.md) 6). One shared history (65 flip-flops) serves all four threads, instead of a capture register and comparator per thread.
- **Pin ownership.** The host gives each output pin to one thread (pe_config). `OUT` from another thread changes nothing and sets ERR. Each pin is push-pull or open-drain; an open-drain uio pin reads the bus level ([isa.md](isa.md) 5).

## 3 Memory

- Two IHP SRAM macros (`pe_sram`): `RM_IHPSG13_1P_512x16_c2_bm_bist` holds the programs of all threads and the LD/ST data (addresses 0 to 511), `RM_IHPSG13_1P_1024x16_c2_bm_bist` the frames and tables (LDX/STX addresses 512 to 1,535; 1,536 to 2,047 are 512 to 1,023 again). One access per cycle. The core sends the request for a slot 3 cycles before that slot, from flip-flops, so the macro read (about 7.4 ns after the clock edge at the slow corner, macro liberty) and the decode fit before the slot ([isa.md](isa.md) 3.3).
- Why: the 128 x 16 latch array of the first version (97,044 um2 in synthesis) held neither a USB device program with its descriptors nor one 1,518-byte Ethernet frame. The two macros give 1,536 words in 124,983 um2 of macro area (LEF) ([decisions.md](decisions.md) entry 3).
- Layout: both macros sit 40 um above the bottom edge of the tile. The vertical power stripes have a pitch of 67.44 um, a multiple of the macro's power column pitch, so that each stripe over a macro lands on a column of its own net ([decisions.md](decisions.md) entry 19). A data macro at x = 280.80 um left a short stripe in the 14 um channel between the macros, which the layout precheck rejects (each supply port must reach both tile edges); at x = 348.24 um the channel is one stripe pitch wide and the grid stripes pass through it.
- The memory has no reset; the host loads it after every power-up and select ([host_port.md](host_port.md) section 8).

## 4 Host port

SPI mode 0, MSB first, up to 10 MHz SCK; each pin through a 2-flip-flop synchronizer, so the port runs in the chip clock domain. Commands: READ_ID, READ_STATUS, STOP, START, WRITE_MEM, READ_MEM, WRITE_CFG, PUSH, PULL, CLEAR, each a code {c, not c} so that one wrong bit gives no command ([host_port.md](host_port.md) 4). STOP holds the processor in reset (memory and configuration kept); START releases it at a known cycle, so cycle 0 of every thread is defined. Measured port rates: 493,827 words/s (PUSH) and 449,438 words/s (PULL) at 10 MHz SCK ([host_port.md](host_port.md) 10).

## 5 Pins and pads

| Pins | Use | Direction |
|---|---|---|
| `ui_in[7:5]`, `uo_out[7]` | host SPI SCK, CS_N, MOSI, MISO | in, out |
| `ui_in[4:0]` | processor inputs 8 to 12 | in |
| `uo_out[6:0]` | processor outputs 8 to 14; 1 after reset (UART idle level) | out, no enable |
| `uio[7:0]` | processor pins 0 to 7, output with enable and input; spike filter on uio[6], uio[7] | in/out |

The pads are the 3.3 V digital I/O pads of the shuttle (TTIHP27a); they are not 5 V tolerant, so 5 V buses need level shifting ([info.md](info.md), external parts). Every output comes from a flip-flop (event slot pin register, helper pin register or the MISO register), so no output glitches. The host can give any of the 15 output pins to a line helper instead of a thread (pin configuration, [helpers.md](helpers.md)); the helpers read their line inputs from any of the 13 input pins.

## 6 Clock and reset

- Clock: one domain, timed at 60 MHz (CLOCK_PERIOD 16.667 ns) at three corners; the host may also run the chip at 50 or 40 MHz with its own firmware constants ([host_port.md](host_port.md) 11). Clock gating only in the FIFO latch arrays, with integrated clock-gate cells. The 10BASE-Te helper samples its data input on both clock edges (120 Msample/s at 60 MHz): one flip-flop on the falling edge, then two rising-edge stages, so that every sample reaches the logic through two stages like the other inputs ([signoff.md](signoff.md), asynchronous inputs).
- Reset: `rst_n` asserts at once (pins go to the reset level without a clock) and is released at the second clock edge (`pe_reset_sync`). STOP is a second, synchronous reset source of the processor (`run_rst_n`, the AND of two flip-flop outputs). The review of every asynchronous input and of the reset tree is in [signoff.md](signoff.md).

## 7 Size and timing

Synthesis in the layout flow (`tt_submission/stats/synthesis-stats.txt` of the gds run): 275,442 um2 of standard cells (16,671 cells, of which 2,249 flip-flops and 512 latches of the host FIFOs) and two SRAM macros of 124,983 um2. Of that, the USB helper with its clocked and NRZ modes is 44,164 um2 (39,808 um2 before those modes) and the 10BASE-Te helper 52,161 um2 (local synthesis, typical corner, [decisions.md](decisions.md) entry 18). Layout on 8x4 tiles (1,724.16 x 710.64 um): 21,741 standard cells after place and route (352,440 um2 with clock, hold and repair buffers; 6,911 repair and 3,067 hold buffers), utilisation 0.395, power 11.2 mW at 60 MHz (flow estimate). The clock tree has at most 8 sinks per leaf buffer ([decisions.md](decisions.md) entry 17). Timing at 60 MHz: flip-flop setup slack +2.307 ns at the slow corner (1.08 V, 125 C), +3.265 ns typical, +3.806 ns fast; hold slack +0.590 / +0.276 / +0.101 ns; no setup, hold, max-slew, max-capacitance or max-fanout violation at the three corners; power grid, LVS, routing DRC and antenna checks 0 (gds run 44557589283, commit b89bd94; the setup-margin setting of [decisions.md](decisions.md) entry 21). The run, its reports and the open items are in [signoff.md](signoff.md); the newest values are on the [Results](https://soundgod10-droid.github.io/protocol-emulator-asic/results/) page.

## 8 Compared with other programmable I/O engines

| | Engines and memory | Timing of a pin change |
|---|---|---|
| RP2040 PIO | per block 4 state machines, one 32-instruction memory, 9 instructions, two 32-bit shift and two scratch registers each ([RP2040 datasheet](https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf) build 2025-02-20, pp. 311 to 313) | one instruction per clock, each with a delay of up to 31 cycles (p. 313): the pin changes when the instruction executes |
| TI PRU-ICSS (AM335x) | two independent 32-bit RISC cores at 200 MHz, 8 KB data RAM each and 12 KB shared ([TIDUB30](https://www.ti.com/lit/ug/tidub30/tidub30.pdf), p. 4) | single-cycle instructions, deterministic execution (p. 4): the pin changes when the instruction executes |
| XMOS XS1 | up to 8 threads per core, four instructions in flight from four threads, one memory for program and data ([XS1 background](https://media.futureelectronics.com/semiconductors/microprocessors/embedded-processors/XMOS%20Architecture%20Background.pdf), p. 1); the scheduler issues from the next thread that is not waiting (p. 1) | a port can drive a value when its 16-bit port counter reaches a given count ([AN10059](https://www.xmos.com/download/AN10059:-How-to-perform-timed-output-on-a-port(1.0.0rc3).pdf/), p. 2) |
| This chip | 4 threads in fixed slots, 512 x 16 program and 1,024 x 16 data memory shared, 4 data registers per thread; a USB low-speed and a 10BASE-Te line helper on I/O ports; the USB helper also runs clocked and NRZ lines (SPI, JTAG, SWD clock, UART) at up to 15 MHz ([helpers.md](helpers.md) section 6) | every output pin change is an event at an absolute 16-bit cycle time, one event slot per pin; a late event still happens and is reported to the host |

The closest of these is the XMOS timed port. The differences here are the fixed slots (a waiting thread does not give its slot away), the event slot on every output pin, and the LATE report. The cost is the instruction rate: one instruction every 4 cycles per thread limits, for example, the SPI target firmware to 1 MHz ([host_port.md](host_port.md) section 13).
