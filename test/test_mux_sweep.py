"""Multiplexer delay sweep of the helper's clocked mode: the board-side delay at which each rate first fails.

What it does:
  Runs the T1 (SPI controller against the serial flash model) and T2 (JTAG chain) benches of test_slu_clk.py
  unchanged, on the unit instance tb.u_slc_ut, with their pin-path corner table rebuilt for each step of a sweep.
  The pad delays stay those of docs/verification.md section 9; the multiplexer delay of the shuttle's pin path is
  the swept parameter:
  - spread sweep: the multiplexer delay is anywhere from 0 to M ns on each pin and each way (the four corners of
    test_slu_clk.py with M instead of 10 ns: fast pads and no multiplexer, slow pads and M, and both skews). One
    read-back setting (sample delay 0 to 15 cycles, rising or falling-edge copy of DIN) must work at all four
    corners. The result is the smallest M at which no setting works (resolution 0.25 ns), found by 1 or 2 ns steps
    from 0 and bisection of the last step;
  - known-delay sweep (30 MHz SPI): the same delay m on every pin, pads at their corners and skews; the read-back
    setting is chosen for that m, as at bring-up after the loopback measurement (docs/bench.md);
  - control sweep: the 30 MHz SPI control of test_slc_spi_flash_7m5_30mhz (sample delay 0, slow pads) at
    multiplexer delays 0 to 10 ns, to show from which delay the control fails.

Why:
  The multiplexer delay of TTIHP27a is not measured; the published figure is an estimate of about 10 ns round trip
  through the pads and the multiplexer. These sweeps give, for each multiplexer-dependent rate, the delay at which it
  first fails and so the margin against that estimate (GC-4). Logic simulation with delay parameters at 60 MHz; the
  pads and the multiplexer are not simulated electrically, and the device models use their data-sheet times.
"""

import os
import sys

import cocotb

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_slu_clk as sc  # noqa: E402  T1/T2 benches and device models, unchanged

GL = sc.GL
STEP = 0.25                                      # ns, resolution of the first failing delay
SETTINGS = [(d, f) for f in (0, 1) for d in range(16)]


def corners(mux_max, mux_min=0.0):
    """Pin-path corners (clock out, data out, input) in ns: pads of docs/verification.md section 9 plus a multiplexer
    delay from mux_min to mux_max on each pin and each way."""
    o, i = sc.OUT_PAD, sc.IN_PAD
    return {
        "fast": (o[0] + mux_min, o[0] + mux_min, i[0] + mux_min),
        "slow": (o[1] + mux_max, o[1] + mux_max, i[1] + mux_max),
        "clock slow, data fast": (o[1] + mux_max, o[0] + mux_min, i[0] + mux_min),
        "clock fast, data slow": (o[0] + mux_min, o[1] + mux_max, i[1] + mux_max),
    }


def round_trip(mux_max):
    """Slowest round trip of the spread corners: slow output pad + multiplexer + multiplexer + slow input pad."""
    return sc.OUT_PAD[1] + sc.IN_PAD[1] + 2 * mux_max


def report(log, test, lines):
    """The summary block to the log and to test/output/<test>.txt (kept as a CI artifact)."""
    log.info("SUMMARY %s\n  %s" % (test, "\n  ".join(lines)))
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "output")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, test + ".txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def fmt(win):
    return " ".join("%d%s" % (d, "f" if f else "r") for d, f in win) or "none"


async def spi_window(dut, u, txper, mode, table, first_only=False, order=None):
    """Read-back settings that read the JEDEC ID with 0 device timing errors at every corner of table."""
    sc.CORNERS = table
    win = []
    for delay, fall in order or SETTINGS:
        good = True
        for c in ("slow", "fast", "clock slow, data fast", "clock fast, data slow"):
            if not (await sc.id_ok(dut, u, mode, txper, delay, fall, c))[0]:
                good = False
                break
        if good:
            win.append((delay, fall))
            if first_only:
                break
    return win


