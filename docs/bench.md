# FPGA bench (draft)

Status: draft, written before the boards are bought. No row of this page has run on hardware yet. Every result that is
added later states the conditions in section 1 and links its files under `bench/results/<date>/`.

## 1. What the bench is, and what it is not

The bench runs the chip RTL (the file set of `fpga/files.f`: the latch FIFO memory as FPGA memory, the SRAM macros as
block RAM, no IHP cells) on a ULX3S board (Lattice ECP5 LFE5U-12F or -85F, speed grade 6) with the board's own
25 MHz oscillator as the chip clock. It is not the IHP netlist, its pads, the SRAM macros, or the shuttle's pin
multiplexer; no pin-timing number from the bench applies to the chip.

**Why 25 MHz.** The chip runs at 60 MHz. On the FPGA, the post-route fmax with nextpnr's timing models (not vendor
sign-off), worst of 5 place-and-route seeds, is below 60 MHz on every family measured:

| Family, speed grade | Worst of 5 seeds | Run |
|---|---|---|
| ECP5 LFE5U-25F, speed 8 (ABC9 without wide-LUT muxes, router rip-up) | 39.28 MHz | fpga 44870318873 |
| ECP5 LFE5U-25F, speed 8 | 39.06 MHz | fpga 44870318873 |
| Artix-7 xc7a35t, speed 1 (open flow) | 37.17 MHz | fpga 44808418108 |
| ECP5 LFE5U-12F, speed 6 (ULX3S on sale) | 29.86 MHz | fpga 44881773305 |
| ECP5 LFE5U-85F, speed 6 (ULX3S on sale) | 29.62 MHz | fpga 44881773305 |
| Gowin GW2AR-18 (Tang Nano 20K) | 25.58 MHz | fpga 44870318873 |

The limiting paths are long carry chains with about 60 % routing delay. Retiming, other synthesis options, more seeds
and another vendor's fabric did not reach 60 MHz, so the bench clock is the worst seed divided by 1.05 (the same 5 %
margin as the gate), rounded down to the board oscillator: 25 MHz on the ECP5 speed-6 parts (29.86 / 1.05 = 28.4 MHz).

