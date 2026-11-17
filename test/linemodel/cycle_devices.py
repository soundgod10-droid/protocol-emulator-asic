"""Per-cycle device adapters for the line models (no simulator, no cocotb).

A bus model of the model benches (test/test_fw_full.py Bench) is a callable

    dev(cycle, uo_out, uio_out, uio_oe) -> (ui_in, uio_in)

called once per clock cycle before PeModel.step(). The adapters here give the event-time line models of
this package that shape at a fixed clock period:

  UsbHostPort   runs a usb_host.UsbHostSession generator (enumeration, transactions). The host drives
                RCV, VP, VM (ui_in bits); the chip drives VPO, VMO, OE# (uio bits), as through a
                low-speed transceiver. The bus is the host level while the host drives, the device level
                while OE# is low, else J.
  TenBaseTPort  drives a tenbaset partner wave (+1, -1, 0) as the receive slicer and squelch inputs and
                captures the chip's TD+, TD-, OE as a level wave for tenbaset.decode_tx().

Input changes happen at the clock edge after their event time, so the line edges carry up to one clock
period of quantization (16.7 ns at 60 MHz): inside the USB receiver limits (TDJR1 75 ns) and inside the
10BASE-T receiver limit (13.5 ns) only for the mean (a both-edge receiver sees at most half a period less
resolution than with exact edges). The cocotb tests drive exact edges; these adapters are for model
benches and the visual simulator.
"""
from .usb_host import Drive, Wait, Listen, Result
from .wave import Wave

USB_PINS = {"rcv": 0, "vp": 1, "vm": 2, "vpo": 0, "vmo": 1, "oen": 2}
ETH_PINS = {"slicer": 0, "squelch": 1, "tdp": 0, "tdm": 1, "oe": 2}
LEVEL_PINS = {"J": (0, 1), "K": (1, 0), "SE0": (0, 0), "SE1": (1, 1)}     # (VP, VM)


class _Cursor:
    """Level of a wave at increasing times, without a scan from the start."""

    def __init__(self, wave):
        self.w = wave
        self.i = 0
        self.level = wave.initial

    def at(self, t):
        e = self.w.edges
        while self.i < len(e) and e[self.i][0] <= t:
            self.level = e[self.i][1]
            self.i += 1
        return self.level


class UsbHostPort:
    """USB low-speed host session on the chip pins, one call per clock cycle."""

    def __init__(self, session_gen, clk_ps=16666, pins=None):
        self.p = dict(USB_PINS, **(pins or {}))
        self.clk_ps = clk_ps
        self.gen = session_gen
        self.result = None
        self.report = None
        self.done = False
        self.act = None
        self.t0 = None
        self.cur = None
        self.k0 = 0
        self.host = "IDLE"
        self.dev = "IDLE"
        self.rcv = 0
        self.dev_edges = []           # device level changes (ps, level)
        self.host_eops = []           # host SE0 -> J times (ps)
        self.collisions = []
        self._next()

    def _next(self):
        try:
            self.act = self.gen.send(self.result)
        except StopIteration as stop:
            self.act, self.done, self.report = None, True, stop.value
        self.t0 = None

    def _dev_level(self, uio_out, uio_oe):
        p = self.p
        if not (uio_oe >> p["oen"]) & 1 or (uio_out >> p["oen"]) & 1:
            return "IDLE"
        return {(1, 0): "K", (0, 1): "J", (0, 0): "SE0", (1, 1): "SE1"}[
            ((uio_out >> p["vpo"]) & 1, (uio_out >> p["vmo"]) & 1)]

    def _set_host(self, lvl, t):
        if self.host == "SE0" and lvl in ("J", "IDLE"):
            self.host_eops.append(t)
        self.host = lvl

    def _listen_done(self, a, t):
        ed = self.dev_edges[self.k0:]
        sop = next((te for te, lv in ed if lv == "K"), None)
        if sop is None:
            limit = self.t0 + a.timeout_ps
        else:
            limit, prev = self.t0 + a.cap_ps, None
            for te, lv in ed:
                if te > sop and prev == "SE0" and lv in ("J", "IDLE"):
                    limit = min(limit, te + a.post_eop_ps)
                    break
                prev = lv
        if t < limit:
            return None
        init = self.dev_edges[self.k0 - 1][1] if self.k0 else "IDLE"
        return Wave(init, [(te - self.t0, lv) for te, lv in ed if te <= limit], end=limit - self.t0)

    def __call__(self, cycle, uo_out, uio_out, uio_oe):
        t = cycle * self.clk_ps
        d = self._dev_level(uio_out, uio_oe)
        if d != self.dev:
            self.dev = d
            self.dev_edges.append((t, d))
        while self.act is not None:
            a = self.act
            if self.t0 is None:
                self.t0 = t
                self.k0 = len(self.dev_edges)
                if isinstance(a, Drive):
                    self.cur = _Cursor(a.wave)
                else:
                    self._set_host("IDLE", t)
            rel = t - self.t0
            if isinstance(a, Drive):
                if rel >= a.wave.end:
                    self.result = Result(self.t0)
                    self._next()
                    continue
                self._set_host(self.cur.at(rel), t)
                break
            if isinstance(a, Wait):
                if rel >= a.duration_ps:
                    self.result = Result(self.t0)
                    self._next()
                    continue
                break
            if isinstance(a, Listen):
                w = self._listen_done(a, t)
                if w is None:
                    break
                self.result = Result(self.t0, w)
                self._next()
                continue
            raise TypeError("unknown host action %r" % (a,))
        if self.act is None:
            self._set_host("IDLE", t)
        if self.host != "IDLE" and self.dev != "IDLE":
            self.collisions.append((t, self.host, self.dev))
        lv = self.host if self.host != "IDLE" else ("J" if self.dev == "IDLE" else self.dev)
        vp, vm = LEVEL_PINS[lv]
        if lv in ("J", "K"):
            self.rcv = vp
        p = self.p
        return (self.rcv << p["rcv"]) | (vp << p["vp"]) | (vm << p["vm"]), 0


