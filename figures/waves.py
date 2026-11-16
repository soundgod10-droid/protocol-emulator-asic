"""Waveform figures (SVG) of protocol tests on the RTL, from the pin VCD of each test.

What it does:
  .github/workflows/docs.yaml runs protocol tests of test/test_rtl_fw.py on the RTL with the pin dump of
  test/tb.v (make PIN_VCD=1 FST=): the chip pins only, as VCD. This script reads each VCD and takes one part of the
  test: each bench of a test starts with a chip reset, so the parts are the intervals that start at a rising edge
  of rst_n. It measures the protocol times on the pins over the whole part, draws a window of the waveform, and
  marks each measured value and the limit that applies. The I2C times use the measurement function and the limits
  of the test itself (test_fw_full.i2c_times, I2C_LIMITS), and the UART limit is the test's (test_fw_full.SHARE),
  so a figure and its test cannot disagree.

Conditions: logic simulation of the RTL (Icarus Verilog), clock period 16.668 ns (test_rtl_fw CLK_PS, 60 MHz
rounded down); ideal pins and buses: no pad, wire or bus rise time. Times in cycles are exact; times in us are
given at the declared 60 MHz.

Usage: python figures/waves.py <vcd folder> <out folder>
  The vcd folder holds <test name>.vcd for each test in FIGURES.
"""

import bisect
import json
import math
import os
import re
import sys
import traceback
from functools import reduce

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
for sub in ("test", "model", "asm"):
    sys.path.insert(0, os.path.join(ROOT, sub))
import svg as S  # noqa: E402
import test_fw_full as ff  # noqa: E402

F_CLK = 60e6  # declared clock (info.yaml clock_hz)
F_MHZ = 60
CLK_PS = int(re.search(r"^CLK_PS = (\d+)", open(os.path.join(ROOT, "test", "test_rtl_fw.py"), encoding="utf-8").read(),
                       re.M).group(1))
RUN = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ.get('GITHUB_REPOSITORY', '')}"
       f"/actions/runs/{os.environ.get('GITHUB_RUN_ID', '')}")


# ----- VCD -----


class Sig:
    def __init__(self, pairs):
        self.t = [p[0] for p in pairs]
        self.v = [p[1] for p in pairs]

    def at(self, t):
        i = bisect.bisect_right(self.t, t) - 1
        return self.v[i] if i >= 0 else None

    def window(self, t0, t1):
        i = max(bisect.bisect_right(self.t, t0) - 1, 0)
        j = bisect.bisect_right(self.t, t1)
        return list(zip(self.t[i:j], self.v[i:j]))


class Vcd:
    """Value changes of a VCD file: {name: Sig}, times in ps; x and z read as None."""

    def __init__(self, path):
        self.scale_ps = 1
        self.ids = {}
        raw = {}
        with open(path, encoding="ascii", errors="replace") as f:
            header = []
            for line in f:
                header.append(line)
                if "$enddefinitions" in line:
                    break
            self._header(" ".join(header).split(), raw)
            t = 0
            for line in f:
                line = line.strip()
                if not line:
                    continue
                c = line[0]
                if c == "#":
                    t = int(line[1:]) * self.scale_ps
                elif c in "bB":
                    val, ident = line[1:].split()
                    self._set(raw, ident, t, None if any(ch in val for ch in "xXzZ") else int(val, 2))
                elif c in "01xXzZ":
                    self._set(raw, line[1:].strip(), t, int(c) if c in "01" else None)
        self.sigs = {name: Sig(pairs) for name, pairs in raw.items()}

    def _header(self, tok, raw):
        i = 0
        while i < len(tok):
            if tok[i] == "$timescale":
                j = tok.index("$end", i)
                m = re.match(r"(\d+)(fs|ps|ns|us)", "".join(tok[i + 1:j]))
                self.scale_ps = int(m.group(1)) * {"fs": 0.001, "ps": 1, "ns": 1000, "us": 1000000}[m.group(2)]
                i = j
            elif tok[i] == "$var":
                j = tok.index("$end", i)
                ident, name = tok[i + 3], tok[i + 4]
                self.ids.setdefault(ident, []).append(name)
                raw[name] = []
                i = j
            i += 1

    def _set(self, raw, ident, t, v):
        for name in self.ids.get(ident, ()):
            s = raw[name]
            if s and s[-1][0] == t:
                s[-1] = (t, v)
            elif not s or s[-1][1] != v:
                s.append((t, v))

    def __getitem__(self, name):
        return self.sigs[name]