async def jtag_window(dut, u, txper, table, first_only=False, order=None):
    """Read-back settings with which the whole T2 session (IDCODEs, BYPASS, 1,000-bit scan) is correct at every
    corner of table."""
    sc.CORNERS = table
    win = []
    for delay, fall in order or SETTINGS:
        good = True
        for c in ("slow", "fast", "clock slow, data fast", "clock fast, data slow"):
            f, _ = await sc.jtag_session(dut, u, txper, delay, fall, c)
            if f:
                good = False
                break
        if good:
            win.append((delay, fall))
            if first_only:
                break
    return win


async def first_fail(log, name, window, step, m_stop):
    """Spread sweep: window(m, first_only, order) -> settings. Returns (last passing M, first failing M, window at
    the last coarse pass); first failing M is None when every step up to m_stop passes."""
    m, last, last_win = 0.0, None, []
    while m <= m_stop:
        win = await window(m, False, None)
        log.info("%s: multiplexer 0 to %.2f ns each way (slowest round trip %.2f ns): %d settings: %s" % (
            name, m, round_trip(m), len(win), fmt(win)))
        if not win:
            break
        last, last_win = m, win
        m += step
    else:
        return last, None, last_win
    if last is None:
        return None, 0.0, []
    lo, hi = last, m
    order = sorted(last_win, key=lambda s: -s[0])
    while hi - lo > STEP:
        mid = round((lo + hi) / 2 / STEP) * STEP
        ok = bool(await window(mid, True, order))
        log.info("%s: bisection %.2f ns: %s" % (name, mid, "a setting works" if ok else "no setting works"))
        lo, hi = (mid, hi) if ok else (lo, mid)
    for extra in (hi + 1.0, hi + 2.0):                          # the failure stays failed above the edge
        win = await window(extra, False, None)
        log.info("%s: check above the edge, %.2f ns: %d settings" % (name, extra, len(win)))
        assert not win, "%s: settings %s work again at %.2f ns above the first failure %.2f ns" % (
            name, fmt(win), extra, hi)
    return lo, hi, last_win


def summary_line(name, lo, hi, step_win):
    if hi is None:
        return "%s: no failure up to %.2f ns each way (round trip %.2f ns)" % (name, lo, round_trip(lo))
    return ("%s: last pass %.2f ns, first failure %.2f ns each way (slowest round trip %.2f / %.2f ns, spread "
            "against the fast corner %.2f ns)" % (name, lo, hi, round_trip(lo), round_trip(hi),
                                                   round_trip(hi) - sc.OUT_PAD[0] - sc.IN_PAD[0]))


