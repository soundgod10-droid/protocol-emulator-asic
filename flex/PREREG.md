# Flexibility census: pre-registration

This file fixes, before the RTL changes again, how the chip's flexibility will be measured: which protocols, in which order, by whom, against which oracles, for how long, and how the result is reported. It is committed and pushed before the next RTL change. The push and the CI run of this commit give the GitHub server timestamp. Nothing in this file is changed after the push; a correction is a new, dated section at the end, and the original text stays.

## 1. Question

Can the frozen chip run protocols that were not written, tested or in view when its RTL was fixed? The competition asks whether the chip "can be reprogrammed to support protocols it wasn't designed for". A small set chosen by the designers cannot answer this, because the designers choose what they expect to pass. This census therefore takes a whole outside catalogue, fixed by a rule written here, and attempts every protocol in it in an order that nobody can choose.

## 2. Catalogue and pool

- **Catalogue:** sigrok libsigrokdecode, commit `71f451443029322d57376214c330b518efd84f88` (master head, committed 2024-10-01T21:02:49Z), directory `decoders/`: 132 entries, 131 decoders and the helper directory `common`.
- **Pool rule.** A decoder is in the pool if all of these hold, read from its `pd.py` at that commit:
  1. `inputs = ['logic']`: it decodes pins, not the output of another decoder;
  2. its `tags` contain neither `Util` nor `Encoding`: it is a protocol, not a measurement aid, line code or signal format;
  3. it is not one of the protocols the competition page names (uart, spi, i2c, usb_signalling, jtag, swd, ps2, can);
  4. no firmware for it exists in this repository at this commit (onewire_link: `fw/onewire.s`; am230x: `fw/am2302.s`; avr_pdi: `fw/pdi.s`);
  5. it names one protocol. One decoder fails this rule: ir_irmp wraps an external multi-protocol library (IRMP) and overlaps ir_nec, ir_rc5, ir_rc6 and ir_sirc of the pool.
- **No feasibility filter.** Protocols that are expected to fail are in the pool and are attempted. The prediction (section 8) states them beforehand.
- **Count.** 131 decoders: 63 take another decoder's output, 68 take pins. Of the 68, 12 carry the Util or Encoding tag, 8 are named on the page, 3 have firmware here and 1 names no single protocol. **Pool: 44 decoders.** Every one of the 131 decoders is a row of `flex/pool.csv` with its inputs, its tags, its status and the reason. Each row was read in the decoder's `pd.py` at the commit above.
- The 44 pool protocols: ac97, aud, caliper, cec, cjtag, dali, dcf77, dsi, em4100, em4305, flexray, i2s, ieee488, ir_nec, ir_rc5, ir_rc6, ir_sirc, lfast, lpc, maple_bus, mcs48, mdio, microwire, pjdl, qi, rc_encode, rgb_led_ws281x, sae_j1850_vpw, sda2506, sdcard_sd, sdq, seven_segment, sle44xx, spdif, st7735, stepper_motor, swim, t55xx, tdm_audio, tlc5620, usb_power_delivery, wiegand, xy2-100, z80.
- **Nothing for the pool is written before the freeze.** At this commit the repository holds no firmware, model or test for any pool protocol, and none is added before the `rtl-freeze` tag (section 3). The git history of `fw/` and `test/` shows this.

## 3. Freeze

- The census runs on one fixed chip: the commit tagged `rtl-freeze`, which is the tape-out commit (the configuration of the final layout run). The tag comes with a GitHub Release that lists the SHA-256 of `git archive` of `src/`, of the final GDS of that commit's gds run, and of `asm/pe_asm.py` and `model/pe_model.py`. The assembler and the reference model freeze with the RTL.
- The chip clock of the census is the clock that the frozen configuration declares (`info.yaml` `clock_hz`).
- Protocols written before the tag (the 7 programs of the README Flexibility and Hold-out sections) are "tier 0": they run again, unchanged, on the frozen chip and are reported as "written after the instruction set, before the final RTL", not as census results.
- **A defect found during the census** (RTL, assembler, model, test bench): the fix is its own commit with its own reason, it is listed with the results, and every protocol that had a verdict runs again on the fixed version. The verdicts of the fixed version are the reported ones.

## 4. Order: public random beacon

- **Beacon:** drand quicknet (chain hash `52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971`, genesis 1692803367, period 3 s; `https://api.drand.sh/52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971/public/<round>`). Round r is published at 1692803367 + 3 (r - 1) seconds.
- **Round: 34763812**, published 2026-12-12 17:00:00 UTC.
- **If the `rtl-freeze` tag is not pushed by 2026-12-11 17:00 UTC** (24 h before that round), a commit pushed before round 34763812 is published names the replacement round: the first round at least 24 h after the planned tag date. The replacement can be named only before the original round is out, so the order can never be chosen after seeing randomness.
- **Rule.** Let R be the `randomness` field of the round, as 64 lowercase hexadecimal characters. For each pool decoder id d (as in `pool.csv`), compute SHA-256 over the ASCII bytes of R followed directly by d, with no separator. Sort the 44 decoders by this digest as a 64-character lowercase hexadecimal string, ascending. This is the census order.

## 5. Who writes, and what each person may read

Four roles for each protocol, each filled by a writer who did not fill another role for that protocol and has not seen another writer's work for it:

| Role | Reads | Writes |
|---|---|---|
| Limit reader A and limit reader B (independent) | the pinned specification only | the table of timing and level limits (min/max) and one test scenario |
| Peer-model writer | the pinned specification only | the model of the other side of the link (device or host), applying the limits table at its corners |
| Firmware writer | `docs/` at the tag; `fw/` at the tag (public examples); the frozen assembler and reference model as tools; the pinned specification | the firmware and its host-side driver code |