def segments(vcd):
    """(start, end) of each bench: from a rising edge of rst_n to the next falling edge or the end of the dump."""
    r = vcd["rst_n"]
    end = max(s.t[-1] for s in vcd.sigs.values() if s.t)
    rises = [t for i, t in enumerate(r.t) if r.v[i] == 1 and i > 0 and r.v[i - 1] == 0]
    out = []
    for t in rises:
        falls = [u for i, u in enumerate(r.t) if u > t and r.v[i] == 0]
        out.append((t, falls[0] if falls else end))
    return out


def cyc(dt):
    return round(dt / CLK_PS)


def us(cycles):
    return cycles / F_MHZ


def bit(v, k):
    return None if v is None else (v >> k) & 1


def level(out, oe, k):
    """Pin level that the chip drives on uio pin k: its output value with OE = 1, None (released) with OE = 0."""
    if out is None or oe is None:
        return None
    return (out >> k) & 1 if (oe >> k) & 1 else None


def combo(fn, sigs, t0, t1):
    """Changes [(t, value)] of fn(values of sigs) from t0 to t1; the first entry is the value at t0."""
    times = sorted({t0} | {t for s in sigs for t, _ in s.window(t0, t1) if t > t0})
    out = []
    for t in times:
        v = fn(*[s.at(t) for s in sigs])
        if not out or out[-1][1] != v:
            out.append((t, v))
    return out


def edges(ch, kind=None):
    """Times of the changes in ch (kind 'rise' or 'fall' for 0 -> 1 or 1 -> 0 only)."""
    out = []
    for (t, v), (_, p) in zip(ch[1:], ch):
        if kind is None or (kind == "rise" and p == 0 and v == 1) or (kind == "fall" and p == 1 and v == 0):
            out.append(t)
    return out


def value_at(ch, t):
    i = bisect.bisect_right([c[0] for c in ch], t) - 1
    return ch[i][1] if i >= 0 else None


# ----- drawing -----


def nice_step(span_us, target=10):
    for s in (0.05, 0.1, 0.2, 0.25, 0.5, 1, 2, 2.5, 5, 10, 20, 25, 50, 100, 200, 500, 1000):
        if span_us / s <= target:
            return s
    return 2000


