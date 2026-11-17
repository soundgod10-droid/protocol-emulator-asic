"""Line waveforms as edge lists, frequency offset and bounded jitter.

A Wave is an initial level plus a sorted list of (time_ps, level) edges.
Every edge is a change of level. Times are integer picoseconds, relative
to the start of the wave. A cocotb test can drive pins at these exact times
(Timer(t, "ps")) and can turn pin edges captured from the DUT back into a
Wave with from_pin_edges().

Levels are protocol symbols (USB: "J", "K", "SE0", "SE1", "IDLE";
10BASE-T: +1, 0, -1). Pin mapping is done by to_pin_edges() and
from_pin_edges() with a table that the test bench supplies.
"""
import random


class Wave:
    __slots__ = ("initial", "edges", "end", "marks")

    def __init__(self, initial, edges=None, end=None, marks=None):
        self.initial = initial
        self.edges = []
        last_t, last_l = None, initial
        for t, lvl in (edges or []):
            t = int(round(t))
            if last_t is not None and t < last_t:
                raise ValueError("edges not sorted: %d after %d" % (t, last_t))
            if lvl == last_l:
                continue
            if last_t is not None and t == last_t:
                # two changes at the same time: keep the later one
                self.edges.pop()
                last_l = self.edges[-1][1] if self.edges else initial
                if lvl == last_l:
                    continue
            self.edges.append((t, lvl))
            last_t, last_l = t, lvl
        if end is None:
            end = self.edges[-1][0] if self.edges else 0
        self.end = int(round(end))
        self.marks = dict(marks or {})

    # ---- construction -------------------------------------------------
    @classmethod
    def from_segments(cls, segments, initial=None, t0=0.0, marks=None):
        """segments: list of (level, duration_ps). Durations may be float.

        Edge times are accumulated in float and rounded once, so rounding
        error does not grow along the wave (error <= 0.5 ps per edge).
        """
        t = float(t0)
        edges = []
        if initial is None:
            initial = segments[0][0] if segments else None
        for lvl, dur in segments:
            edges.append((t, lvl))
            t += dur
        return cls(initial, edges, end=t, marks=marks)

    # ---- queries ------------------------------------------------------
    @property
    def final(self):
        return self.edges[-1][1] if self.edges else self.initial

    def level_at(self, t):
        lvl = self.initial
        for te, le in self.edges:
            if te > t:
                break
            lvl = le
        return lvl

    def runs(self, t_end=None):
        """List of (t_start, t_stop, level) for each constant run.

        The run before the first edge is not listed. The last run ends at
        t_end (default: self.end).
        """
        t_end = self.end if t_end is None else t_end
        out = []
        for i, (t, lvl) in enumerate(self.edges):
            t2 = self.edges[i + 1][0] if i + 1 < len(self.edges) else max(t_end, t)
            out.append((t, t2, lvl))
        return out

    def window(self, t0, t1):
        """New wave with the edges in [t0, t1), times kept absolute."""
        init = self.level_at(t0 - 1) if self.edges else self.initial
        ed = [(t, l) for t, l in self.edges if t0 <= t < t1]
        return Wave(init, ed, end=t1, marks=self.marks)

    # ---- transforms ---------------------------------------------------
    def shifted(self, dt):
        return Wave(self.initial, [(t + dt, l) for t, l in self.edges],
                    end=self.end + dt,
                    marks={k: v + dt for k, v in self.marks.items()})

    def concat(self, other, gap_ps=0):
        """Append other after self.end + gap_ps (other's times are relative)."""
        off = self.end + gap_ps
        ed = list(self.edges) + [(t + off, l) for t, l in other.edges]
        if other.initial != self.final:
            ed.insert(len(self.edges), (off, other.initial))
        marks = dict(self.marks)
        for k, v in other.marks.items():
            marks[k] = v + off
        return Wave(self.initial, ed, end=other.end + off, marks=marks)

    def with_ppm(self, ppm, t_ref=0):
        """Frequency offset: a source that is ppm fast has all intervals
        divided by (1 + ppm * 1e-6). Positive ppm = higher bit rate."""
        k = 1.0 / (1.0 + ppm * 1e-6)
        f = lambda t: t_ref + (t - t_ref) * k
        return Wave(self.initial, [(f(t), l) for t, l in self.edges],
                    end=f(self.end), marks={n: f(v) for n, v in self.marks.items()})

    def with_jitter(self, peak_ps, seed, mode="uniform", select=None,
                    min_gap_ps=1):
        """Bounded jitter. Each selected edge moves by an independent offset
        in [-peak_ps, +peak_ps]. mode "uniform" draws uniformly; mode
        "extreme" draws only -peak_ps or +peak_ps (worst case).
        select(i, t, old_level, new_level) -> bool chooses edges (default all).
        Edges keep their order: an offset that would cross the neighbour
        edge is clipped so that the gap is at least min_gap_ps.
        """
        rng = random.Random(seed)
        out = []
        prev_l = self.initial
        for i, (t, l) in enumerate(self.edges):
            d = 0.0
            if select is None or select(i, t, prev_l, l):
                if mode == "extreme":
                    d = peak_ps if rng.random() < 0.5 else -peak_ps
                else:
                    d = rng.uniform(-peak_ps, peak_ps)
            out.append([t + d, l])
            prev_l = l
        for i in range(1, len(out)):
            if out[i][0] < out[i - 1][0] + min_gap_ps:
                out[i][0] = out[i - 1][0] + min_gap_ps
        return Wave(self.initial, [(t, l) for t, l in out], end=self.end,
                    marks=self.marks)

    def with_edge_offsets(self, fn):
        """General deterministic edge shift: fn(i, t, old, new) -> dt_ps."""
        out, prev = [], self.initial
        for i, (t, l) in enumerate(self.edges):
            out.append((t + fn(i, t, prev, l), l))
            prev = l
        out.sort(key=lambda e: e[0])
        return Wave(self.initial, out, end=self.end, marks=self.marks)

    # ---- pins ---------------------------------------------------------
    def to_pin_edges(self, mapping):
        """mapping: level -> tuple of pin values (one entry per pin).
        Returns (initial_tuple, [(t, pin_index, value), ...]) sorted by time,
        ready for a driver loop."""
        cur = mapping[self.initial]
        out = []
        for t, l in self.edges:
            new = mapping[l]
            for k, (a, b) in enumerate(zip(cur, new)):
                if a != b:
                    out.append((t, k, b))
            cur = new
        return mapping[self.initial], out

    @classmethod
    def from_pin_edges(cls, initial_pins, pin_edges, inverse, end=None,
                       merge_ps=0):
        """Rebuild a level wave from captured pin edges.

        initial_pins: tuple of pin values at t = 0.
        pin_edges: [(t, pin_index, value)], any order.
        inverse: pin tuple -> level.
        merge_ps: a level that lasts less than merge_ps between two others
        is removed (skew between pins of one differential pair). Default 0:
        every level is kept, which is the strict choice for a DUT output.
        """
        cur = list(initial_pins)
        ed = []
        for t, k, v in sorted(pin_edges, key=lambda e: e[0]):
            cur[k] = v
            ed.append((t, inverse[tuple(cur)]))
        w = cls(inverse[tuple(initial_pins)], ed, end=end)
        if merge_ps > 0 and len(w.edges) > 1:
            kept = []
            for i, (t, l) in enumerate(w.edges):
                nxt = w.edges[i + 1][0] if i + 1 < len(w.edges) else None
                if nxt is not None and nxt - t < merge_ps and kept:
                    continue
                kept.append((t, l))
            w = cls(w.initial, kept, end=w.end)
        return w


