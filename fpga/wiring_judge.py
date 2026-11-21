# SPDX-License-Identifier: Apache-2.0
"""Host side of the wiring check (fpga/wiring_check.v): frame decoder and judge.

What it does:
  decode(samples, div)   finds the frames in one host input's samples (one sample per FPGA clock cycle, or any
                         rate with div samples a bit): start bit, 4 bytes LSB first, stop bits, the index byte
                         0xA0-0xAF and the check byte must agree. Returns (start sample, index, ui, rst_n).
  judge(...)             names every wrong wire: for each host input, the FPGA pin whose index it decodes (none:
                         open, stuck or shorted; another one: swapped or moved); for each host output, the ui bits
                         that follow it when it alone is high (none: open; another: moved; two or more: shorted),
                         ui bits that stay high with every host output low (stuck high, e.g. an open wire with the
                         pull-up), and the rst_n wire.

Why:
  The bench host program (RP2350) and the simulation test (fpga/test_wiring_check.py) use the same judge, so the
  test shows that the judge names a moved wire before the first bench day.
"""

FPGA_OUT = ["uo[%d]" % k for k in range(8)] + ["uio[%d]" % k for k in range(8)]


def decode(samples, div):
    frames = []
    n = len(samples)
    i = 0
    while i < n - 1:
        if samples[i] == 1 and samples[i + 1] == 0:
            t0 = i + 1
            pos = [t0 + div // 2 + k * div for k in range(40)]
            if pos[-1] >= n:
                break
            bits = [samples[p] for p in pos]
            seg = [bits[b * 10:(b + 1) * 10] for b in range(4)]
            if all(s[0] == 0 and s[9] == 1 for s in seg):
                v = [sum(s[1 + j] << j for j in range(8)) for s in seg]
                if v[0] >> 4 == 0xA and v[3] == v[0] ^ v[1] ^ v[2] ^ 0x5A and v[2] in (0, 1):
                    frames.append((t0, v[0] & 0x0F, v[1], v[2]))
                    i = t0 + 40 * div
                    continue
        i += 1
    return frames


def judge(out_frames, out_expected, in_steps, in_expected, rst_steps=None):
    """out_frames: {host input: frames from decode}; out_expected: {host input: FPGA output index 0-15};
    in_steps: [(host outputs driven high (set), ui byte seen)]; in_expected: {host output: ui bit};
    rst_steps: [(rst_n driven, rst_n seen)]. Returns the list of faults (empty: wiring correct)."""
    faults = []
    for h in sorted(out_expected):
        e = out_expected[h]
        ids = sorted({f[1] for f in out_frames.get(h, [])})
        if not ids:
            faults.append("host input %d: no valid frame (open, stuck or shorted wire; expected %s)" % (h, FPGA_OUT[e]))
        elif ids != [e]:
            faults.append("host input %d: wired to %s, expected %s" % (h, " and ".join(FPGA_OUT[x] for x in ids),
                                                                      FPGA_OUT[e]))
    low = [ui for high, ui in in_steps if not high]
    stuck = 0
    for ui in low:
        stuck |= ui
    for b in range(8):
        if stuck >> b & 1:
            faults.append("ui[%d]: high with every host output low (open wire with the pull-up, or stuck)" % b)
    for h in sorted(in_expected):
        e = in_expected[h]
        seen = [ui for high, ui in in_steps if high == {h}]
        if not seen:
            faults.append("host output %d: not driven alone in any frame" % h)
            continue
        follow = 0xFF
        for ui in seen:
            follow &= ui
        follow &= ~stuck & 0xFF
        bits = [b for b in range(8) if follow >> b & 1]
        if not bits:
            faults.append("host output %d: reaches no ui pin (open wire; expected ui[%d])" % (h, e))
        elif bits != [e]:
            faults.append("host output %d: reaches %s, expected ui[%d]" % (h, " and ".join("ui[%d]" % b for b in bits),
                                                                          e))
    if rst_steps is not None:
        if not any(d == 0 for d, s in rst_steps) or not any(d == 1 for d, s in rst_steps):
            faults.append("rst_n: not driven both ways")
        elif any(d != s for d, s in rst_steps):
            faults.append("rst_n: the FPGA sees %s while the host drives %s" % (
                [s for d, s in rst_steps], [d for d, s in rst_steps]))
    return faults