def draw(path, title, subtitle, lanes, t0, t1, marks, table, notes):
    """lanes: [dict(name, sub, ch=[(t, v)], labels=[(t, text)], top=[(t, text)])]; marks: [dict(lane, ta, tb, text,
    colour, pos='top'|'bottom')]; table: [(quantity, measured, limit, source, ok)]."""
    W, left, right = 1120, 190, 24
    lane_h, top = 58, 92
    plot_w = W - left - right
    n_tab, n_notes = len(table), len(notes)
    H = top + lane_h * len(lanes) + 44 + 30 + 22 * (n_tab + 1) + 16 * n_notes + 26
    s = S.Svg(W, H)
    X = lambda t: left + (t - t0) / (t1 - t0) * plot_w  # noqa: E731
    s.text(20, 30, title, size=18, weight="bold")
    s.text(20, 52, subtitle, size=12, colour="muted")

    span = (t1 - t0) / 1e6
    step = nice_step(span)
    k = 0
    while k * step <= span + 1e-9:
        x = X(t0 + k * step * 1e6)
        s.line(x, top - 8, x, top + lane_h * len(lanes), colour=S.GRID, sw=1)
        s.text(x, top + lane_h * len(lanes) + 16, f"{S.fmt(k * step)} µs", size=10.5, anchor="middle", colour="muted")
        k += 1
    s.text(left, top + lane_h * len(lanes) + 34, f"time from the left edge of the window; grid {S.fmt(step)} µs",
           size=10.5, colour="muted")

    for i, lane in enumerate(lanes):
        y0 = top + i * lane_h
        hi, lo = y0 + 12, y0 + 36
        mid = (hi + lo) / 2
        s.text(20, mid - 2, lane["name"], size=12.5, weight="bold")
        if lane.get("sub"):
            s.text(20, mid + 13, lane["sub"], size=10.5, colour="muted", mono=True)
        ch = [(max(t, t0), v) for t, v in lane["ch"] if t <= t1]
        segs = []
        for j, (t, v) in enumerate(ch):
            te = ch[j + 1][0] if j + 1 < len(ch) else t1
            if te > t:
                segs.append((t, te, v))
        run, prev = [], None
        y_of = lambda v: mid if v is None else (hi if v else lo)  # noqa: E731
        for ta, tb, v in segs:
            if v is None:
                if run:
                    s.path(run, sw=1.6)
                    run = []
                if prev is not None:
                    s.line(X(ta), y_of(prev), X(ta), mid, colour=S.MUTED, sw=1, dash="2 2")
                s.line(X(ta), mid, X(tb), mid, colour=S.MUTED, sw=1.2, dash="4 3")
            else:
                if prev is None and segs[0][0] != ta:
                    s.line(X(ta), mid, X(ta), y_of(v), colour=S.MUTED, sw=1, dash="2 2")
                if run:
                    run.append((X(ta), y_of(v)))
                else:
                    run = [(X(ta), y_of(v))]
                run.append((X(tb), y_of(v)))
            prev = v
        if run:
            s.path(run, sw=1.6)
        for t, text in lane.get("labels", ()):
            if t0 <= t <= t1:
                s.text(X(t), lo + 15, text, size=9.5, anchor="middle", mono=True, colour="muted")
        for t, text in lane.get("top", ()):
            if t0 <= t <= t1:
                s.text(X(t), y0 + 4, text, size=10.5, anchor="middle", weight="bold", colour="line")

    for m in marks:
        y0 = top + m["lane"] * lane_h
        y = y0 + (3 if m.get("pos", "top") == "top" else 50)
        c = m.get("colour", "meas")
        xa, xb = X(m["ta"]), X(m["tb"])
        s.line(xa, y0 + 8, xa, y0 + 40, colour=S.COLOURS[c], sw=0.8, dash="2 2")
        s.line(xb, y0 + 8, xb, y0 + 40, colour=S.COLOURS[c], sw=0.8, dash="2 2")
        s.line(xa, y, xb, y, colour=c, sw=1.3, start=True, end=True)
        tx = m.get("tx", (xa + xb) / 2)
        s.text(tx, y - 4 if m.get("pos", "top") == "top" else y + 13, m["text"], size=10.5, anchor="middle",
               colour=c, weight="bold")

    y = top + lane_h * len(lanes) + 66
    cols = (20, 330, 575, 745, 1060)
    for x, head in zip(cols, ("Quantity (whole test part)", "Measured", "Limit", "Source of the limit", "Result")):
        s.text(x, y, head, size=11.5, weight="bold")
    s.line(20, y + 6, W - 20, y + 6, colour=S.GRID, sw=1)
    for r, (q, meas, lim, src, ok) in enumerate(table):
        yy = y + 22 * (r + 1)
        s.text(cols[0], yy, q, size=11.5)
        s.text(cols[1], yy, meas, size=11.5, colour="meas", weight="bold")
        s.text(cols[2], yy, lim, size=11.5, colour="limit", weight="bold")
        s.text(cols[3], yy, src, size=10.5, colour="muted")
        s.text(cols[4], yy, "pass" if ok else "FAIL", size=11.5, weight="bold", colour="meas" if ok else "limit")
    yy = y + 22 * (n_tab + 1) + 10
    for k2, note in enumerate(notes):
        s.text(20, yy + 16 * k2, note, size=10.5, colour="muted")
    s.save(path, title, subtitle)


COMMON = [
    "Logic simulation of the RTL (Icarus Verilog), clock period 16.668 ns; ideal pins: no pad, wire or bus rise time.",
]


def note_run(test):
    return f"Test {test} in test/test_rtl_fw.py, pin VCD of the docs workflow run {RUN}."


def host_lane(vcd, t0, t1):
    return dict(name="HOST_CS_N", sub="ui_in[6], host port",
                ch=combo(lambda v: bit(v, 6), [vcd["ui_in"]], t0, t1))


# ----- UART -----


