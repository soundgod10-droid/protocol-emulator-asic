"""10BASE-T link-partner model: frames, Manchester line code, TP_IDL, normal
link pulses, the Link Integrity Test state machine, a decoder and checker
for the DUT transmitter, and expected results for the DUT receiver.

References: IEEE Std 802.3-2022, subclause and printed page (PDF page =
printed page + 1). Only section and page are cited.

Line levels: +1 = TD+ positive w.r.t. TD- ("HI"), -1 = negative ("LO"),
0 = silence (0 mV +/- 50 mV, 14.2.1.1 p.509). A positive DO signal gives a
positive TD+ (14.2.1.1 p.509); IDL and TP_IDL start HI (7.3.1.1 p.321;
14.3.1.2.1 p.523).

Values from raster figures (Figure 14-6 p.518, Figure 14-11 p.523,
Figure 14-13 p.524) are read off the figure images; printed numbers are
exact, the shape between printed points is read by eye.
"""
from bisect import bisect_left
from dataclasses import dataclass, field
import itertools

from .crc import eth_fcs_octets, eth_fcs_ok, bytes_to_bits_lsb_first, bits_to_bytes_lsb_first
from .wave import Wave, fit_line, minimax_fit

BT_PS = 100_000.0                 # 10 Mb/s, 7.3.2 p.324
LIM = {
    "RATE_PPM": 100.0,            # +/-0.01 %, 7.3.2 p.324
    "PREAMBLE_BITS": 56,          # DTE sends >= 56 bits, 7.2.3.2 p.316; 7 octets 4.2.5 p.261
    "TX_BIT_LOSS": 2,             # MAU may drop <= 2 bits at TX start, 14.2.1.1 p.509
    "RX_BIT_LOSS": 5,             # MAU may drop <= 5 bits at RX start, 14.2.1.2 p.509
    "MIN_FRAME": 64,              # minFrameSize 512 bits, Table 4-2 p.282
    "MAX_BASIC_FRAME": 1518,      # maxBasicFrameSize, Table 4-2 p.282
    "IPG_BT": 96,                 # interPacketGap 96 bits, Table 4-2 p.282
    "IPG_RX_MIN_BT": 47,          # gap may shrink to 47 BT at the DTE, Table 4-2 NOTE 1 p.282
    "TX_JITTER_DIRECT_PS": 8_000, # +/-8 ns into 100 ohm, 14.3.1.2.3 p.525
    "TX_JITTER_MODEL_PS": 3_500,  # +/-3.5 ns through the TP model, 14.3.1.2.3 p.525
    # Test method of B.4.3.3 p.6287 (informative): zero crossings 8.0 BT and
    # 8.5 BT after a triggering zero crossing within +/-11 ns for an
    # integrated MAU (+/-7 ns for an external MAU); TP_IDL and the first bit
    # left out.
    "TX_JITTER_B433_PS": 11_000,
    "RX_JITTER_PS": 13_500,       # receiver accepts +/-13.5 ns, 14.3.1.3.1 p.527
    "IDL_MIN_BT": 2.0,            # IDL held >= 2 BT, 7.3.1.1 p.321
    "IDLE_DETECT_BT": 2.3,        # idle detected within 2.3 BT, 14.3.1.3.3 p.528
    # Start of TP_IDL, Figure 14-11 p.523: after the last positive-going
    # transition (t = 0) the voltage stays above 585 mV*sin(...) up to
    # 2.5 BT, under 3.1 V up to 4.5 BT then under a line to +50 mV at
    # 6.0 BT; lower bound -3.1 V from 2.5 to 4.5 BT then a line to -50 mV at
    # 45 BT.
    "TPIDL_HI_MIN_BT": 2.5,
    "TPIDL_UP_KNEE_BT": 4.5, "TPIDL_UP_END_BT": 6.0,
    "TPIDL_LO_KNEE_BT": 4.5, "TPIDL_LO_END_BT": 45.0,
    # Link test pulse, Figure 14-13 p.524: with the template origin at the
    # start of the pulse, positive required up to 0.85 BT (inner limit from
    # 0.25 BT to 0.85 BT); upper bound 3.1 V to 1.3 BT then a line to 0.3 V
    # at 2.0 BT; lower bound -3.1 V from 0.85 to 2.0 BT then a line to
    # -50 mV at 4.0 BT.
    "NLP_FALL_MIN_BT": 0.85,
    "NLP_UP_KNEE_BT": 1.3, "NLP_UP_END_BT": 2.0, "NLP_UP_END_V": 0.3,
    "NLP_LO_KNEE_BT": 2.0, "NLP_LO_END_BT": 4.0,
    "NLP_TYP_WIDTH_PS": 100_000,  # T1 typical 100 ns, Table 28-1 p.938
    # Silence between link test pulses 16 ms +/- 8 ms, 14.2.1.1 p.509
    "NLP_SILENCE_MIN_PS": 8e9, "NLP_SILENCE_MAX_PS": 24e9,
    # Link Integrity Test timers, 14.2.1.7 p.511
    "LINK_LOSS_PS": (50e9, 150e9), "LC_MAX": (2, 10),
    "LINK_TEST_MAX_PS": (25e9, 150e9), "LINK_TEST_MIN_PS": (2e9, 7e9),
    # TX amplitude 2.2..2.8 V peak into 100 ohm, 14.3.1.2.1 p.521
    "AMPL_V": (2.2, 2.8),
}
SFD = [1, 0, 1, 0, 1, 0, 1, 1]    # 3.2.2 p.240; 4.2.6 p.261


