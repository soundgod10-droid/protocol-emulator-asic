# Blind arm: protocols written from the specification only, on the frozen chip

This file fixes, before the beacon round below is published, which protocols of the candidate list go to a blind arm and how they are written and judged. It is committed and pushed more than 24 hours before that round; the push and the CI run of this commit give the GitHub server time. Nothing in this file is changed after the push; a correction or a decision that this file leaves open is a new, dated section at the end, and the original text stays.

## 1 Question

Can a writer who has never seen the firmware of this chip make it run a protocol from the protocol's specification and the chip's documents alone? The census ([PREREG.md](PREREG.md)) answers whether the frozen chip runs protocols of an outside catalogue; this arm adds writer independence: the writer reads no example program.

## 2 Which protocols: chosen by nobody

- **List:** the rows of [candidates.csv](candidates.csv) with `list` = `C` (41 rows), as committed in this push. Rule and order: [docs/counting.md](../docs/counting.md) section 7.
- **Beacon:** drand quicknet (chain hash `52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971`, genesis 1692803367, period 3 s; `https://api.drand.sh/52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971/public/<round>`), as in PREREG section 4.
- **Round: 34159012**, published 2026-11-21 17:00:00 UTC (1692803367 + 3 x 34159011 s).
- **Rule.** Let R be the `randomness` field of the round, as 64 lowercase hexadecimal characters. For each id d of list C, compute SHA-256 over the ASCII bytes of R followed directly by d. Sort the ids by this digest as a lowercase hexadecimal string, ascending. The first 8 are the blind arm B. The other rows form the pre-freeze list P, in the score order of docs/counting.md.
- **Order inside B:** the same rule with the census round of PREREG section 4 (34763812, or its replacement), applied to the 8 ids of B.
- **Check:** `python flex/order.py --round 34159012` prints B and P. The core of the rule is four lines:

```python
import csv, hashlib
R = "<randomness of round 34159012>"
ids = [r["id"] for r in csv.DictReader(open("flex/candidates.csv")) if r["list"] == "C"]
print(sorted(ids, key=lambda d: hashlib.sha256((R + d).encode()).hexdigest())[:8])
```

## 3 Nothing for B before the tag

No commit before the `rtl-freeze` tag adds firmware, a model or a test for a protocol of B. Check: every commit before the tag that touches `fw/` or `test/` is listed with `git log --before=<tag time> --name-only -- fw test`; no file name and no commit message names an id of B or its protocol.

## 4 What the writer gets: the sealed bundle

- The bundle is `git archive rtl-freeze docs asm model` and, for each protocol, its pinned specification (address and SHA-256, committed before the writer starts, as PREREG section 5).
- It has no `fw/` and no `test/`: the writer sees no example program and no test.
- The SHA-256 of the bundle is committed before the writer starts.
- A writer who is a person may get `fw/` at the tag as well; that variant is reported in its own column.

## 5 Feedback and time box

- The writer may use the frozen assembler and the reference model of the bundle as tools.
- The writer may ask for at most 3 runs of the judge (section 6); each run returns only PASS or FAIL for each criterion.
- Time box for each protocol: 2 hours of firmware writing for a software writer (as PREREG section 7), 8 hours for a person. After it, the verdict is the best of what passed, or FAIL-TIMEBOX.

## 6 Oracles and verdicts

- Two limit readers and a peer-model writer, each from the pinned specification only, as PREREG section 5; none of them is the firmware writer.
- The pass criteria and verdicts of PREREG section 6: assembly and fit, the RTL test through the SPI host port with the peer model, the sigrok decoder at `71f4514` where one exists (else the cell says so), the limits, and the same test on the gate-level netlist of the `rtl-freeze` gds run.
- Each verdict is committed when it is reached.

## 7 Record

For each protocol: the bundle hash; the commits of the writer's start and end (GitHub server time); a statement of what the writer read and which tools the writer used; the judge runs and their results; the verdict. The writer's working notes and session logs are not published.

## 8 Report

Each writer type is reported in its own column, never pooled with the census, with the rows written by the design team, or with another writer type. The report gives n, the verdicts, the time used and the judge runs used.

## 9 The writer

The writer type is not fixed by this file: a person outside the project, or an AI model given the bundle only (from the vendor of the design tools or from another vendor), or a mix with each protocol assigned before the writer starts. The choice is recorded before the tag in a dated section at the end of this file. The rules of sections 2 to 8 hold for every writer type.

## 10 Conditions

Every result of this arm is a logic-simulation result (RTL and functional gate-level netlist at the declared clock, ideal pins), like the census. Electrical needs (transceivers, level shifters) are named per protocol and not simulated.

## Record: beacon round 34159012 (2026-11-21)

- **Round read** 2026-11-21 at about 17:57 UTC, after its publication, from `https://api.drand.sh/52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971/public/34159012`:
  - round `34159012`
  - randomness `a08eacf123483fa9dc7089ba9459360735c113f0b6f520cf4b721ad3bcccba9b`
  - signature `b8092c5860421cd7018845802cc6bf37e75fcb7ad5acfca4653c6a7d220e3cd2e3df712a2401c45aee413b816c7b862c`
- **Checks:** the randomness is the SHA-256 of the signature bytes; the round time is 1692803367 + 3 x 34159011 = 1795280400 s = 2026-11-21T17:00:00Z; the latest round at the time of reading was 34160144 (2026-11-21T17:56:36Z), so the round was public before this record. flex/order.py and flex/candidates.csv are the files of the commits 7bc7479 and 5fb04b8 (pushed 2026-11-17, more than 24 hours before the round); neither was changed since.
- **Result** of `python flex/order.py a08eacf123483fa9dc7089ba9459360735c113f0b6f520cf4b721ad3bcccba9b` (the same as `python flex/order.py --round 34159012`), also in [blind_order.txt](blind_order.txt):
  - **blind arm B** (8, in SHA-256(R + id) order): pocsag, j1708, ds1302, biss, hdlc, ata, magstripe, snes;
  - **pre-freeze list P** (33, score order): dmx512, dcc, sbus, lin, hd44780, hx711, irda, oregon, ssi, tm1637, mdb, psi5, tlc5940, unio, i3c, iso7816, sent, adb, hub75, joybus, x10, iolink, mil1553, sdi12, centronics, sega6, arinc429, asi, kline, lanc, mbp, endat, knx.
- The order inside B is set later with the census round (section 2). From this record on, section 3 applies to the eight ids of B: no commit before the `rtl-freeze` tag adds firmware, a model or a test for any of them. The writer type (section 9) stays open.