**What follows from it.** The USB low-speed and 10BASE-T helpers have their line rates fixed for 60 MHz (40 clock
cycles a USB bit, 3 a 10BASE-T half bit; `docs/helpers.md` section 1), so neither runs at its line rate on this bench:
**USB and 10BASE-T are not shown on hardware**; they are shown in RTL and gate-level simulation at 60 MHz. The
firmware protocols run on the bench at rates scaled to 25 MHz (the firmware assembled with that clock's constants).

## 2. Wiring (ULX3S)

Pin map: `fpga/ulx3s_bench.lpf` (sites from the board's published constraint file). The host is a Raspberry Pi Pico 2
(RP2350), as on the demo board; its GPIO numbers are assigned in the host program.

| Chip pin | ULX3S header pin | FPGA site | Direction (FPGA) |
|---|---|---|---|
| clk | on-board 25 MHz oscillator | G2 | in |
| rst_n | gp[19] | F17 | in, pull-up |
| ui[0..7] | gp[0..7] | B11, A10, A9, B9, A7, C8, C6, A6 | in, pull-up |
| uo[0..7] | gn[0..7] | C11, A11, B10, C10, A8, B8, C7, B6 | out |
| uio[0..5] | gp[8], gn[8], gp[9], gn[9], gp[10], gn[10] | A4, A5, A2, B1, C4, B4 | in/out, pull-up |
| uio[6..7] | gp[18], gn[18] | H18, H17 | in/out, pull-up |

gp/gn 11-13 are left free (the board's WiFi module shares them). All signals are LVCMOS 3.3 V on both sides. Keep
every jumper under 10 cm, with a ground wire beside each group, and one ground point for the analyser.

## 3. Bench steps and pass criteria

Each row has a number, an outside oracle where one exists, and a negative control that must fail.

| ID | Shows | Pass criterion | Control (must fail) |
|---|---|---|---|
| F0 | Wiring | wiring-check bitstream (`fpga/wiring_check.v`): the host judge (`fpga/wiring_judge.py`) names 0 faults | one wire moved: named (the simulation test `fpga/test_wiring_check.py` shows a swapped pair, a moved input wire and an open wire each named) |
| F1 | Clock, reset, host link | clock 25 MHz +/- 100 ppm, counted by the host against its crystal; READ_ID = 50 45 02 03; WRITE_MEM / READ_MEM with 4 patterns over all 1,536 words, 0 errors | wrong SCK mode: ID wrong |
| F2 | Firmware self-test | every `fw/` program's host-visible result equals the cycle model's | one instruction word flipped: mismatch |
| P1 | Firmware protocols | UART, SPI controller and target, I2C, 1-Wire, AM2302, CAN, SWD, JTAG, PS/2 against real parts at the 25 MHz rates, decoded by sigrok from the analyser capture; the payload equals what the host asked | one pulse inverted in the capture: the decoder's judgement fails |

USB (enumeration, descriptors, repetition) and 10BASE-T (link, frames, ping) are not bench rows at 25 MHz (section 1).

## 4. Bitstreams

`.github/workflows/fpga.yaml`, job `wiring`, builds the wiring check and the chip (`fpga/fpga_top.v`) for the ULX3S 12F
and 85F with the bench pin map, every pin constrained, at 25 MHz, and keeps them as the artifact `bench-bitstreams`
with `MANIFEST.txt`. The chip bitstream must reach 26.25 MHz (25 MHz + 5 %) with the pins placed as above.

## 5. Evidence layout

`bench/results/<date>/` per bench day:
- `MANIFEST.txt`: the commit, the tool versions, and the sha256 of every RTL file, the pin map, the bitstream and the
  host program;
- logs of the host program and the judges, with the PASS/FAIL line of each row and of its control;
- sigrok captures (`.sr`) and their decoded text;
- photos of each set-up (metadata removed).

A row of section 3 is reported as PASS only with its control failing in the same session.

## 6. Judges

The judges are in `bench/judges/`. Each one gives PASS or FAIL from the files of a bench session, and each has a
negative control that must fail. In CI they run on results of the RTL (logic simulation), which shows that each judge
reads its input format and that its control fails; it does not show that a bench session will pass.

| Row | Judge | Input on the bench | Control (must fail) | CI proof |
|---|---|---|---|---|
| P1, USB wire | `capture.py row ROW.json CAPTURE.vcd OUT` | analyser export (VCD of named channels; sigrok-cli or the RP2040/RP2350 LogicAnalyzer), decoded by the pinned sigrok-cli with the decoder stack of the row; content equal to what the host program sent or received | the capture with one pulse of the data line inverted | `bench.yaml`: every scenario window of the sigrok run sampled at 24 and 100 MS/s |
| USB (demo board) | `usb_os.judge_lsusb`, `judge_lsusb_tree` | `lsusb -v -d 1209:0001`, `lsusb -t` | a changed bInterval; a 12M link | `test.yaml` job host, descriptors read from the RTL |
| USB (demo board) | `usb_os.judge_pnputil`, `judge_pnpdevice` | `pnputil /enum-devices /connected`; `Get-PnpDevice ...` piped to `ConvertTo-Json` | Status other than Started / OK; another product ID | as above |
| USB (demo board) | `usb_os.judge_system_profiler` | `system_profiler SPUSBDataType -json` | full speed; another product ID | as above |
| 10BASE-T (demo board) | `net.judge_ping_capture`, `judge_frames` | pcapng (Wireshark, or `pktmon etl2pcap`) | last echo reply removed; one ICMP checksum broken | `test.yaml` job host, the line of the RTL run |
| 10BASE-T (demo board) | `net.judge_ping`, `judge_arp_table`, `judge_nic_stats` | `ping -n N`, `arp -a`, `Get-NetAdapterStatistics | ConvertTo-Json` before and after | one reply lost; another MAC; a receive error | as above |

The USB and 10BASE-T judges serve the demo board (60 MHz); on the 25 MHz FPGA bench these rows are not run
(section 1). The text formats are those of usbutils, Windows 10/11 in English and macOS 14; the first bench session
checks each parser against the real output before its row counts.

P1 rates at the 25 MHz bench clock (T = 40 ns), from the firmware's cycle limits (docs/host_port.md sections 11, 13,
14) divided by 25 MHz; the host assembles each program with the 25 MHz constants (docs/host_program.md section 4):

| Program | Limit in cycles | Rate at 25 MHz | Decoder option |
|---|---|---|---|
| UART TX/RX (fw/uart_tx_full.s, fw/uart_rx_full.s) | B = clock / rate | 115,200 bit/s: B 217 (+0.006 %); 1 Mbit/s: B 25 | uart:baudrate |
| SPI controller (fw/spi_ctl.s) | half period H >= 25 | 500 kHz | - |
| SPI target (fw/spi_tgt.s) | half period >= 25 | 500 kHz | - |
| JTAG (fw/jtag.s, fw/jtag_tap.s) | TCK 40 / 36 cycles | 625 / 694 kHz | - |
| SWD (fw/swd.s) | SWCLK 32 cycles | 781 kHz | - |
| CAN (fw/can.s) | about 124 cycles of receiver work per bit (C2) | 125 kbit/s (200 cycles); 250 kbit/s (100 cycles) does not fit | can:bitrate |
| I2C (fw/i2c_ctl.s) | the .equ table per clock | Standard and Fast mode; the 25 MHz .equ values are set by the same model check as the 40/50/60 MHz table before the first P1 day | - |
| PS/2, AM2302, 1-Wire, PDI | microsecond timing from the .equ / host words | the protocol's own rate | - |
