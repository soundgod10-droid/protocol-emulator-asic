# Line front ends (Pmod modules)

Two plug-in modules for the USB low-speed and 10BASE-Te helpers (docs/helpers.md). They plug into the project Pmod
headers of the demo board (uio and ui), so the same parts serve the silicon bring-up. On the 25 MHz FPGA bench these
helpers do not run at their line rates (docs/bench.md section 1), so the modules are built for the demo board (60 MHz).

| File | Content |
|---|---|
| usb_ls_pmod.svg | USB low speed: TUSB1106 transceiver (SPEED low), OE# pull-up, switched 1.5 kohm D- pull-up (SOFTCON), ESD diodes |
| eth_te_pmod.svg | 10BASE-Te: 2 chip pads per leg with 69.8 ohm each, 20 MHz L-C-L filter, RJ45 with magnetics, slicer and squelch comparators |
| ../../spice/eth_te_tx_*.sp, ../../spice/eth_te_rx.sp | ngspice decks of the 10BASE-Te transmit amplitude (IHP pad models) and of the receive thresholds |

Pmod pins follow the demo board convention (bit k of a port on Pmod pin k + 1 for k = 0 to 3 and on pin k + 3 for
k = 4 to 7; GND on pins 5 and 11, 3.3 V on pins 6 and 12); check it against the demo board schematic before layout.

## USB low speed (usb_ls_pmod.svg)

| Chip pin | Pmod | Module net | Transceiver pin (TUSB1106PWR) |
|---|---|---|---|
| uio0 (helper TXA) | uio pin 1 | VPO | 13 VPO |
| uio1 (helper TXB) | uio pin 2 | VMO | 14 VMO |
| uio2 (helper not OE) | uio pin 3 | OE_N | 3 OE# (10 kohm pull-up: no drive while the chip pins are released) |
| uio3 (thread pin, or a host GPIO) | uio pin 4 | SOFTCON | 2 SOFTCON (100 kohm pull-down: detached at power-up) |
| ui_in[0] | ui pin 1 | RCV | 4 RCV |
| ui_in[1] | ui pin 2 | VP | 5 VP |
| ui_in[2] | ui pin 3 | VM | 6 VM |

Pin numbers are those of the PW (TSSOP-16) package, SCAS818E Table 1. SPEED (10) to GND: low speed with slew-rate
control, D+/D- edges 75-300 ns at 200-600 pF (SCAS818E p.11). SUSPND (7) to GND. VCC(I/O) (9) on 3.3 V (with
VCC(I/O) open, D+ and D- are 3-state, Table 7). Regulator bypass (Figure 9, low speed): VCC(5.0) (16) and Vreg(3.3)
(15) together on 3.3 V (3.3 V +/- 10 %). 1.5 kohm from Vpu(3.3) (1) to D- (11); Vpu(3.3) is open while SOFTCON is low.
The 33 ohm series resistors are inside the device (functional block diagram). Self-powered: VBUS goes only to a
100 nF capacitor (Figure 9 text).
Open: the data sheet gives no 24 h VBUS-short rating for D+/D- (USB 2.0 7.1.1).

## 10BASE-Te (eth_te_pmod.svg)

| Chip pin | Pmod | Module net |
|---|---|---|
| uio0 (helper TXA, 10BASE-Te) | uio pin 1 | TXA -> 69.8 ohm -> TD+ |
| uio1 (helper TXB) | uio pin 2 | TXB -> 69.8 ohm -> TD- |
| uio2 (helper TXC, TXA one clock later) | uio pin 3 | TXC -> 69.8 ohm -> TD+ |
| uio3 (helper TXD, TXB one clock later) | uio pin 4 | TXD -> 69.8 ohm -> TD- |
| ui_in[0] (SEL DIN) | ui pin 1 | slicer output |
| ui_in[1] (SEL AUX0) | ui pin 2 | squelch output |

The host gives uio2 and uio3 the helper signals TXC and TXD (WRITE_CFG pin words, docs/host_port.md section 4); the
firmware fw/eth_10bt.s does not change.

Predicted by ngspice with the IHP SG13CMOS5L pad models (schematic netlist, no extraction; IO supply 3.3 V +/- 3 %;
slow corner = slow MOS, 125 C, 3.2 V; fast corner = fast MOS, -40 C, 3.4 V; magnetics 1:1 with 350 uH, leakage and
winding capacitance assumed; load 100 ohm):

| Case | Peak differential voltage | Limit (10BASE-Te) |
|---|---|---|
| 69.8 ohm, lossless magnetics, slow corner, all-ones (lowest peak) | 1.541 V | >= 1.54 V |
| 69.8 ohm, lossless magnetics, fast corner, 1010 pattern (highest peak) | 1.944 V | <= 1.96 V |
| magnetics loss -1.0 dB (HR911105A maximum), any series resistor | no value holds both corners: the slow corner needs <= 43 ohm, the fast corner >= 47 ohm | |