def ppm_of(measured_period, nominal_period):
    """Frequency offset in ppm of a measured period (positive = fast)."""
    return (nominal_period / measured_period - 1.0) * 1e6


def fit_line(xs, ys):
    """Least-squares y = a + b*x. Returns (a, b)."""
    n = len(xs)
    if n < 2:
        raise ValueError("need 2 points")
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    b = sxy / sxx
    return my - b * mx, b


def minimax_fit(xs, ys, slope_guess, rel_span=1e-3, iters=80):
    """Line y = a + b*x that minimises the largest |y - a - b*x|
    (Chebyshev fit). The spread max(y-bx) - min(y-bx) is convex in b, so a
    ternary search over b in slope_guess*(1 +/- rel_span) finds it.
    Returns (a, b, max_abs_dev). If every point lies within J of some
    line with slope in the range, max_abs_dev <= J."""
    def spread(b):
        r = [y - b * x for x, y in zip(xs, ys)]
        return max(r), min(r)
    lo, hi = slope_guess * (1 - rel_span), slope_guess * (1 + rel_span)
    for _ in range(iters):
        m1 = lo + (hi - lo) / 3
        m2 = hi - (hi - lo) / 3
        s1 = spread(m1)
        s2 = spread(m2)
        if s1[0] - s1[1] <= s2[0] - s2[1]:
            hi = m2
        else:
            lo = m1
    b = (lo + hi) / 2
    mx, mn = spread(b)
    return (mx + mn) / 2, b, (mx - mn) / 2