def _line(x0, y0, x1, y1, y):
    """x where the straight template edge from (x0,y0) to (x1,y1) has value y."""
    return x0 + (x1 - x0) * (y0 - y) / (y0 - y1)


def tpidl_hi_max_bt(ampl_v):
    """Latest end of the HI after the last positive transition for a square
    driver of amplitude ampl_v (Figure 14-11 p.523 upper bound)."""
    if ampl_v >= 3.1:
        return LIM["TPIDL_UP_KNEE_BT"]
    return _line(LIM["TPIDL_UP_KNEE_BT"], 3.1, LIM["TPIDL_UP_END_BT"], 0.05, ampl_v)


def tpidl_tail_max_bt(ampl_v):
    return _line(LIM["TPIDL_LO_KNEE_BT"], 3.1, LIM["TPIDL_LO_END_BT"], 0.05, ampl_v)


def nlp_width_window_ps(ampl_v):
    """(min, max) width of a square link pulse of amplitude ampl_v that fits
    Figure 14-13 p.524 with the template origin at the start of the pulse:
    fall in [0.85 BT, f_max], f_max where the upper bound drops below ampl_v
    (1.375 BT at 2.8 V)."""
    wmin = LIM["NLP_FALL_MIN_BT"] * BT_PS
    fmax = _line(LIM["NLP_UP_KNEE_BT"], 3.1, LIM["NLP_UP_END_BT"], LIM["NLP_UP_END_V"], ampl_v)
    return wmin, fmax * BT_PS


def nlp_tail_max_ps(ampl_v):
    return _line(LIM["NLP_LO_KNEE_BT"], 3.1, LIM["NLP_LO_END_BT"], 0.05, ampl_v) * BT_PS


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------
def build_frame(dst, src, ethertype, payload, pad=True, add_fcs=True):
    """DA, SA, Length/Type, data, pad to 64 octets with FCS, FCS.
    3.1.1 p.239; 3.2.8 p.243; 3.2.9 p.243."""
    body = bytes(dst) + bytes(src) + int(ethertype).to_bytes(2, "big") + bytes(payload)
    if pad and len(body) < LIM["MIN_FRAME"] - 4:
        body += bytes(LIM["MIN_FRAME"] - 4 - len(body))
    return body + eth_fcs_octets(body) if add_fcs else body


def packet_bits(frame, preamble_bits=56, sfd=True, extra_bits=()):
    """Preamble 1010... (left to right, ends in 0; 4.2.5 p.261), SFD, frame
    octets LSB first (3.3 p.244; the FCS octets from eth_fcs_octets give
    x31 first). preamble_bits < 56 models a truncated preamble.
    extra_bits: dribble bits after the frame."""
    pre = [(i + 1) % 2 for i in range(56)][56 - preamble_bits:] if preamble_bits else []
    # keep the pattern ending in 0 next to the SFD
    out = list(pre)
    if sfd:
        out += SFD
    out += bytes_to_bits_lsb_first(frame)
    out += list(extra_bits)
    return out