- **Specification.** Before the limit readers start, the specification used for a protocol is pinned: its URL and SHA-256 are committed. Where the standard is not public, the source cited in the decoder's `pd.py` or a public application note is used and named. A specification with no timing limits makes the protocol "hollow": the limits criterion is then reported as not applicable, and the protocol is counted in its own column.
- **Limit disagreements** are settled by rule: the stricter value is used. No one rules by hand.
- **The firmware writer may not read:** `test/`, `flex/` results, the limits table, the peer model, or any other writer's work for the census.
- Writers do not change `src/`, `asm/` or `model/`; a needed change is a defect (section 3) or a FAIL-LIMIT.

## 6. Pass criteria, oracles and verdicts

A protocol is **PASS** if all of these hold:

1. The firmware assembles with the frozen assembler and fits the chip memory, alone (other programs are not required to fit beside it).
2. In the RTL simulation of the frozen chip (cocotb with Icarus Verilog, as `test/`), the host loads and runs the firmware through the SPI host port only, and the chip pins exchange the test scenario with the peer model.
3. **Outside oracle:** the pin waveform of that run, written as VCD, is decoded by the named sigrok decoder at commit `71f4514` (sigrok-cli run as a separate program) with no annotation of a warning or error class, and the decoded content equals the content of the scenario.
4. **Limits:** every limit of the limits table holds at the peer model's timing corners (minimum and maximum of each device time).
5. The same test passes on the gate-level netlist of the `rtl-freeze` gds run. A gate-level failure of an RTL PASS is a defect (section 3).

Verdicts: **PASS**; **PARTIAL** (a named subset of roles or features passes, for example transmit only); **FAIL-LIMIT** (the chip cannot, with the measured or calculated reason: rate, pins, levels); **FAIL-TIMEBOX** (the firmware writer did not finish in the time box). Each verdict is committed when it is reached, so the commit order shows that nothing was dropped or repeated.

SDF timing simulation at the slow corner runs for 5 PASS protocols, chosen as the first 5 PASS protocols in census order.

## 7. Time box and stop rule

- Per protocol: 2 hours of firmware writing and at most 3 runs of the judge (section 6). After that the verdict is the best of what passed, or FAIL-TIMEBOX.
- The census starts when round 34763812 (or its replacement, section 4) is published and the order is committed.
- **Stop:** 2027-01-08 23:59 UTC, or when all 44 have a verdict, whichever comes first. Results never stop it. A stop before the end leaves a prefix of the beacon order, which is a uniform random sample of the pool.

## 8. Prediction (written before any census firmware exists)

Predicted from the present instruction set at 60 MHz: one instruction per thread every 4 cycles, pin events placed to one cycle (16.7 ns), 20 processor pins (5 inputs, 7 outputs, 8 bidirectional), 3.3 V pins with external parts allowed. Each row of `pool.csv` gives the predicted verdict and its reason. The rates in the reasons are the usual rates of each standard. The limit readers check them against the pinned specification. The predictions are not revised afterwards.

| Predicted verdict | Count | Protocols |
|---|---|---|
| FAIL-LIMIT | 4 | ac97 (BIT_CLK 12.288 MHz), flexray (2.5 Mbit/s lowest rate, oversampled receiver), lfast (about 5 Mbit/s), lpc (LCLK 33 MHz) |
| UNCERTAIN | 8 | aud, ieee488, maple_bus, mcs48, sdcard_sd, spdif, xy2-100, z80 |
| PASS | 32 | the other 32 rows of the pool |

Expected result: 32 to 40 PASS of 44.

**Envelope (second denominator).** A protocol is inside the envelope if its specification allows a configuration with a bit rate or clock of 2 MHz or less and with at most 20 chip pins of the directions above. By the prediction, ac97, flexray, lfast and lpc are outside it, so the envelope pool is 40. The limit readers' table decides envelope membership of each protocol, using this rule.

## 9. Analysis and report

Reported in `docs/flexibility.md` and the README, whatever the result:

- n attempted, and the PASS count with the 95 % two-sided Clopper-Pearson interval, for two denominators: (a) all attempted protocols of the pool; (b) the attempted protocols inside the envelope. PARTIAL counts as not PASS in these rates and is listed separately. If the census stops early, the exact hypergeometric bound on the PASS count of the whole pool is given as well.
- Every non-PASS with its reason; predicted against actual verdicts (a table of section 8 against the results).
- The two signal-format rows (seven_segment, stepper_motor) and any hollow protocol in their own column, with the rates also given without them.
- Firmware size (median and maximum words), writing time per protocol, CI minutes per protocol.
- The share of protocols that use no protocol-specific RTL feature: a feature is protocol-specific if one protocol's standard defines it and no other protocol of the census uses it; the usage table (feature by protocol) is measured from the census firmware.
- Tier 0 (section 3) in its own table.

## Record of the commit history change (2026-11-15)

On 2026-11-15 the commit history of this repository was rewritten to set the author of every commit to the engineer. No file content changed. The commit hashes changed with it. This file was first pushed as commit 26b43bf69c44d0e83bb03b3fe219bac230cb4f2a; after the change the same commit is 775a971898943f00c9567bb620b9dc8fd3608f09. The GitHub Actions runs of the first push keep the original server time, 2026-11-15T20:21:51Z: test 43757647585, gds 43757647489, coverage 43757647451, docs 43757647518.
