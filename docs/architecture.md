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
        |        |  read port        | store / host write                 |
        |        v                   v                                    |
        |   pe_latch_ram 128 x 16 (program and data of all threads;       |
        |     2,048 latches, one integrated clock gate per word)          |
        |                                                                 |
        |   pe_host_fifo: per thread 4-word in-FIFO and 4-word out-FIFO   |
        |                                                                 |
        |   pe_timed_out x 15: one event slot per output pin              |
        |     (event time, value, OE); the pin flip-flop changes when     |
        |     now = event time                                            |
        |                                                                 |
        |   pe_timed_in: 13 inputs, pe_sync2 each; spike filter (K = 4)   |
        |     on inputs 6, 7 (uio[6], uio[7]); 5-cycle input history      |
        +-----------------------------+-----------------------------------+
                                      |
              uo_out[6:0] = V of pins 14..8;  uio_out / uio_oe = V / OE of pins 7..0
              inputs: ui_in[4:0] (inputs 8..12), uio_in[7:0] (inputs 0..7)
```

## 2 Threads, slots and the pipeline

- **Fixed slots.** Thread n issues one 16-bit instruction in each clock cycle c with c mod 4 = n ([isa.md](isa.md) 3.1). A thread that waits (WAIT, SAMPLE, a full event slot, an empty FIFO) keeps its slot, so the timing of one thread does not depend on the other three. At 60 MHz each thread issues 15 million instructions per second.
- **Pipeline.** The core shares one datapath between the threads. The register selects of the next instruction are decoded in the cycle before its slot (prefetch), so the slot cycle holds the register read, the ALU and the write-back ([decisions.md](decisions.md) entry on the slow-corner path). LD and LDX use the one memory read port and take 2 slots ([isa.md](isa.md) 3.3).
- **Time.** Each thread has a 16-bit time register T. `OUT pin, value, offset` writes an event for cycle T + offset (0 to 7) into the slot of that pin. The event must be written 4 cycles (LEAD) to 32,767 cycles ahead ([isa.md](isa.md) 4.2). A late event still happens, 4 cycles after the instruction, and sets the sticky LATE flag. `TADD` adds 0 to 4,095 cycles (immediate) or a register to T, so bit times are exact whatever the instructions in between take.
- **Inputs.** `SAMPLE` reads the synchronized level of a given cycle t (the pin level of t - 2) from the 5-cycle input history; `WAIT` returns the cycle of an input edge, with a timeout ([isa.md](isa.md) 6). One shared history (65 flip-flops) serves all four threads, instead of a capture register and comparator per thread.
- **Pin ownership.** The host gives each output pin to one thread (pe_config). `OUT` from another thread changes nothing and sets ERR. Each pin is push-pull or open-drain; an open-drain uio pin reads the bus level ([isa.md](isa.md) 5).

## 3 Memory

- 128 words x 16 bits, one array for the programs and data of all threads (`pe_latch_ram`). Latches (`sg13cmos5l_dlhq_1`) instead of flip-flops, with one integrated clock gate (`sg13cmos5l_lgcp_1`) per word, so a write opens only the latches of one word and an address change near the clock edge cannot make a glitch. A word is written in the first half of the cycle after the request.
- Area by synthesis: 97,044 um2 for the 128 x 16 array alone (local synthesis, typical corner); the whole chip 228,405 um2 in the synthesis step of the layout flow (gds run 43779857970, `stats/synthesis-stats.txt` of the `tt_submission` artifact). A 256 x 16 array was measured and not chosen for this version ([decisions.md](decisions.md), memory size): it fails the layout flow today and no measured program needs more than 128 words. The IHP 256 x 16 SRAM macro (`RM_IHPSG13_1P_256x16_c2_bm_bist`, IHP-Open-PDK) is 236.8 x 118.78 um (28,127 um2) with power on Metal4; it is the option for a larger memory.
- The memory has no reset (latches); the host loads it after every power-up and select ([host_port.md](host_port.md) section 8).

## 4 Host port

SPI mode 0, MSB first, up to 10 MHz SCK; each pin through a 2-flip-flop synchronizer, so the port runs in the chip clock domain. Commands: READ_ID, READ_STATUS, STOP, START, WRITE_MEM, READ_MEM, WRITE_CFG, PUSH, PULL, CLEAR, each a code {c, not c} so that one wrong bit gives no command ([host_port.md](host_port.md) 4). STOP holds the processor in reset (memory and configuration kept); START releases it at a known cycle, so cycle 0 of every thread is defined. Measured port rates: 493,827 words/s (PUSH) and 449,438 words/s (PULL) at 10 MHz SCK ([host_port.md](host_port.md) 10).

## 5 Pins and pads

| Pins | Use | Direction |
|---|---|---|
| `ui_in[7:5]`, `uo_out[7]` | host SPI SCK, CS_N, MOSI, MISO | in, out |
| `ui_in[4:0]` | processor inputs 8 to 12 | in |
| `uo_out[6:0]` | processor outputs 8 to 14; 1 after reset (UART idle level) | out, no enable |
| `uio[7:0]` | processor pins 0 to 7, output with enable and input; spike filter on uio[6], uio[7] | in/out |

The pads are the Tiny Tapeout 3.3 V digital pads; they are not 5 V tolerant, so 5 V buses need level shifting ([info.md](info.md), external parts). Every output comes from a flip-flop (event slot pin register or the MISO register), so no output glitches.

## 6 Clock and reset

- Clock: one domain, timed at 60 MHz (CLOCK_PERIOD 16.667 ns) at three corners; the host may also run the chip at 50 or 40 MHz with its own firmware constants ([host_port.md](host_port.md) 11). Clock gating only in the memory and FIFO latch arrays, with integrated clock-gate cells.
- Reset: `rst_n` asserts at once (pins go to the reset level without a clock) and is released at the second clock edge (`pe_reset_sync`). STOP is a second, synchronous reset source of the processor (`run_rst_n`, the AND of two flip-flop outputs). The review of every asynchronous input and of the reset tree is in [signoff.md](signoff.md).

## 7 Size and timing

Synthesis step of the layout flow (gds run 43779857970): 228,405 um2, 10,050 cells, of which 2,560 latches (memory and FIFOs), 1,121 flip-flops and 160 clock gates. Layout on 8x4 tiles (1,724.16 x 710.64 um): 12,932 cells after place and route (297,607 um2 with clock, hold and repair buffers), utilisation 0.246, power 7.55 mW at 60 MHz (flow estimate). The clock tree has at most 8 sinks per leaf buffer ([decisions.md](decisions.md) entry 17). Timing at 60 MHz, slow corner (1.08 V, 125 C): flip-flop setup slack +3.84 ns; fast-corner hold slack +0.10 ns; no setup, hold, max-slew or max-capacitance violation at the three corners (run 43712217729). The run, its reports and the open items are in [signoff.md](signoff.md).