# ---------------------------------------------------------------------------
# Manchester line code, TP_IDL, link pulses
# ---------------------------------------------------------------------------
def manchester_wave(bits, ppm=0.0, tpidl_hi_bt=3.0, tpidl_tail_bt=0.0,
                    lead_silence_ps=0.0):
    """CD0 = HI then LO, CD1 = LO then HI, transition at mid-bit
    (7.3.1.1 p.319). After the last bit: start of TP_IDL = HI that lasts
    tpidl_hi_bt from the last positive-going transition (Figure 14-11
    p.523), an optional LO tail, then silence.
    Marks: start, last_pos, idle_start (end of the last bit cell), end."""
    half = BT_PS / 2 / (1 + ppm * 1e-6)
    bt = 2 * half
    segs = []
    if lead_silence_ps > 0:
        segs.append((0, lead_silence_ps))
    for b in bits:
        first, second = (-1, +1) if b else (+1, -1)
        for lvl in (first, second):
            if segs and segs[-1][0] == lvl:
                segs[-1] = (lvl, segs[-1][1] + half)
            else:
                segs.append((lvl, half))
    t_idle = sum(d for _, d in segs)
    if bits and bits[-1] == 1:
        t_lastpos = t_idle - half              # mid-bit rising edge of a CD1
        hi = tpidl_hi_bt * bt - half
        segs[-1] = (+1, segs[-1][1] + hi)
    else:
        t_lastpos = t_idle                     # extra transition at the boundary
        segs.append((+1, tpidl_hi_bt * bt))
    if tpidl_tail_bt > 0:
        segs.append((-1, tpidl_tail_bt * bt))
    segs.append((0, 0.0))
    return Wave.from_segments(segs, initial=0, marks={
        "start": lead_silence_ps, "last_pos": t_lastpos, "idle_start": t_idle})


def nlp_wave(width_ps=None, tail_ps=0.0):
    """One normal link pulse: single positive pulse (14.3.1.2.1 p.523;
    width T1 100 ns typical, Table 28-1 p.938)."""
    width_ps = LIM["NLP_TYP_WIDTH_PS"] if width_ps is None else width_ps
    segs = [(+1, width_ps)]
    if tail_ps:
        segs.append((-1, tail_ps))
    segs.append((0, 0.0))
    return Wave.from_segments(segs, initial=0, marks={"pulse": 0.0})


def zero_crossing_jitter(wave, peak_ps, seed, mode="uniform"):
    """Jitter on +1<->-1 transitions only (the zero crossings that
    14.3.1.3.1 p.527 limits to +/-13.5 ns)."""
    return wave.with_jitter(peak_ps, seed, mode=mode,
                            select=lambda i, t, o, n: o != 0 and n != 0)


class LinkPartnerTx:
    """Builds the partner's transmit line: frames, gaps, TP_IDL and NLPs.

    Times are ps. NLPs fill idle time: after a packet the sequence starts
    with silence (14.2.1.1 p.509); silence between pulses is nlp_silence_ps
    (default 16 ms; window 8..24 ms)."""

    def __init__(self, ppm=0.0, nlp_silence_ps=16e9, nlp_width_ps=None):
        self.ppm = ppm
        self.k = 1 / (1 + ppm * 1e-6)
        self.nlp_silence_ps = nlp_silence_ps
        self.nlp_width_ps = nlp_width_ps
        self.wave = Wave(0, [], end=0)
        self.events = []          # ('pulse', t) / ('active', t0, t1)
        self.frames = []          # (t_start, frame, expect)
        self._since_activity = 0.0

    def frame(self, frame, gap_bt=96, preamble_bits=56, sfd=True,
              extra_bits=(), tpidl_hi_bt=3.0, expect=None):
        gap = gap_bt * BT_PS * self.k
        w = manchester_wave(packet_bits(frame, preamble_bits, sfd, extra_bits),
                            ppm=self.ppm, tpidl_hi_bt=tpidl_hi_bt)
        # gap counts from the end of the last bit cell (start of idle)
        idle_start_prev = self.wave.marks.get("idle_start", None)
        if idle_start_prev is not None:
            pad = max(0.0, idle_start_prev + gap - self.wave.end)
        else:
            pad = 0.0
        t0 = self.wave.end + pad
        self.wave = self.wave.concat(w, gap_ps=pad)
        self.events.append(("active", t0, t0 + w.marks["idle_start"]))
        self.frames.append((t0, bytes(frame), expect))
        return t0

    def idle(self, duration_ps, silences=None):
        """Idle with NLPs. silences: explicit list of silence lengths (ps)
        before each pulse (to place pulses outside the window)."""
        t_end = self.wave.end + duration_ps
        it = iter(silences) if silences is not None else None
        while True:
            s = next(it, None) if it is not None else self.nlp_silence_ps
            if s is None:
                break
            if self.wave.end + s >= t_end:
                break
            p = nlp_wave(self.nlp_width_ps)
            t0 = self.wave.end + s
            self.wave = self.wave.concat(p, gap_ps=s)
            self.events.append(("pulse", t0))
        self.wave = Wave(self.wave.initial, self.wave.edges, end=t_end,
                         marks=self.wave.marks)