def fig_uart(vcd, out):
    """test_rtl_uart_tx, part 2: 115,200 bit/s 8N1 (the parts are 1 Mbit/s, 115,200 bit/s, 9,600 bit/s)."""
    ts, te = segments(vcd)[1]
    rate = 115_200
    b_nom = F_CLK / rate
    tx = combo(lambda v: bit(v, 4), [vcd["uo_out"]], ts, te)
    starts, ready = [], -1
    for t in edges(tx, "fall"):
        if t > ready:
            starts.append(t)
            ready = t + 9.5 * b_nom * CLK_PS
    offs = [cyc(t - max(s0 for s0 in starts if s0 <= t)) for t in edges(tx) if t >= starts[0]]
    gaps = [cyc(b - a) for a, b in zip(starts, starts[1:])]
    b = reduce(math.gcd, [o for o in offs if o] + gaps)
    err = b_nom / b - 1
    t_s0 = starts[0]
    bt = b * CLK_PS
    labels, topl = [], []
    for f, s0 in enumerate(starts[:2]):
        bits = [value_at(tx, s0 + (k + 0.5) * bt) for k in range(10)]
        byte = sum(v << i for i, v in enumerate(bits[1:9]))
        for k, name in enumerate(["S"] + [str(i) for i in range(8)] + ["P"]):
            labels.append((s0 + (k + 0.5) * bt, name))
        topl.append((s0 + 5 * bt, f"frame {f + 1}: 0x{byte:02X}"))
    lanes = [dict(name="UART TX", sub="uo_out[4]", ch=tx, labels=labels, top=topl), host_lane(vcd, ts, te)]
    marks = [dict(lane=0, ta=t_s0 + bt, tb=t_s0 + 2 * bt, text=f"1 bit = {b} cycles = {us(b):.3f} µs", pos="bottom"),
             dict(lane=1, ta=t_s0, tb=t_s0 + 10 * bt, text=f"frame = 10 bits = {us(10 * b):.2f} µs", pos="bottom")]
    ok_err = abs(err) <= ff.SHARE
    table = [
        ("Rate error against 115,200 bit/s", f"{err * 100:+.3f} % ({b} cycles per bit)",
         f"within ±{ff.SHARE * 100:g} %", "design share of the UART clock budget (test_fw_full.SHARE)", ok_err),
        ("Edges on the bit grid", f"all {len(offs)} edges at k × {b} cycles", "every edge",
         "receiver model test_fw_full.check_tx", all(o % b == 0 for o in offs)),
        ("Frames back to back", f"{len(starts)} frames, start to start {sorted(set(gaps))[0]:,} cycles",
         f"10 bits = {10 * b:,} cycles", "check_tx", set(gaps) == {10 * b}),
    ]
    notes = COMMON + [
        "UART has no standard timing limit; the limit is the part of the clock budget that this transmitter may use.",
        note_run("test_rtl_uart_tx (part 2 of 3)")]
    draw(out, "UART transmitter on the RTL: 115,200 bit/s, 8N1",
         f"fw/uart_tx_full.s at 60 MHz; first 2 of {len(starts)} frames; every bit edge is a timed OUT event",
         lanes, t_s0 - 1.0 * bt, t_s0 + 21 * bt, marks, table, notes)
    return dict(bit_cycles=b, rate_error_pct=err * 100, frames=len(starts), edges=len(offs), ok=ok_err)


# ----- SPI -----