@cocotb.test(skip=GL)
async def test_mux_spi_30mhz(dut):
    """30 MHz SPI controller (TXPER 1, mode 0) against the flash model: control sweep, spread sweep to the first
    failure, known-delay sweep with the setting chosen per delay."""
    u = await sc.unit_reset(dut)
    sc.dump_on(dut, False)
    log = dut._log
    saved = sc.CORNERS
    out = []
    try:
        # control: sample delay 0, rising-edge copy, slow pads + multiplexer m
        ctl = []
        for k in range(21):
            m = 0.5 * k
            sc.CORNERS = corners(m)
            ok, got, _ = await sc.id_ok(dut, u, 0, 1, 0, 0, "slow")
            ctl.append((m, ok))
        fails = [m for m, ok in ctl if not ok]
        first = next((m for m, ok in ctl if all(not o for mm, o in ctl if mm >= m)), None)
        out.append("control (30 MHz, sample delay 0, slow pads + m): ID read wrong at m = %s ns; wrong at every m "
                   "from %s ns" % (" ".join("%.1f" % m for m in fails) or "none", first))
        assert not ctl[-1][1], "control: the ID was read correctly at 10 ns"

        async def win30(m, first_only, order):
            return await spi_window(dut, u, 1, 0, corners(m), first_only, order)
        lo, hi, w = await first_fail(log, "SPI 30 MHz mode 0", win30, 1.0, 30.0)
        assert lo is not None and lo >= 10.0, "SPI 30 MHz: the 10 ns window of test_slu_clk.py no longer holds"
        out.append(summary_line("SPI 30 MHz mode 0 (spread 0 to M)", lo, hi, w))

        # review frames: one ID read at the slow corner at the last pass and at the first failure
        if hi is not None:
            wl = await spi_window(dut, u, 1, 0, corners(lo))
            assert wl, "SPI 30 MHz: no setting at the last passing delay %.2f ns" % lo
            d, f = wl[len(wl) // 2]
            for m in (lo, hi):
                sc.CORNERS = corners(m)
                sc.dump_on(dut, True)
                t0 = sc.now_ns()
                ok, got, errs = await sc.id_ok(dut, u, 0, 1, d, f, "slow")
                sc.dump_on(dut, False)
                out.append("waveform: ID read, slow corner, multiplexer %.2f ns, setting %d%s, %.1f to %.1f ns: %s%s" % (
                    m, d, "f" if f else "r", t0, sc.now_ns(), " ".join("%02X" % b for b in got),
                    "" if ok else " (wrong or timing error: %s)" % (errs[:1] or "data")))

        # known delay: the same m on every pin, pad corners and pad skews only
        for m in (10.0, 20.0, 30.0, 45.0, 60.0):
            win = await spi_window(dut, u, 1, 0, corners(m, m))
            out.append("SPI 30 MHz mode 0, known multiplexer %.0f ns on every pin (round trip %.2f to %.2f ns): "
                       "%d settings: %s" % (m, sc.OUT_PAD[0] + sc.IN_PAD[0] + 2 * m, round_trip(m), len(win), fmt(win)))
    finally:
        sc.CORNERS = saved
    report(log, "test_mux_spi_30mhz", out)


@cocotb.test(skip=GL)
async def test_mux_spi_15mhz(dut):
    """15 MHz SPI controller (TXPER 2) against the flash model, modes 0 and 3: spread sweep to the first failure."""
    u = await sc.unit_reset(dut)
    sc.dump_on(dut, False)
    log = dut._log
    saved = sc.CORNERS
    out = []
    try:
        for mode in (0, 3):
            async def win15(m, first_only, order, mode=mode):
                return await spi_window(dut, u, 2, mode, corners(m), first_only, order)
            lo, hi, w = await first_fail(log, "SPI 15 MHz mode %d" % mode, win15, 2.0, 60.0)
            assert lo is not None and lo >= 10.0, "SPI 15 MHz mode %d: fails inside the 10 ns range" % mode
            out.append(summary_line("SPI 15 MHz mode %d (spread 0 to M)" % mode, lo, hi, w))
    finally:
        sc.CORNERS = saved
    report(log, "test_mux_spi_15mhz", out)


@cocotb.test(skip=GL)
async def test_mux_jtag(dut):
    """JTAG shift at 30 MHz (TXPER 1) and 15 MHz (TXPER 2) TCK, navigation at TXPER 4, TDO 7 ns after the falling
    edge: spread sweep to the first failure."""
    u = await sc.unit_reset(dut)
    sc.dump_on(dut, False)
    log = dut._log
    saved = sc.CORNERS
    out = []
    try:
        for txper, step, stop in ((1, 1.0, 30.0), (2, 2.0, 60.0)):
            async def winj(m, first_only, order, txper=txper):
                return await jtag_window(dut, u, txper, corners(m), first_only, order)
            name = "JTAG %d MHz TCK" % (30 // txper)
            lo, hi, w = await first_fail(log, name, winj, step, stop)
            assert lo is not None and lo >= 10.0, "%s: fails inside the 10 ns range" % name
            out.append(summary_line(name + " (spread 0 to M)", lo, hi, w))
    finally:
        sc.CORNERS = saved
    report(log, "test_mux_jtag", out)