So the module meets the amplitude window at all corners only with low-loss magnetics (about 0.4 dB or less) and a
series resistor chosen per board; with the data-sheet maximum loss it misses each limit by about 1 %. The first board
measures the amplitude into 100 ohm and the magnetics loss before the row is claimed.

Receive: 100 ohm termination to VB = 1.65 V; slicer TLV3502 channel A (RD+ against RD-); squelch channel B (RD+
against VREF = VB + 197 mV). ngspice (10 MHz sine at the jack): 300 mV peak gives -50.5 mV of comparator overdrive
(rejected; IEEE 802.3 14.3.1.3.2), 585 mV gives +88.4 mV (+33.5 mV with -1.0 dB magnetics loss), against the
comparator's 6.5 mV offset and 6 mV hysteresis (SBOS321E p.5).

## Parts (exact part numbers; prices and stock are read by the buyer)

| Ref | Part number | Maker | Value / function | Qty per module |
|---|---|---|---|---|
| USB U1 | TUSB1106PWR | Texas Instruments | USB full/low-speed transceiver, TSSOP-16 | 1 |
| USB U2 | TPD2E2U06DRLR | Texas Instruments | 2-channel ESD protection, D+/D- | 1 |
| USB J3 | 61729-0010BLF | Amphenol ICC (FCI) | USB Type-B receptacle, through hole | 1 |
| USB R1 | RC0603FR-071K5L | Yageo | 1.5 kohm 1 % 0603, D- pull-up | 1 |
| USB R2 | RC0603FR-0710KL | Yageo | 10 kohm 1 % 0603, OE# pull-up | 1 |
| USB R3, R4 | RC0603FR-07100KL | Yageo | 100 kohm 1 % 0603 | 2 |
| USB C1, C3 | GRM188R71H104KA93D | Murata | 100 nF X7R 50 V 0603 | 2 |
| USB C2 | GRM188R61E105KA12D | Murata | 1 uF X5R 25 V 0603 | 1 |
| Eth J3 | HR911105A | HanRun | RJ45 with 10/100 magnetics and LEDs | 1 |
| Eth U1 | TLV3502AIDCNR | Texas Instruments | dual 4.5 ns comparator, SOT-23-8 | 1 |
| Eth U3 | TPD4E05U06DQAR | Texas Instruments | 4-channel ESD protection, chip side of the magnetics | 1 |
| Eth R1-R4 | RG1608P-69R8-B-T5 | Susumu | 69.8 ohm 0.1 % 0603 (per-board alternative values: 56, 62, 66.5 ohm, same series) | 4 |
| Eth R5, R6 | RC0603FR-0749R9L | Yageo | 49.9 ohm 1 % 0603, termination | 2 |
| Eth R7, R8, R10 | RG1608P-103-B-T5 | Susumu | 10.0 kohm 0.1 % 0603, VB and VREF dividers | 3 |
| Eth R9 | RG1608P-7871-B-T5 | Susumu | 7.87 kohm 0.1 % 0603, VREF divider | 1 |
| Eth R11, R12 | RC0603FR-0733RL | Yageo | 33 ohm 1 % 0603, comparator outputs | 2 |
| Eth L1, L2 | 0603HP-R39XJLU | Coilcraft | 390 nH 5 % 0603 | 2 |
| Eth L3, L4 | 0603HP-R24XJLU | Coilcraft | 240 nH 5 % 0603 | 2 |
| Eth C1 | GRM1885C1H161JA01D | Murata | 160 pF C0G 50 V 0603, filter | 1 |
| Eth C2 | GRM188R71H103KA01D | Murata | 10 nF X7R 0603, VB | 1 |
| Eth C3-C5, TCT | GRM188R71H104KA93D | Murata | 100 nF X7R 0603 | 4 |
| Eth C6 | GRM188R61E225KA12D | Murata | 2.2 uF X5R 0603, comparator supply | 1 |
| both J1, J2 | PREC006DBAN-M71RC (or 2x6 2.54 mm right-angle male) | Sullins | Pmod plug | 2 |

The HR911105A pin numbering is taken from its drawing when the module is laid out (the data sheet copy read for this
file is a scanned image; its electrical values were read from the search-engine text of that copy: OCL 350 uH min
at 100 kHz with 8 mA DC, insertion loss -1.0 dB max from 300 kHz to 100 MHz, return loss -20 dB min from 1 to 10 MHz,
1,500 Vrms isolation).