def fig_spi(vcd, out):
    """test_rtl_spi_ctl, part 1: mode 0 (CPOL 0, CPHA 0), D 0, smallest half period H."""
    ts, te = segments(vcd)[0]
    o, e, i = vcd["uio_out"], vcd["uio_oe"], vcd["uio_in"]
    cs = combo(lambda a, b: level(a, b, ff.P_CS), [o, e], ts, te)
    sck = combo(lambda a, b: level(a, b, ff.P_SCK), [o, e], ts, te)
    mosi = combo(lambda a, b: level(a, b, ff.P_MOSI), [o, e], ts, te)
    miso = combo(lambda v: bit(v, ff.P_MISO), [i], ts, te)
    falls, rises = edges(cs, "fall"), edges(cs, "rise")
    xfers = [(f0, min(r for r in rises if r > f0)) for f0 in falls if any(r > f0 for r in rises)]
    halves, setup, hold, lead, lag, bytes_mosi, bytes_miso = set(), [], [], [], [], [], []
    mosi_t = edges(mosi)
    for f0, r0 in xfers:
        ed = [t for t in edges(sck) if f0 < t < r0]
        lead.append(cyc(ed[0] - f0))
        lag.append(cyc(r0 - ed[-1]))
        for k in range(0, len(ed) - 15, 16):
            grp = ed[k:k + 16]
            halves |= {cyc(b - a) for a, b in zip(grp, grp[1:])}
        bm = bs = 0
        for n, t in enumerate(t for t in ed if value_at(sck, t) == 1):
            before = [u for u in mosi_t if u < t]
            after = [u for u in mosi_t if u > t]
            if before and before[-1] > f0:
                setup.append(cyc(t - before[-1]))
            if after and after[0] < r0:
                hold.append(cyc(after[0] - t))
            bm = (bm << 1) | (value_at(mosi, t - 1) or 0)
            bs = (bs << 1) | (value_at(miso, t - 1) or 0)
            if n % 8 == 7:
                bytes_mosi.append((t, bm & 0xFF))
                bytes_miso.append((t, bs & 0xFF))
                bm = bs = 0
    h = min(halves)
    f0, r0 = xfers[0]
    ed0 = [t for t in edges(sck) if f0 < t < r0]
    rise0 = [t for t in ed0 if value_at(sck, t) == 1]
    lab_mosi = [(t - 3.5 * 2 * h * CLK_PS, f"0x{v:02X}") for t, v in bytes_mosi if f0 < t < r0]
    lab_miso = [(t - 3.5 * 2 * h * CLK_PS, f"0x{v:02X}") for t, v in bytes_miso if f0 < t < r0]
    lanes = [dict(name="CS", sub="uio[0]", ch=cs), dict(name="SCK", sub="uio[3]", ch=sck),
             dict(name="MOSI", sub="uio[1], chip out", ch=mosi, top=lab_mosi),
             dict(name="MISO", sub="uio[2], device", ch=miso, top=lab_miso), host_lane(vcd, ts, te)]
    marks = [dict(lane=1, ta=rise0[1], tb=rise0[2], text=f"SCK period {2 * h} cycles = {1000 * us(2 * h):.0f} ns",
                  pos="bottom"),
             dict(lane=0, ta=f0, tb=ed0[0], text=f"CS to SCK {lead[0]} cycles", pos="bottom")]
    one_h = len(halves) == 1
    table = [
        ("SCK half periods inside each byte", f"{sorted(halves)} cycles, SCK {F_CLK / 2e6 / h:.2f} MHz",
         "one value H for all", "test rule (test_fw_full.spi_half_periods)", one_h),
        ("MOSI set-up before the sample edge", f"min {min(setup)} cycles = {1000 * us(min(setup)):.0f} ns",
         "> 0 (stable at the edge)", "device model samples at the edge", min(setup) > 0),
        ("MOSI hold after the sample edge", f"min {min(hold)} cycles = {1000 * us(min(hold)):.0f} ns",
         "> 0", "device model (test_fw_full.SpiDevice)", min(hold) > 0),
        ("Bytes exchanged (both transfers)", f"MOSI {' '.join(f'{v:02X}' for _, v in bytes_mosi)}; "
         f"MISO {' '.join(f'{v:02X}' for _, v in bytes_miso)}", "as sent", "test_rtl_spi_ctl",
         [v for _, v in bytes_mosi] == [w & 0xFF for w in ff.SPI_WORDS] and [v for _, v in bytes_miso] == ff.SPI_DEV),
    ]
    notes = COMMON + [
        "SPI has no standard timing limits; a device data sheet sets them. Limits here: the rules of the test's device model.",
        note_run("test_rtl_spi_ctl (part 1 of 8: mode 0, D 0)")]
    draw(out, f"SPI controller on the RTL: mode 0, SCK {F_CLK / 2e6 / h:.1f} MHz",
         "fw/spi_ctl.s at 60 MHz; first transfer: 3 bytes with CS low; MISO from the independent device model",
         lanes, f0 - 40 * CLK_PS * 2, r0 + 40 * CLK_PS * 2, marks, table, notes)
    return dict(half_period=h, sck_mhz=F_CLK / 2e6 / h, setup_min=min(setup), hold_min=min(hold), ok=one_h)


# ----- I2C -----


