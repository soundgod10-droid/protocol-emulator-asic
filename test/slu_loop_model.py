"""Bit-exact model of the serial line unit's receive phase loop (src/pe_slu_loop.v).

One step per sample, integers only, in the order of the RTL (pe_slu_loop_step):
  1. theta += 2^12 (one sample)
  2. on an edge before the initial sync: count it; at init_edge edges, theta = MID + 2^11
  3. on an edge after the initial sync: phase error eps against the expected edge (bit middle for
     Manchester, bit boundary for NRZ), inside the window: theta -= eps / 2^kp (rounded half up),
     integrator I -= eps / 2^ki_sh in the last gear (clipped to +/-2047), accepted edges + 1
  4. theta >= N * 2^12: wrap, theta += I >> 4
  5. first sample of the bit period with theta >= the decision point: the bit is the sample.

This is the loop of the clock-recovery study that chose the 10BASE-T and USB configurations
(second-order loop with gear shift for Manchester, hard resync for USB). test_slu.py runs it on
the same sample stream as the RTL and compares every cycle.
"""

ONE = 1 << 12


class LoopConfig:
    def __init__(self, n, manch, kp=(0, 0, 0), gn=(0, 0), ki_en=False, ki_sh=0,
                 win=None, wina=None, acq_n=0, init_edge=2, ddr=False):
        self.n, self.manch, self.kp, self.gn = n, manch, kp, gn
        self.ki_en, self.ki_sh = ki_en, ki_sh
        if win is None:
            win = n // 4 if manch else n // 2
        self.win = win
        self.wina = win if wina is None else wina
        self.acq_n, self.init_edge, self.ddr = acq_n, init_edge, ddr


# 10BASE-T: both clock edges, 12 samples per bit, kp 2 / 4 / 6 after 8 and 24 edges,
# integrator 2 kp + 2 = 14 (shift 14 - 4 = 10) in the last gear, window 3 samples, 5 during
# the first 24 edges.
TENBASET = LoopConfig(12, True, kp=(2, 4, 6), gn=(8, 24), ki_en=True, ki_sh=10,
                      win=3, wina=5, acq_n=24, ddr=True)
# USB low speed: one sample per clock, 40 samples per bit, hard resync, window 20 samples.
USB_LS = LoopConfig(40, False)


class Loop:
    def __init__(self, cfg):
        self.c = cfg
        self.th = 0
        self.ii = 0
        self.nacc = 0
        self.nedge = 0
        self.init = False
        self.decided = False
        self.xprev = 0

    def step_pair(self, x0, x1):
        """Both clock edges: the two samples of one clock cycle (x0 first). At most one edge is used
        in a cycle: after an edge between the last sample and x0, an edge between x0 and x1 (a pulse
        of half a clock cycle, 8.3 ns at 60 MHz, shorter than any 10BASE-T pulse) is not used.
        Returns the decided bit of the cycle or None."""
        e0 = x0 != self.xprev
        r0 = self.step(x0)
        r1 = self.step(x1, allow=not e0)
        return r1 if r0 is None else r0

    def step(self, x, allow=True):
        """One sample; returns the decided bit or None. allow = False: an edge here is not used."""
        c = self.c
        none = c.n * ONE
        half = none // 2
        mid = half if c.manch else 0
        tdec = (3 * c.n * 1024 - 2048) if c.manch else (half - 2048)
        th = self.th + ONE
        edge = allow and x != self.xprev
        self.xprev = x
        if edge and not self.init:
            self.nedge = min(self.nedge + 1, 3)
            if self.nedge >= c.init_edge:
                th = mid + 2048
                self.init = True
                self.decided = False
                self.nacc = 0
        elif edge and self.init:
            a = th - 2048 - mid + half
            assert -none <= a < 2 * none, a
            a %= none
            eps = a - half
            w = (c.wina if (c.acq_n and self.nacc < c.acq_n) else c.win) * ONE
            if abs(eps) <= w:
                if self.nacc < c.gn[0]:
                    kp, hk = c.kp[0], False
                elif self.nacc < c.gn[1]:
                    kp, hk = c.kp[1], False
                else:
                    kp, hk = c.kp[2], c.ki_en
                corr = eps if kp == 0 else (eps + (1 << (kp - 1))) >> kp
                th -= corr
                if hk:
                    sh = c.ki_sh
                    d = eps if sh == 0 else (eps + (1 << (sh - 1))) >> sh
                    self.ii = max(-2047, min(2047, self.ii - d))
                self.nacc = min(self.nacc + 1, 63)
        if th >= none:
            th = th - none + (self.ii >> 4)
            self.decided = False
        out = None
        if self.init and not self.decided and th >= tdec:
            out = x
            self.decided = True
        assert -(1 << 19) <= th < (1 << 19), th
        self.th = th
        return out