# ---------------------------------------------------------------------------
# Link Integrity Test function, Figure 14-6 p.518, 14.2.1.7 p.511,
# timers 14.2.3.2 p.519. RD and link test pulses are the partner's view of
# the other end; DO (own transmit) is taken as idle.
# ---------------------------------------------------------------------------
PASS, FAIL_RESET, FAIL, FAIL_COUNT, FAIL_EXTEND = (
    "LINK TEST PASS", "LINK TEST FAIL RESET", "LINK TEST FAIL",
    "LINK TEST FAIL COUNT", "LINK TEST FAIL EXTEND")


class LinkIntegrity:
    def __init__(self, link_loss_ps, lc_max, link_test_max_ps, link_test_min_ps,
                 start=FAIL_RESET):
        self.p = (link_loss_ps, lc_max, link_test_max_ps, link_test_min_ps)
        self.link_loss, self.lc_max, self.lt_max, self.lt_min = self.p
        self.start = start

    def run(self, events, t_end):
        """events: ('pulse', t) and ('active', t0, t1). Returns the state
        trace [(t, state)]; FAIL_RESET/COUNT/EXTEND appear only when they
        last (or for one instant)."""
        tr = []
        st = {"s": None, "count": 0, "t_loss": 0.0, "t_min": 0.0, "t_max": 0.0}

        def enter(s, t):
            st["s"] = s
            tr.append((t, s))
            if s == PASS:
                st["t_loss"] = st["t_min"] = t
            elif s == FAIL_RESET:
                st["count"] = 0
            elif s == FAIL:
                st["t_min"] = st["t_max"] = t
            elif s == FAIL_COUNT:
                st["count"] += 1

        def settle(t):
            """Transitions taken at once while RD is idle and no pulse."""
            while True:
                s = st["s"]
                if s == FAIL_RESET or s == FAIL_COUNT:
                    enter(FAIL, t)
                elif s == FAIL and st["count"] >= self.lc_max:
                    enter(FAIL_EXTEND, t)
                elif s == FAIL_EXTEND:
                    enter(PASS, t)                 # RD = idle * DO = idle
                else:
                    return

        def advance(t_next):
            while True:
                s = st["s"]
                if s == PASS:
                    te = st["t_loss"] + self.link_loss
                    if te > t_next:
                        return
                    enter(FAIL_RESET, te)
                    settle(te)
                elif s == FAIL:
                    te = st["t_max"] + self.lt_max
                    if te > t_next:
                        return
                    enter(FAIL_RESET, te)
                    settle(te)
                else:
                    return

        enter(self.start, 0.0)
        settle(0.0)
        evs = sorted(events, key=lambda e: e[1])
        for e in evs:
            t = e[1]
            if t > t_end:
                break
            advance(t)
            s = st["s"]
            if e[0] == "pulse":
                min_done = (t - st["t_min"]) >= self.lt_min
                if s == PASS:
                    if min_done:
                        enter(PASS, t)
                elif s == FAIL:
                    if min_done:
                        enter(FAIL_COUNT, t)
                    else:
                        enter(FAIL_RESET, t)
                    settle(t)
            else:
                t1 = e[2]
                if s == PASS:
                    enter(PASS, t)
                    st["t_loss"] = st["t_min"] = t1     # timers restart until RD idle
                else:
                    enter(FAIL_EXTEND, t)
                    enter(PASS, t1)                      # RD idle at t1
        advance(t_end)
        return tr

    @staticmethod
    def state_at(trace, t):
        s = trace[0][1]
        for tt, ss in trace:
            if tt > t:
                break
            s = ss
        return s