def fig_i2c(vcd, out):
    """test_rtl_i2c, part 1: Fast-mode, no clock stretching."""
    ts, te = segments(vcd)[0]
    ui, oe, uo = vcd["uio_in"], vcd["uio_oe"], vcd["uio_out"]
    bus = combo(lambda v: (bit(v, ff.P_SCL), bit(v, ff.P_SDA)), [ui], ts, te)
    t_idle = next(t for t, v in bus if v == (1, 1))
    bus = [(t, v) for t, v in bus if t >= t_idle]
    changes = [(cyc(t - t_idle), v) for t, v in bus]
    times = ff.i2c_times(changes)
    lim = ff.I2C_LIMITS["Fm"]
    scl = [(t, v[0]) for t, v in bus]
    sda = [(t, v[1]) for t, v in bus]
    chip_sda = combo(lambda a, b: 0 if level(a, b, ff.P_SDA) == 0 else 1, [uo, oe], t_idle, te)
    start = next(t for (t, v), (_, p) in zip(bus[1:], bus) if p == (1, 1) and v == (1, 0))
    sr = [t for t in edges(scl, "rise") if t > start][:9]
    sf = [t for t in edges(scl, "fall") if t > start][:10]
    bits = [value_at(sda, t) for t in sr]
    names = ["A6", "A5", "A4", "A3", "A2", "A1", "A0", "R/W", "ACK"]
    labels = [(t, f"{n}={b}") for t, n, b in zip(sr, names, bits)]
    addr = sum(b << (6 - k) for k, b in enumerate(bits[:7]))
    topl = [(sr[3], f"address 0x{addr:02X}, {'read' if bits[7] else 'write'}"), (sr[8], "ACK from the target")]
    lanes = [dict(name="SCL", sub="uio[6], bus", ch=scl), dict(name="SDA", sub="uio[7], bus", ch=sda, labels=labels,
                                                                 top=topl),
             dict(name="SDA pulled low", sub="by the chip", ch=chip_sda)]
    marks = [dict(lane=0, ta=sf[2], tb=sr[3], text=f"tLOW {us(cyc(sr[3] - sf[2])):.2f} µs", pos="top"),
             dict(lane=0, ta=sr[3], tb=sf[3], text=f"tHIGH {us(cyc(sf[3] - sr[3])):.2f} µs", pos="bottom"),
             dict(lane=1, ta=start, tb=sf[0], text=f"tHD;STA {us(cyc(sf[0] - start)):.2f} µs", pos="bottom",
                  tx=None)]
    marks[2].pop("tx")
    rows, ok_all = [], True
    names_t = dict(low="tLOW, SCL low", high="tHIGH, SCL high", hd_sta="tHD;STA, START hold",
                   su_sta="tSU;STA, repeated START set-up", su_sto="tSU;STO, STOP set-up",
                   buf="tBUF, bus free STOP to START", su_dat="tSU;DAT, data set-up", hd_dat="tHD;DAT, data hold")
    for key in ("low", "high", "hd_sta", "su_sta", "su_sto", "buf", "su_dat", "hd_dat"):
        if not times[key]:
            continue
        got = min(times[key])
        need = math.ceil(lim[key] * F_MHZ - 1e-9)
        ok = got >= need
        ok_all &= ok
        rows.append((names_t[key], f"min {got} cycles = {us(got):.3f} µs", f"≥ {lim[key]:g} µs",
                     "UM10204 Rev. 7.0 Table 11, Fast-mode", ok))
    period = min(times["period"])
    f_khz = F_MHZ * 1000 / period
    ok = period >= math.ceil(F_MHZ * 1000 / lim["f_khz"] - 1e-9)
    ok_all &= ok
    rows.insert(0, ("fSCL, SCL clock frequency", f"max {f_khz:.1f} kHz (period {period} cycles)",
                    f"≤ {lim['f_khz']} kHz", "UM10204 Rev. 7.0 Table 11, Fast-mode", ok))
    notes = COMMON + [
        "Bus levels with ideal pull-ups: a real bus adds the rise time (Fast-mode tr ≤ 300 ns), set by the pull-up and the bus capacitance.",
        "Measured with the test's own function test_fw_full.i2c_times over the whole part (write, repeated START, read, NACKed address).",
        note_run("test_rtl_i2c (part 1: Fast-mode, no clock stretching)")]
    draw(out, f"I2C controller on the RTL: Fast-mode, {f_khz:.1f} kHz",
         "fw/i2c_ctl.s at 60 MHz, open drain; START and address byte; the target is the independent bus model",
         lanes, start - 2.0e6, sr[8] + 2.0e6, marks, rows, notes)
    return dict(f_khz=f_khz, mins={k: min(v) for k, v in times.items() if v and k != "both"}, ok=ok_all)


# ----- CAN -----