class TenBaseTPort:
    """10BASE-T link partner on the chip pins, one call per clock cycle.

    send(wave, at_cycle) queues a partner wave (levels +1, -1, 0; tenbaset.LinkPartnerTx().wave or
    manchester_wave / nlp_wave) to start at a cycle. tx_wave() returns the chip's line levels."""

    def __init__(self, clk_ps=16666, pins=None):
        self.p = dict(ETH_PINS, **(pins or {}))
        self.clk_ps = clk_ps
        self.queue = []               # (start ps, wave)
        self.cur = None
        self.cur_t0 = None
        self.rx = 0
        self.tx_level = 0
        self.tx_edges = []            # chip line level changes (ps, level)

    def send(self, wave, at_cycle):
        self.queue.append((at_cycle * self.clk_ps, wave))
        self.queue.sort(key=lambda q: q[0])

    def _tx(self, uio_out, uio_oe):
        p = self.p
        if not (uio_oe >> p["oe"]) & 1 or not (uio_out >> p["oe"]) & 1:
            return 0
        tdp, tdm = (uio_out >> p["tdp"]) & 1, (uio_out >> p["tdm"]) & 1
        return 1 if (tdp and not tdm) else (-1 if (tdm and not tdp) else 0)

    def tx_wave(self, end_cycle=None):
        end = (end_cycle * self.clk_ps) if end_cycle is not None else (self.tx_edges[-1][0] if self.tx_edges else 0)
        return Wave(0, [(t, lv) for t, lv in self.tx_edges if t < end], end=end)

    def __call__(self, cycle, uo_out, uio_out, uio_oe):
        t = cycle * self.clk_ps
        lv = self._tx(uio_out, uio_oe)
        if lv != self.tx_level:
            self.tx_level = lv
            self.tx_edges.append((t, lv))
        if self.cur is None and self.queue and self.queue[0][0] <= t:
            self.cur_t0, w = self.queue.pop(0)
            self.cur = (w, _Cursor(w))
        if self.cur is not None:
            w, c = self.cur
            rel = t - self.cur_t0
            if rel >= w.end:
                self.cur, self.rx = None, 0
            else:
                self.rx = c.at(rel)
        p = self.p
        return ((1 if self.rx > 0 else 0) << p["slicer"]) | ((1 if self.rx != 0 else 0) << p["squelch"]), 0