def lit_corner_params(points=2):
    """Timer sets over the allowed ranges (14.2.1.7 p.511): 2 = both ends,
    3 = ends and middle."""
    def vals(r):
        lo, hi = r
        if points == 2:
            return (lo, hi)
        return (lo, (lo + hi) / 2, hi) if isinstance(lo, float) else (lo, (lo + hi) // 2, hi)
    return list(itertools.product(vals(LIM["LINK_LOSS_PS"]), vals(LIM["LC_MAX"]),
                                  vals(LIM["LINK_TEST_MAX_PS"]), vals(LIM["LINK_TEST_MIN_PS"])))


def expected_link_state(events, t, points=3, starts=(FAIL_RESET, PASS)):
    """State a compliant MAU must be in at time t for the given RD events:
    'PASS' or 'FAIL' if every sampled compliant timer set agrees, else
    'EITHER'. The grid samples the allowed ranges; it is not a proof for
    every value inside them."""
    seen = set()
    for p in lit_corner_params(points):
        for s0 in starts:
            tr = LinkIntegrity(*p, start=s0).run(events, t)
            seen.add("PASS" if LinkIntegrity.state_at(tr, t) == PASS else "FAIL")
    return seen.pop() if len(seen) == 1 else "EITHER"


# ---------------------------------------------------------------------------
# Expected receive result for a frame the partner sends to the DUT
# ---------------------------------------------------------------------------
def expected_rx(frame, preamble_bits=56, sfd=True, extra_bits=(), jitter_ps=0.0,
                ppm=0.0):
    """Returns (verdict, reason, ref); verdict DELIVER, DISCARD or EITHER."""
    if abs(ppm) > 2 * LIM["RATE_PPM"] or jitter_ps > LIM["RX_JITTER_PS"]:
        return "EITHER", "stimulus outside the receiver limits", "7.3.2 p.324; 14.3.1.3.1 p.527"
    if not sfd:
        return "DISCARD", "no SFD: no frame start", "4.2.6 p.261; 3.2.2 p.240"
    n_extra = len(extra_bits)
    fcs_good = eth_fcs_ok(frame)
    if n_extra % 8:
        if not fcs_good:
            return "DISCARD", "dribble bits and FCS error: alignmentError", "4.2.4.2.1 p.260"
        # truncated to the octet boundary; FCS is then valid
        verdict = ("DELIVER", "dribble bits removed by truncation to the octet boundary",
                   "4.2.4.2.1 p.260")
    elif not fcs_good:
        return "DISCARD", "FCS error", "3.4 c) p.244; 4.2.4.1.2 p.260"
    else:
        verdict = ("DELIVER", "valid frame", "3.2 p.240-243")
    if len(frame) < LIM["MIN_FRAME"]:
        return "DISCARD", "shorter than minFrameSize", "4.2.4.2.2 p.261"
    if len(frame) > LIM["MAX_BASIC_FRAME"]:
        return "EITHER", "longer than maxBasicFrameSize: receiver need not enforce", "4.2.4.2.1 a) p.260"
    min_pre = LIM["PREAMBLE_BITS"] - LIM["TX_BIT_LOSS"]
    if preamble_bits < min_pre:
        return "EITHER", "preamble %d bits < %d (56 sent, <= 2 lost at a compliant TX)" % (
            preamble_bits, min_pre), "7.2.3.2 p.316; 14.2.1.1 p.509"
    return verdict


# ---------------------------------------------------------------------------
# Decoder / checker for the DUT transmitter
# ---------------------------------------------------------------------------
@dataclass
class TxEvent:
    kind: str                     # 'packet' | 'pulse' | 'other'
    t0: float
    t1: float
    bits: list = field(default_factory=list)
    frame: bytes = b""
    preamble_bits: int = 0
    extra_bits: int = 0
    violations: list = field(default_factory=list)
    measures: dict = field(default_factory=dict)
    idle_start: float = None


def _bursts(wave):
    """Maximal intervals with a non-zero level."""
    out, cur = [], None
    lvl = wave.initial
    for t, l in wave.edges:
        if lvl == 0 and l != 0:
            cur = [t, None, []]
        if cur is not None:
            cur[2].append((t, l))
        if lvl != 0 and l == 0 and cur is not None:
            cur[1] = t
            out.append(cur)
            cur = None
        lvl = l
    if cur is not None:
        cur[1] = wave.end
        out.append(cur)
    return out


def b433_jitter(crossings):
    """Largest deviation of a zero crossing 8.0 BT or 8.5 BT after a
    triggering zero crossing from its ideal position (B.4.3.3 p.6287).
    crossings: sorted times (ps), first bit already left out."""
    worst = 0.0
    for ti in crossings:
        for n_half in (16, 17):
            target = ti + n_half * BT_PS / 2
            k = bisect_left(crossings, target - 0.25 * BT_PS)
            best = None
            while k < len(crossings) and crossings[k] <= target + 0.25 * BT_PS:
                d = crossings[k] - target
                if best is None or abs(d) < abs(best):
                    best = d
                k += 1
            if best is not None:
                worst = max(worst, abs(best))
    return worst


def decode_tx(wave, ampl_v=2.8, jitter_lim_ps=None, b433_lim_ps=None):
    """Split the DUT TD line into packets and link pulses and check each.
    ampl_v: DUT output amplitude assumed for the figure-template windows
    (default 2.8 V, the top of 14.3.1.2.1 p.521, gives the tightest
    maximum widths)."""
    jl = LIM["TX_JITTER_DIRECT_PS"] if jitter_lim_ps is None else jitter_lim_ps
    bl = LIM["TX_JITTER_B433_PS"] if b433_lim_ps is None else b433_lim_ps
    evs = []
    for t0, t1, edges in _bursts(wave):
        zc = []
        prev = 0
        for t, l in edges:
            if prev != 0 and l != 0:
                zc.append((t, l))           # zero crossing; l = new polarity
            prev = l
        if len(zc) >= 8:
            evs.append(_decode_packet(t0, t1, edges, zc, ampl_v, jl, bl))
        else:
            evs.append(_check_pulse(t0, t1, edges, ampl_v))
    return evs


def _check_pulse(t0, t1, edges, ampl_v):
    ev = TxEvent("pulse", t0, t1)
    V = ev.violations
    lv = [l for _, l in edges]
    if lv[0] != +1:
        V.append(("NLP_POLARITY", "link pulse does not start positive", "14.3.1.2.1 p.523"))
        ev.kind = "other"
        return ev
    w = (edges[1][0] if len(edges) > 1 else t1) - t0
    ev.measures["width_ps"] = w
    wmin, wmax = nlp_width_window_ps(ampl_v)
    if not (wmin <= w <= wmax):
        V.append(("NLP_WIDTH", "link pulse %.0f ps outside %.0f..%.0f ps" % (w, wmin, wmax),
                  "Figure 14-13 p.524"))
    went_neg = False
    for i, (t, l) in enumerate(edges[1:], 1):
        if l == -1:
            went_neg = True
            t_end = edges[i + 1][0] if i + 1 < len(edges) else t1
            if t_end - t0 > nlp_tail_max_ps(ampl_v):
                V.append(("NLP_TAIL", "negative tail to %.0f ps" % (t_end - t0), "Figure 14-13 p.524"))
        if l == +1 and went_neg:
            V.append(("NLP_REBOUND", "positive again after going negative", "14.3.1.2.1 p.523"))
    return ev


def _decode_packet(t0, t1, edges, zc, ampl_v, jl, bl):
    ev = TxEvent("packet", t0, t1)
    V = ev.violations
    # mid-bit crossings. The first crossing is mid-bit (the preamble has only
    # mid-bit transitions, 4.2.5 p.261). Each next mid-bit crossing is the
    # crossing near a predicted position. Acquisition in the first 16 bits
    # (preamble, no boundary crossings): window +/-0.4 BT, gain 1/2 -> 1/4.
    # Then window +/-0.25 BT, gain 1/16. A window around the previous
    # (jittered) crossing would fail above 12.5 ns jitter.
    mids = [zc[0]]
    bnds = []
    pred, per = zc[0][0], BT_PS
    j = 1
    while True:
        target = pred + per
        n = len(mids)
        win = 0.4 if n < 16 else 0.25
        gain = 0.5 if n < 8 else (0.25 if n < 16 else 1 / 16)
        while j < len(zc) and zc[j][0] < target - win * BT_PS:
            bnds.append((zc[j][0], zc[j][1], len(mids) - 1))
            j += 1
        if j < len(zc) and zc[j][0] <= target + win * BT_PS:
            t = zc[j][0]
            mids.append(zc[j])
            pred = target + (t - target) * gain
            j += 1
        else:
            break
    if len(mids) < 2:
        ev.kind = "other"
        V.append(("NO_DATA", "burst with crossings but no Manchester bits", "7.3.1.1 p.319"))
        return ev
    bits = [1 if l == +1 else 0 for _, l in mids]
    ev.bits = bits
    idx = list(range(len(mids)))
    a, T = fit_line(idx, [t for t, _ in mids])
    ppm = (BT_PS / T - 1) * 1e6
    ev.measures["rate_ppm"] = ppm
    if abs(ppm) > LIM["RATE_PPM"]:
        V.append(("RATE", "bit rate offset %.1f ppm > 100 ppm" % ppm, "7.3.2 p.324"))
    # jitter: largest deviation of the zero crossings from the best ideal
    # half-bit grid (minimax fit). The first bit may hold phase violations
    # (14.2.1.1 p.509), so its crossings are left out.
    xs = [2 * k for k, _ in enumerate(mids) if k > 0] + [2 * k + 1 for _, _, k in bnds if k > 0]
    ys = [t for k, (t, _) in enumerate(mids) if k > 0] + [t for t, _, k in bnds if k > 0]
    _, _, jmax = minimax_fit(xs, ys, T / 2)
    ev.measures["jitter_ps"] = jmax
    if jmax > jl:
        V.append(("TX_JITTER", "zero-crossing jitter %.0f ps > %.0f ps" % (jmax, jl),
                  "14.3.1.2.3 p.525"))
    # the same crossings by the trigger method of B.4.3.3 p.6287
    jb = b433_jitter(sorted(ys))
    ev.measures["b433_jitter_ps"] = jb
    if jb > bl:
        V.append(("TX_JITTER_B433", "crossing 8.0/8.5 BT after a trigger off by %.0f ps > %.0f ps" % (jb, bl),
                  "B.4.3.3 p.6287; 14.3.1.2.3 p.525"))
    # preamble and SFD
    pos = None
    for i in range(0, max(0, len(bits) - 7)):
        if bits[i:i + 8] == SFD and all(bits[j] != bits[j + 1] for j in range(i)):
            pos = i
            break
    if pos is None:
        V.append(("NO_SFD", "no 10101011 after an alternating preamble", "3.2.2 p.240; 4.2.6 p.261"))
        return ev
    ev.preamble_bits = pos
    min_pre = LIM["PREAMBLE_BITS"] - LIM["TX_BIT_LOSS"] - 1
    if pos < min_pre:
        V.append(("PREAMBLE", "preamble %d bits < %d (56 sent, <= 2 lost, first bit may be bad)" % (pos, min_pre),
                  "7.2.3.2 p.316; 14.2.1.1 p.509"))
    fb = bits[pos + 8:]
    ev.extra_bits = len(fb) % 8
    if ev.extra_bits:
        V.append(("OCTETS", "%d bits after the last whole octet" % ev.extra_bits, "3.4 b) p.244; 7.2.3.4 p.316"))
        fb = fb[:len(fb) - ev.extra_bits]
    fr = bits_to_bytes_lsb_first(fb)
    ev.frame = fr
    if len(fr) < LIM["MIN_FRAME"]:
        V.append(("RUNT", "frame %d octets < 64" % len(fr), "Table 4-2 p.282; 4.2.3.3 p.259"))
    if len(fr) > LIM["MAX_BASIC_FRAME"]:
        V.append(("LONG", "frame %d octets > 1518" % len(fr), "Table 4-2 p.282"))
    if not eth_fcs_ok(fr):
        V.append(("FCS", "FCS does not match", "3.2.9 p.243"))
    # start of TP_IDL
    t_last_mid = mids[-1][0]
    ev.idle_start = a + (len(mids) - 1 + 0.5) * T
    # last positive-going crossing that belongs to the data (the boundary
    # crossing after a final CD0 sits at idle_start)
    rising = [t for t, l in zc if l == +1 and t <= ev.idle_start + 0.4 * BT_PS]
    t_lastpos = rising[-1] if rising else t_last_mid
    after = [(t, l) for t, l in edges if t > t_lastpos]
    t_hi_end = after[0][0] if after else t1
    hi = (t_hi_end - t_lastpos) / BT_PS
    ev.measures["tpidl_hi_bt"] = hi
    hmax = tpidl_hi_max_bt(ampl_v)
    if hi < LIM["TPIDL_HI_MIN_BT"] or hi > hmax:
        V.append(("TPIDL_HI", "HI after the last positive transition %.3f BT outside 2.5..%.3f" % (hi, hmax),
                  "Figure 14-11 p.523"))
    if (t_hi_end - ev.idle_start) / BT_PS < LIM["IDL_MIN_BT"] - 1e-9:
        V.append(("IDL_MIN", "IDL held %.3f BT < 2" % ((t_hi_end - ev.idle_start) / BT_PS), "7.3.1.1 p.321"))
    neg = False
    for i, (t, l) in enumerate(after):
        if l == -1:
            neg = True
            t_end = after[i + 1][0] if i + 1 < len(after) else t1
            if (t_end - t_lastpos) / BT_PS > tpidl_tail_max_bt(ampl_v):
                V.append(("TPIDL_TAIL", "negative tail too long", "Figure 14-11 p.523"))
        if l == +1 and neg:
            V.append(("TPIDL_REBOUND", "positive after going negative", "14.3.1.2.1 p.523"))
    return ev


def check_tx_timing(events, t_end, check_link=True):
    """Gaps between DUT events: IPG >= 96 BT between packets (Table 4-2
    p.282); link pulse silence 8..24 ms (14.2.1.1 p.509); a silence longer
    than 24 ms means a missing pulse. With check_link, the DUT's pulse and
    packet stream is fed to the Link Integrity Test function of a partner
    at every corner of the timer ranges: each must reach LINK TEST PASS and
    stay there (14.2.1.7 p.511; Figure 14-6 p.518)."""
    V = []
    prev = None
    for ev in events:
        if prev is not None:
            if ev.kind == "packet" and prev.kind == "packet" and prev.idle_start is not None:
                g = (ev.t0 - prev.idle_start) / BT_PS
                if g < LIM["IPG_BT"]:
                    V.append(("IPG", "gap %.2f BT < 96 BT at %.0f ps" % (g, ev.t0), "Table 4-2 p.282"))
            sil = ev.t0 - prev.t1
            if ev.kind == "pulse" and sil < LIM["NLP_SILENCE_MIN_PS"]:
                V.append(("NLP_EARLY", "silence %.4f ms < 8 ms before a link pulse" % (sil / 1e9),
                          "14.2.1.1 p.509"))
            if sil > LIM["NLP_SILENCE_MAX_PS"]:
                V.append(("NLP_MISSING", "silence %.4f ms > 24 ms" % (sil / 1e9), "14.2.1.1 p.509"))
        prev = ev
    if prev is not None and t_end - prev.t1 > LIM["NLP_SILENCE_MAX_PS"]:
        V.append(("NLP_MISSING", "silence at the end %.4f ms > 24 ms" % ((t_end - prev.t1) / 1e9),
                  "14.2.1.1 p.509"))
    if check_link:
        rd = []
        for ev in events:
            if ev.kind == "pulse":
                rd.append(("pulse", ev.t0))
            elif ev.kind == "packet":
                rd.append(("active", ev.t0, ev.idle_start if ev.idle_start else ev.t1))
        for p in lit_corner_params(2):
            tr = LinkIntegrity(*p, start=FAIL_RESET).run(rd, t_end)
            first = next((t for t, s in tr if s == PASS), None)
            if first is None:
                V.append(("LINK_NOT_UP", "partner with timers %s never reached PASS" % (p,),
                          "14.2.1.7 p.511; Figure 14-6 p.518"))
                continue
            if any(s != PASS for t, s in tr if t > first):
                V.append(("LINK_DROP", "partner with timers %s left PASS" % (p,),
                          "14.2.1.7 p.511; Figure 14-6 p.518"))
    return V