def fig_can(vcd, out):
    """test_rtl_can, part 1: the chip sends frames to a node with +0.5 % clock and 10 cycles of loop delay."""
    ts, te = segments(vcd)[0]
    ui, oe, uo = vcd["ui_in"], vcd["uio_oe"], vcd["uio_out"]
    rx = combo(lambda v: bit(v, 4), [ui], ts, te)
    txa = combo(lambda a, b: 0 if level(a, b, 4) == 0 else 1, [uo, oe], ts, te)
    txb = combo(lambda a, b: 0 if level(a, b, 5) == 0 else 1, [uo, oe], ts, te)
    sof = edges(txa, "fall")[0]
    bt_nom = round(F_CLK / 250_000)
    dom = edges(txa, "fall") + edges(txa, "rise")
    frame_end = sof
    for t in sorted(t for t in edges(rx) if t > sof):
        if t - frame_end > 8 * bt_nom * CLK_PS:
            break
        frame_end = t
    in_frame = [t for t in sorted(dom) if sof <= t <= frame_end]
    offs = [cyc(t - sof) for t in in_frame if t > sof]
    b = reduce(math.gcd, offs)
    ack = None
    for t in edges(rx, "fall"):
        if sof < t <= frame_end and value_at(txa, t) == 1 and value_at(txa, t - 20 * CLK_PS) == 1:
            ack = t
    marks = [dict(lane=1, ta=sof, tb=sof + b * CLK_PS, text=f"1 bit = {b} cycles = {us(b):.3f} µs", pos="bottom")]
    topl = [(sof + b * CLK_PS / 2, "SOF")]
    if ack is not None:
        topl.append((ack + b * CLK_PS / 2, "ACK from the node"))
    lanes = [dict(name="CAN bus", sub="ui_in[4], RX", ch=rx, top=topl),
             dict(name="TXA dominant", sub="uio[4], thread 0", ch=txa),
             dict(name="TXB dominant", sub="uio[5], thread 1", ch=txb), host_lane(vcd, ts, te)]
    ok_b = b == bt_nom
    table = [
        ("Bit time on every TXA edge of the frame", f"{b} cycles = {us(b):.3f} µs ({len(offs)} edges)",
         f"{bt_nom} cycles = 4 µs, 250 kbit/s", "nominal bit rate of the test (Bosch CAN 2.0 Part A)", ok_b),
        ("ACK slot", "dominant from the node" if ack else "not found", "dominant (frame received)",
         "Bosch CAN 2.0 Part A, ACK field", ack is not None),
        ("Other node", "+0.5 % clock, 10-cycle loop delay", "every frame correct", "test_rtl_can (test condition)",
         True),
    ]
    notes = COMMON + [
        "TXA and TXB are open drain and joined to the transceiver TXD on the board (docs/info.md); the bus model adds the loop delay.",
        note_run("test_rtl_can (part 1 of 3: chip to node)")]
    draw(out, "CAN 2.0A node on the RTL: 250 kbit/s",
         "fw/can.s on two threads at 60 MHz; the first frame that the chip sends; the other node is the independent model",
         lanes, sof - 4 * b * CLK_PS, frame_end + 6 * b * CLK_PS, marks, table, notes)
    return dict(bit_cycles=b, ack=ack is not None, ok=ok_b and ack is not None)


FIGURES = (("test_rtl_uart_tx", "wave_uart.svg", fig_uart), ("test_rtl_spi_ctl", "wave_spi.svg", fig_spi),
           ("test_rtl_i2c", "wave_i2c.svg", fig_i2c), ("test_rtl_can", "wave_can.svg", fig_can))


def main(vcd_dir, out_dir):
    os.makedirs(out_dir, exist_ok=True)
    result, failed = {}, []
    for test, name, fn in FIGURES:
        try:
            vcd = Vcd(os.path.join(vcd_dir, test + ".vcd"))
            result[test] = fn(vcd, os.path.join(out_dir, name))
            result[test]["figure"] = name
            print(f"{name}: {result[test]}")
            if not result[test]["ok"]:
                failed.append(test)
        except Exception:  # report every figure, then fail
            text = traceback.format_exc()
            print(text)
            print(f"::error title=waveform {test}::" + text.replace("%", "%25").replace("\n", "%0A"))
            failed.append(test)
    with open(os.path.join(out_dir, "waves.json"), "w", encoding="utf-8") as f:
        json.dump(dict(run=RUN, figures=result), f, indent=1)
    if failed:
        sys.exit(f"waveform figures failed or show a failed limit: {failed}")


if __name__ == "__main__":
    main(*sys.argv[1:])
