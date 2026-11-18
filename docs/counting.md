# Counting rule

This page fixes how the protocols of this chip are counted, before any new protocol is added to the count. The registry [flex/protocols.csv](../flex/protocols.csv) has one row per firmware program in `fw/`. The counts come from that file, from the assembler and from the test results of named CI runs; no count is typed by hand.

## 1 What is one protocol

1. A protocol is the link protocol of one specification: the bits on the pins between two devices, their timing and the framing that the specification defines.
2. The roles of one specification (controller and target, host and device, transmitter and receiver), its rates and its frame options are one protocol. Two programs of one protocol are two rows with the same name in the column `protocol`.
3. These are not protocols and are not counted (column `class`): a function (a program that measures or reacts, for example the timed trigger, `function`); an outline program that a full program of the same protocol replaces (`example`); a combination of protocols that run at the same time; a signal format with no framing (servo pulses, quadrature, step and direction, segment levels); a line code alone; a tool or a bridge.
4. A layered protocol is a message format or a device command set that another protocol carries on the same pin waveform (for example a sensor's register map on I2C). It is counted in its own column, not in the protocol count.
5. A configuration of a protocol that is already counted (a subset or a profile of the same specification) counts with that protocol.

Counting the programs in `fw/` instead would count roles, rates and outline programs several times, and the count would grow with each variant. This rule counts each specification once.

## 2 Strict count and broad count

A protocol is in the **strict count** when at least one of its programs meets all four conditions:

| | Condition | How it is checked |
|---|---|---|
| (a) | The firmware assembles with `asm/pe_asm.py` and fits the 512-word program memory alone. (The USB and 10BASE-T programs also load a data image; its addresses are checked where their tests build it.) | the assembler, on every program of the registry |
| (b) | In its test, the other side of the link is a model written from a named public document. The registry gives the document, its issue, its address and the SHA-256 of the file that was read. Where the publisher refuses an automatic download, or the copy is personal, the document number and revision fix the text and the SHA-256 cell is `-`. The model uses nothing from `fw/`, `asm/` or `model/`. | the model named in the column `model` and every function and class that it calls in the test files must not use the assembler, the reference model or a firmware file |
| (c) | The tests of the row pass on the RTL at the declared clock (60 MHz, `info.yaml`), at a rate inside the range of the standard. | `results.xml` of a named test run |
| (d) | The same tests pass on the gate-level netlist of a named layout run (functional cell models, no delays). | `results.xml` of the gate-level jobs of a named run |

The **broad count** needs (a) to (c).

The column `claim` gives what the documentation claims for a row: `strict`, `broad` or `none`. A row that claims more than its evidence gives is an error, and the count fails.

Reason for the four conditions: (a) and (c) show that the unchanged chip runs the protocol; (b) makes a pass mean agreement with the standard, not agreement with our own firmware; (d) shows that the layout netlist does the same. They are items 1, 2 and 5 of the census pass criteria and its model rule ([flex/PREREG.md](../flex/PREREG.md) sections 5 and 6), so census rows and the other rows meet one bar. The census adds an outside decoder (sigrok, item 3) and a limits table from two readers (item 4); for the other rows the decoder is a column of its own.

## 3 Columns beside the counts

| Column | Content |
|---|---|
| `role` | the role of the program; roles are reported, not counted |
| `family` | line code and framing class (UART-framed, clocked serial, open-drain clocked, bit-timed single wire, differential bus, line-coded packet), so that a reader can see how many different kinds of link the count holds |
| `device_interface` | `yes` for an interface that only one kind of device uses (for example a sensor's own single-wire interface); a reader can leave these rows out |
| `tier` | when and how the program was written (section 4) |
| `sigrok` | the libsigrokdecode decoder that the documentation workflow runs on the RTL pins of this program (`sim/tools/sigrok_compare.py`); an oracle separate from the model |

## 4 Tiers

| Tier | Meaning |
|---|---|
| `isa-basis` | UART, SPI and I2C: the instruction set was designed from these programs ([isa.md](isa.md) section 13). |
| `after-isa` | written after the instruction set was fixed, by the design team, with the design in view (CAN, JTAG, SWD, PS/2, the timed trigger). |
| `hold-out` | chosen by a stated rule after the instruction set was fixed and written from public documents ([protocols.md](protocols.md), Hold-out trials). |
| `helper` | protocols that have a line helper designed for them in the RTL (USB low speed, 10BASE-T; [helpers.md](helpers.md)). |

Tiers that later rows will have: `P before tag` and `P after tag` (the candidate list of section 7, written before or after the `rtl-freeze` tag), `blind` ([flex/BLIND.md](../flex/BLIND.md)) and `census` ([flex/PREREG.md](../flex/PREREG.md)). Every row written before the `rtl-freeze` tag is "written before the final RTL" (PREREG section 3). Only rows written after the tag show what the frozen chip can do for protocols that it was not designed for.

## 5 Who wrote the models

The models of the rows in the registry today were written by the design team from the documents, not from the firmware (the headers of `test/test_fw_full.py`, `test/test_flex.py`, `test/test_holdout.py` and `test/linemodel/`). The strict count does not need a different writer. Census rows and blind-arm rows have writers who did not write the firmware ([flex/PREREG.md](../flex/PREREG.md) section 5, [flex/BLIND.md](../flex/BLIND.md)); they are reported in their own columns.

## 6 How the numbers are computed

The job `count` ([.github/workflows/count.yaml](../.github/workflows/count.yaml), script [flex/count.py](../flex/count.py)) runs after the tests of every push (test workflow). It reads the registry, assembles every program, checks condition (b) on the source of each model, reads the `results.xml` files of the test run (RTL) and of the gate-level runs whose `src/` equals the commit's `src/` (the newest successful gds run of `cmos5l`, and gl_fw runs, [.github/workflows/gl_fw.yaml](../.github/workflows/gl_fw.yaml)), and writes the table of counts (strict, broad, roles, families, device interfaces) to its job summary and to the artifact `count`. The summary names every run it used and, for a gl_fw run, the SHA-256 of the netlist. It fails when a program in `fw/` has no row, when a row names a missing program, or when a row claims more than its evidence gives.

To check a number: open the newest `count` job of the test workflow on `cmos5l`, read the count line and the table, open one row's test in the `results.xml` of the run that the summary names, and compare the commit dates of a post-freeze row with the time of the `rtl-freeze` Release. `python flex/count.py --rtl <results.xml> --gl <results.xml ...>` gives the same table locally.

## 7 Protocols outside the census pool, before the freeze (rule N)

The census pool (44 protocols, [flex/PREREG.md](../flex/PREREG.md) section 2) stays untouched until the `rtl-freeze` tag. Other protocols may be written before the tag. Which ones, and in which order, is fixed here by a rule, so that the list is not our choice.

**Universe U.** (i) The 75 libsigrokdecode decoders at commit `71f4514` that `flex/pool.csv` excludes because they decode another decoder's output or carry the Util or Encoding tag. (ii) The protocol programs of other public entries of the same competition, as read from 2026-11-15 to 2026-11-17 (protocol names only; the entries are not named).

**Removals**, in this order, each with its reason in [flex/candidates.csv](../flex/candidates.csv) (column `rule`):

| Rule | Removed when |
|---|---|
| N1 | the competition page names it (UART, SPI, I2C, CAN, JTAG, SWD, PS/2, USB, 10BASE-T and what rides on them) |
| pool | it is a census pool protocol |
| N2 | it is inside the scope of a pool decoder, read from the decoder's own long name and description at `71f4514` |
| N3 | it is a near-copy of a pool protocol: a census writer could reuse its firmware by changing constants only (same line code and same frame grammar) |
| N4 | it is already in `fw/` |
| N5 | it is a configuration of a named or counted protocol |
| N6 | it is not a protocol (section 1, item 3) |
| N7 | it is layered (section 1, item 4) |

**Result:** list C (column `list` = `C`), and a reserve R of 4 rows chosen by us, reported apart and used only when C runs out. The scope check of N2 and N3 was done in two ways: a script searched the id, name, long name, description and documentation of each of the 44 pool decoders at `71f4514` for every candidate's names (5 hits: 2 were "3-wire" in `microwire`, read against DS1302; the other 3 were a word inside an address, a part of the word "asynchronous" and the common word "fieldbus"), and every candidate that shares a line code with a pool protocol was read against it. The result of each reading is in the column `reason` (for example ISO 7816-3 T=0 against `sle44xx`, Joybus against `sdq`).

**Order.** Score V = O + R + E - H, fixed before any row is written:

| Term | Value |
|---|---|
| O, outside oracle | +2: a decoder at `71f4514`; +1: only a public recording in sigrok-dumps |
| R, document | +2: a multi-vendor standard with a free public document; +1: a free data sheet or public guide of one vendor; 0: de facto, or paid (a public application note is used instead) |
| E, electrical | +1: logic level at 3.3 V with no transceiver (class D) |
| H, effort | -1: large effort (L) |

Rows predicted below a pass by pins or rate go last (column `last`). Ties: lower central hours first, then the id. [flex/order.py](../flex/order.py) prints the order. The 8 rows that the beacon gives to the blind arm leave C first ([flex/BLIND.md](../flex/BLIND.md)); the next rows move up. Hours and words in the file are estimates, not measurements; the hours are measured on the first rows written.

## 8 Conditions

All counts are results of logic simulation: the RTL and the functional gate-level netlist at 60 MHz, with ideal pins. No pad, cable, transceiver or silicon timing is in them. The external parts that each protocol needs are in [info.md](info.md), section External hardware.
