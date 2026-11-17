"""USB 2.0 low-speed line model: line states, SYNC, NRZI, bit stuffing, PIDs,
CRC5/CRC16, EOP, packets, waveforms, a decoder and a timing checker.

All references are to "Universal Serial Bus Specification Revision 2.0"
(usb_20.pdf), printed page numbers. Only section and page are cited.

Line states (low speed): J = differential 0 (D- high), K = differential 1
(D+ high), SE0 = both low (Table 7-2, p.145). The idle bus is J (Table 7-2).
"IDLE" in this model means the host driver is off; the device pull-up on D-
holds the bus at J (7.1.5.1 p.141, Table 7-2 p.145), so a receiver reads it as J.
"""
from dataclasses import dataclass, field

from .crc import (usb_crc5_bits, usb_crc16_bits, usb_crc5_check,
                  usb_crc16_check, bytes_to_bits_lsb_first,
                  bits_to_bytes_lsb_first)
from .wave import Wave, fit_line

# ---------------------------------------------------------------------------
# Timing limits. ps unless the name says otherwise.
# ---------------------------------------------------------------------------
BIT_RATE = 1.5e6                       # LS nominal 1.50 Mb/s, 7.1.11 p.159
BIT_PS = 1e12 / BIT_RATE               # 666 666.67 ps
LIM = {
    # LS function (device) transmit rate 1.4775..1.5225 Mb/s (+/-1.5 %),
    # 7.1.11 p.159, Table 7-10 TLDRATE p.182
    "TLDRATE_MIN_BPS": 1.4775e6,
    "TLDRATE_MAX_BPS": 1.5225e6,
    # Host/hub LS transmit rate 1.49925..1.50075 Mb/s (+/-0.05 %),
    # 7.1.11 p.159, Table 7-10 TLDRATHS p.182
    "TLDRATHS_PPM": 500.0,
    # Device (upstream port) source jitter incl. frequency tolerance:
    # next +/-95 ns, paired +/-150 ns. Table 7-10 TUDJ1/TUDJ2 p.182
    "TUDJ1_PS": 95_000, "TUDJ2_PS": 150_000,
    # LS driver jitter vs the actual period: consecutive +/-25 ns,
    # paired +/-10 ns. 7.1.13.1.1 p.160
    "SRC_NEXT_PS": 25_000, "SRC_PAIRED_PS": 10_000,
    # Device differential-to-SE0 transition jitter -40..+100 ns,
    # Table 7-10 TLDEOP p.182 (Note 11 p.185: the two transitions are a
    # nominal bit time apart)
    "TLDEOP_MIN_PS": -40_000, "TLDEOP_MAX_PS": 100_000,
    # Device receiver must decode with jitter next +/-75 ns, paired
    # +/-45 ns. Table 7-10 TDJR1/TDJR2 p.182
    "TDJR1_PS": 75_000, "TDJR2_PS": 45_000,
    # Source SE0 of EOP 1.25..1.50 us, 7.1.13.2.1 p.161, Table 7-10
    # TLEOPT p.183
    "TLEOPT_MIN_PS": 1_250_000, "TLEOPT_MAX_PS": 1_500_000,
    # Receiver must accept an SE0 of EOP as short as 670 ns, 7.1.13.2.1
    # p.161, Table 7-10 TLEOPR p.183
    "TLEOPR_PS": 670_000,
    # SE0 during a differential transition up to 210 ns, Table 7-10 TLST
    # p.183 (an SE0 this short is part of a transition, not an EOP)
    "TLST_PS": 210_000,
    # Inter-packet delay at least 2 bit times (device and host),
    # 7.1.18.1 p.168, Table 7-14 TIPD p.188
    "TIPD_BITS": 2.0,
    # Device response < 7.5 bit times with captive cable, 7.1.18.1 p.168,
    # Table 7-14 TRSPIPD2 p.188. A LS device has a captive cable,
    # 7.1.1.2 p.128.
    "TRSPIPD2_BITS": 7.5,
    # Host response at most 7.5 bit times, 7.1.18.1 p.168
    "HOST_RSP_MAX_BITS": 7.5,
    # Response timeout: not before 16, before 18 bit times; host waits at
    # least 18 bit times. 7.1.19.1 p.168
    "TIMEOUT_MIN_BITS": 16.0, "TIMEOUT_MAX_BITS": 18.0,
    # Reset: hub drives SE0 >= 10 ms (TDRST), root port >= 50 ms (TDRSTR);
    # device may detect SE0 > 2.5 us (TDETRST, max 10 ms); recovery 10 ms
    # (TRSTRCY). 7.1.7.5 p.153, Table 7-13 p.186, Table 7-14 p.188
    "TDRST_PS": 10_000_000_000, "TDRSTR_PS": 50_000_000_000,
    "TDETRST_MIN_PS": 2_500_000, "TDETRST_MAX_PS": 10_000_000_000,
    "TRSTRCY_PS": 10_000_000_000,
    # Suspend after > 3.0 ms idle; LS keep-alive (an LS EOP) at least once
    # per 1 ms frame. 7.1.7.6 p.154-155, Table 7-2 note 4 p.145,
    # 11.8.4.1 p.332, frame 1.000 ms 7.1.12 p.159
    "SUSPEND_PS": 3_000_000_000, "FRAME_PS": 1_000_000_000,
    # SetAddress recovery 2 ms, 9.2.6.3 p.246; SetAddress complete 50 ms
    # (TDSETADDR), no-data request 50 ms, first data packet 500 ms,
    # later packets 500 ms, status after last data 50 ms, 9.2.6.4 p.246,
    # Table 7-14 p.189
    "TRSETADDR_RECOVERY_PS": 2_000_000_000,
    "TDSETADDR_PS": 50_000_000_000, "TDRQCMPLTND_PS": 50_000_000_000,
    "TDRETDATA1_PS": 500_000_000_000, "TDRETDATAN_PS": 500_000_000_000,
    # LS max data payload 8 bytes, 8.4.4 p.206; control 5.5.3 p.39
    "LS_MAX_PAYLOAD": 8,
}

# ---------------------------------------------------------------------------
# PIDs, Table 8-1 p.196. Value is PID<3:0>; bit 0 is sent first.
# ---------------------------------------------------------------------------
PID = {
    "OUT": 0b0001, "IN": 0b1001, "SOF": 0b0101, "SETUP": 0b1101,
    "DATA0": 0b0011, "DATA1": 0b1011, "DATA2": 0b0111, "MDATA": 0b1111,
    "ACK": 0b0010, "NAK": 0b1010, "STALL": 0b1110, "NYET": 0b0110,
    "PRE": 0b1100, "SPLIT": 0b1000, "PING": 0b0100,
}
PID_NAME = {v: k for k, v in PID.items()}      # 1100 -> PRE (ERR shares it)
TOKENS = {"OUT", "IN", "SOF", "SETUP"}
DATAS = {"DATA0", "DATA1", "DATA2", "MDATA"}
HANDSHAKES = {"ACK", "NAK", "STALL", "NYET"}

SYNC_BITS = [0, 0, 0, 0, 0, 0, 0, 1]   # 7.1.10 p.159 (KJKJKJKK after NRZI)
LS_PINS = {"J": (0, 1), "K": (1, 0), "SE0": (0, 0), "SE1": (1, 1)}  # (D+, D-)
# Driver table with output enable for the host model: (D+, D-, OE).
# IDLE = driver off; the device's D- pull-up makes the bus J.
LS_DRIVE = {"J": (0, 1, 1), "K": (1, 0, 1), "SE0": (0, 0, 1), "IDLE": (0, 1, 0)}


def pid_byte(name, bad_check=False):
    """PID with its check field: check = ones' complement of PID<3:0>
    (8.3.1 p.195)."""
    t = PID[name]
    chk = (~t) & 0xF
    if bad_check:
        chk ^= 0x1          # one check bit wrong
    return t | (chk << 4)


def int_bits(value, n):
    return [(value >> i) & 1 for i in range(n)]       # LSb first, 8.1 p.195


# ---------------------------------------------------------------------------
# Packet bit streams (NRZ, before stuffing, without SYNC)
# ---------------------------------------------------------------------------
def token_bits(pid_name, addr, endp, bad_crc=False, bad_pid=False):
    """Token: PID, ADDR(7), ENDP(4), CRC5 over ADDR+ENDP. 8.4.1 p.199."""
    f = int_bits(addr, 7) + int_bits(endp, 4)
    crc = usb_crc5_bits(f)
    if bad_crc:
        crc[0] ^= 1
    return int_bits(pid_byte(pid_name, bad_pid), 8) + f + crc


def sof_bits(frame, bad_crc=False):
    """SOF: PID, frame number (11), CRC5. 8.4.3 p.204."""
    f = int_bits(frame, 11)
    crc = usb_crc5_bits(f)
    if bad_crc:
        crc[0] ^= 1
    return int_bits(pid_byte("SOF"), 8) + f + crc


def data_bits(pid_name, payload, bad_crc=False, bad_pid=False):
    """Data packet: PID, data bytes LSb first, CRC16. 8.4.4 p.206."""
    f = bytes_to_bits_lsb_first(payload)
    crc = usb_crc16_bits(f)
    if bad_crc:
        crc[-1] ^= 1
    return int_bits(pid_byte(pid_name, bad_pid), 8) + f + crc


def handshake_bits(pid_name, bad_pid=False):
    """Handshake: PID only. 8.4.5 p.206."""
    return int_bits(pid_byte(pid_name, bad_pid), 8)


# ---------------------------------------------------------------------------
# Bit stuffing and NRZI
# ---------------------------------------------------------------------------
def stuff(bits):
    """Insert a 0 after six consecutive 1s, counting from the SYNC field
    (the final SYNC 1 counts). Also after the last bit before EOP.
    7.1.9 p.157. Returns (stuffed_bits, indices_of_stuffed_zeros)."""
    out, idx, run = [], [], 0
    for b in bits:
        out.append(b)
        if b:
            run += 1
            if run == 6:
                idx.append(len(out))
                out.append(0)
                run = 0
        else:
            run = 0
    return out, idx


def nrzi(bits, start="J"):
    """NRZI: 1 = no change, 0 = change. 7.1.8 p.157."""
    lvl, out = start, []
    for b in bits:
        if b == 0:
            lvl = "K" if lvl == "J" else "J"
        out.append(lvl)
    return out


# ---------------------------------------------------------------------------
# Error injection description
# ---------------------------------------------------------------------------
@dataclass
class Inject:
    """Line-level faults applied when a packet is turned into symbols.

    stuff_violation: remove the first stuffed 0 (gives seven 1s); if the
        packet has no stuffed bit, overwrite 7 bits after the PID with 1s.
    truncate_bits: keep only this many packet bits (after SYNC, before
        stuffing), then send EOP.
    eop: "normal" | "none" (no SE0; the driver goes to IDLE = J).
    eop_se0_ps: SE0 width override (default: 2 bit times of the source).
    dribble_bits: extend the last data symbol by this many bit times
        (7.1.9.1 p.157: a receiver must accept up to six bit times with no
        transition before EOP).
    """
    stuff_violation: bool = False
    truncate_bits: int = None
    eop: str = "normal"
    eop_se0_ps: float = None
    dribble_bits: float = 0.0


def packet_symbols(packet_bits, inj=None):
    inj = inj or Inject()
    pb = list(packet_bits)
    if inj.truncate_bits is not None:
        pb = pb[:inj.truncate_bits]
    st, idx = stuff(SYNC_BITS + pb)
    if inj.stuff_violation:
        if idx:
            st[idx[0]] = 1
        else:
            p = 16 if len(st) >= 23 else 8
            for k in range(p, min(p + 7, len(st))):
                st[k] = 1
            if len(st) < p + 7:
                st += [1] * (p + 7 - len(st))
    return nrzi(st), st


def packet_wave(packet_bits, inj=None, ppm=0.0, lead_idle_ps=0.0,
                se0_bits=2.0, j_bits=1.0):
    """Waveform of one packet as sent by a LS source.

    ppm: source rate offset (positive = fast). Bit time and default EOP
    scale with it. EOP: SE0 for se0_bits, then J for j_bits, then IDLE
    (7.1.7.4.1 p.151). Marks: sop, eop_se0, eop_end (SE0->J), end.
    """
    inj = inj or Inject()
    bit = BIT_PS / (1.0 + ppm * 1e-6)
    syms, _ = packet_symbols(packet_bits, inj)
    segs = []
    if lead_idle_ps > 0:
        segs.append(("IDLE", lead_idle_ps))
    for s in syms:
        if segs and segs[-1][0] == s:
            segs[-1] = (s, segs[-1][1] + bit)
        else:
            segs.append((s, bit))
    if inj.dribble_bits:
        segs[-1] = (segs[-1][0], segs[-1][1] + inj.dribble_bits * bit)
    t_sop = lead_idle_ps
    t_data_end = sum(d for _, d in segs)      # lead idle + all data symbols
    marks = {"sop": t_sop}
    if inj.eop == "none":
        segs.append(("IDLE", bit))
        marks["eop_end"] = None
    else:
        se0 = inj.eop_se0_ps if inj.eop_se0_ps is not None else se0_bits * bit
        marks["eop_se0"] = t_data_end
        marks["eop_end"] = t_data_end + se0
        segs.append(("SE0", se0))
        segs.append(("J", j_bits * bit))
        segs.append(("IDLE", 0.0))
    marks = {k: v for k, v in marks.items() if v is not None}
    w = Wave.from_segments(segs, initial="IDLE", marks=marks)
    return w


def se0_wave(duration_ps, then_idle_ps=0.0):
    """Plain SE0 (bus reset, 7.1.7.5 p.153) followed by IDLE."""
    return Wave.from_segments([("SE0", duration_ps), ("IDLE", then_idle_ps)],
                              initial="IDLE")


def keepalive_wave(ppm=0.0):
    """LS keep-alive = a LS EOP (Table 7-2 note 4 p.145; 11.8.4.1 p.332)."""
    bit = BIT_PS / (1.0 + ppm * 1e-6)
    return Wave.from_segments([("SE0", 2 * bit), ("J", bit), ("IDLE", 0.0)],
                              initial="IDLE",
                              marks={"eop_se0": 0.0, "eop_end": 2 * bit})


def host_rx_stress(wave, seed, rand_ps=None, duty_ps=None):
    """Jitter for a host-to-device stimulus that reaches the device receiver
    limits TDJR1 (next transition, +/-75 ns) and TDJR2 (paired, +/-45 ns),
    Table 7-10 p.182, without exceeding them.

    Every J<->K transition gets an independent uniform offset in
    [-rand, +rand] with rand = TDJR2/2 (paired deviation <= TDJR2), plus a
    fixed duty distortion: J->K edges +duty, K->J edges -duty with
    duty = (TDJR1 - TDJR2)/2. Consecutive deviation is then <= 2*rand +
    2*duty = TDJR1. Edges to or from SE0/IDLE are not moved.
    """
    rand_ps = LIM["TDJR2_PS"] / 2 if rand_ps is None else rand_ps
    duty_ps = (LIM["TDJR1_PS"] - LIM["TDJR2_PS"]) / 2 if duty_ps is None else duty_ps
    import random
    rng = random.Random(seed)

    def off(i, t, old, new):
        o = "J" if old == "IDLE" else old
        if {o, new} == {"J", "K"}:
            d = rng.uniform(-rand_ps, rand_ps)
            return d + (duty_ps if new == "K" else -duty_ps)
        return 0.0
    w = wave.with_edge_offsets(off)
    w.marks = dict(wave.marks)
    return w


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------
@dataclass
class Decoded:
    found: bool = False
    pid_name: str = None
    pid_raw: int = None
    payload: bytes = b""          # data bytes (data packets), or token fields
    fields: dict = field(default_factory=dict)
    errors: list = field(default_factory=list)   # (code, text, ref)
    info: list = field(default_factory=list)
    sop: float = None
    se0_start: float = None
    eop_end: float = None
    se0_width: float = None
    bit_ps: float = None
    phase: float = None
    transitions: list = field(default_factory=list)  # (t, bit_index)
    n_line_bits: int = 0

    def err(self, code, text, ref):
        self.errors.append((code, text, ref))

    @property
    def ok(self):
        return self.found and not self.errors

    def codes(self):
        return {e[0] for e in self.errors}


def _norm(lvl):
    return "J" if lvl == "IDLE" else lvl


def decode_packet(wave, t_from=None, nominal_bit_ps=BIT_PS,
                  se0_glitch_ps=None, max_run_bits=7.5, allow_dribble=True):
    """Decode the first packet that starts at or after t_from.

    se0_glitch_ps: SE0 runs shorter than this inside a packet are treated
    as part of a differential transition (receiver view, TLST 210 ns,
    Table 7-10 p.183). Use 0 for a strict check of a DUT output: then any
    SE0 ends the packet.
    max_run_bits: a J/K run longer than this ends decoding (no EOP).
    """
    if se0_glitch_ps is None:
        se0_glitch_ps = LIM["TLST_PS"]
    d = Decoded()
    t_from = float("-inf") if t_from is None else t_from
    runs = [(a, b, _norm(l)) for a, b, l in wave.runs()]
    # find SOP: J (or IDLE) -> K
    prev = _norm(wave.initial)
    i0 = None
    for i, (a, b, l) in enumerate(runs):
        if a >= t_from and l == "K" and prev == "J":
            i0 = i
            break
        prev = l
    if i0 is None:
        return d
    d.found = True
    d.sop = runs[i0][0]
    # merge short SE0 glitches into the transition (receiver view)
    seq = []
    for a, b, l in runs[i0:]:
        if l == "SE0" and (b - a) < se0_glitch_ps and seq and seq[-1][2] in "JK":
            continue                         # ignore, previous level continues
        if seq and seq[-1][2] == l:
            seq[-1] = (seq[-1][0], b, l)
            continue
        if seq and seq[-1][2] in ("J", "K") and l in ("J", "K") and seq[-1][1] != a:
            seq[-1] = (seq[-1][0], a, seq[-1][2])
        seq.append((a, b, l))
    # J/K runs until SE0
    jk, se0 = [], None
    for k, (a, b, l) in enumerate(seq):
        if l in ("J", "K"):
            jk.append((a, b, l))
        else:
            if l == "SE0":
                se0 = (a, b, k)
            else:
                d.err("SE1", "SE1 on the bus at %.0f ps" % a, "Table 7-2 p.145")
            break
    if not jk:
        d.err("SYNC", "no J/K data", "7.1.10 p.159")
        return d
    # first estimate of the bit time from SYNC: 7 transitions over 6 bits
    trans = [r[0] for r in jk]
    if len(trans) >= 7:
        T = (trans[6] - trans[0]) / 6.0
    else:
        T = nominal_bit_ps
    if not (0.8 * nominal_bit_ps < T < 1.2 * nominal_bit_ps):
        T = nominal_bit_ps
    a0 = trans[0]
    # end of data: SE0 start, or the end of an over-long run
    t_end = se0[0] if se0 else None
    # run lengths from the time between neighbouring transitions (a run is
    # at most 7 bit times, so a small error in T cannot add up), then a
    # least-squares fit of transition time against bit index; twice
    for it in range(2):
        idx = [0]
        for k in range(1, len(trans)):
            idx.append(idx[-1] + max(1, int(round((trans[k] - trans[k - 1]) / T))))
        if len(trans) >= 3:
            a0, T = fit_line(idx, trans)
    d.bit_ps, d.phase = T, a0
    d.transitions = list(zip(trans, idx))
    # bits in the last run
    last_t = trans[-1]
    if t_end is None:
        # no SE0: count the last run up to max_run_bits + 1 and report
        n_last = int(max_run_bits) + 1
        d.err("NO_EOP", "no SE0 after the packet data", "7.1.7.4.1 p.151")
    else:
        n_last = int(round((t_end - last_t) / T))
        if n_last < 1:
            n_last = 1
        if n_last > max_run_bits:
            d.err("STUFF", "run of %d bit times before SE0" % n_last,
                  "7.1.9.1 p.157")
    # symbol per bit
    syms = []
    for k in range(len(jk)):
        n = (idx[k + 1] - idx[k]) if k + 1 < len(jk) else n_last
        syms += [jk[k][2]] * n
    d.n_line_bits = len(syms)
    # NRZI decode
    raw, prevl = [], "J"
    for s in syms:
        raw.append(1 if s == prevl else 0)
        prevl = s
    if raw[:8] != SYNC_BITS:
        d.err("SYNC", "SYNC not KJKJKJKK: %s" % "".join(map(str, raw[:8])),
              "7.1.10 p.159")
    # unstuff (count from SYNC)
    bits, run, k = [], 0, 0
    while k < len(raw):
        b = raw[k]
        if run == 6:
            if b == 1:
                d.err("STUFF", "seven consecutive ones at line bit %d" % k,
                      "7.1.9.1 p.157")
            run = 0
            k += 1
            continue
        bits.append(b)
        run = run + 1 if b else 0
        k += 1
    pbits = bits[8:]
    # dribble: up to six bit times with no transition before EOP may add a
    # bit (7.1.9.1 p.157); drop one trailing extra bit if it is a 1
    if allow_dribble and len(pbits) % 8 == 1 and pbits and pbits[-1] == 1 \
            and n_last <= 6 and se0:
        pbits = pbits[:-1]
        d.info.append("dribble bit dropped")
    if len(pbits) < 8:
        d.err("LEN", "fewer than 8 bits after SYNC", "8.3.1 p.195")
        return _eop(d, seq, se0, T)
    pid = sum(pbits[i] << i for i in range(8))
    d.pid_raw = pid
    if ((pid >> 4) ^ 0xF) != (pid & 0xF):
        d.err("PID", "PID check field not the complement: %02x" % pid,
              "8.3.1 p.195")
    name = PID_NAME.get(pid & 0xF)
    d.pid_name = name
    body = pbits[8:]
    if name in TOKENS:
        if len(body) != 16:
            d.err("LEN", "token not 3 bytes (%d bits after PID)" % len(body),
                  "8.4.1 p.199")
        elif not usb_crc5_check(body):
            d.err("CRC5", "CRC5 residual wrong", "8.3.5.1 p.198")
        if len(body) >= 11:
            v = sum(body[i] << i for i in range(11))
            if name == "SOF":
                d.fields = {"frame": v}
            else:
                d.fields = {"addr": v & 0x7F, "endp": v >> 7}
    elif name in DATAS:
        if len(body) % 8 or len(body) < 16:
            d.err("LEN", "data packet length %d bits after PID" % len(body),
                  "8.4.4 p.206")
        else:
            if not usb_crc16_check(body):
                d.err("CRC16", "CRC16 residual wrong", "8.3.5.2 p.198")
            d.payload = bits_to_bytes_lsb_first(body[:-16])
    elif name in HANDSHAKES:
        if len(body) != 0:
            d.err("LEN", "handshake longer than one byte", "8.4.5 p.206")
    else:
        if name is None:
            d.err("PID", "undefined PID", "8.3.1 p.196")
    return _eop(d, seq, se0, T)


def _eop(d, seq, se0, T):
    if se0 is None:
        return d
    a, b, k = se0
    d.se0_start, d.se0_width = a, b - a
    nxt = seq[k + 1][2] if k + 1 < len(seq) else "J"
    if nxt != "J":
        d.err("EOP_J", "SE0 not followed by J (%s)" % nxt, "7.1.7.4.1 p.151")
    d.eop_end = b
    return d


# ---------------------------------------------------------------------------
# Jitter measurement (7.1.13.1.1 p.160: time between transitions is
# N * TPERIOD +/- jitter; consecutive and paired transitions)
# ---------------------------------------------------------------------------
def jitter_devs(transitions, t_ref_ps):
    """transitions: [(t, bit_index)]. Returns (next_devs, paired_devs)."""
    nd, pd = [], []
    for k in range(1, len(transitions)):
        (t0, i0), (t1, i1) = transitions[k - 1], transitions[k]
        nd.append((t1 - t0) - (i1 - i0) * t_ref_ps)
    for k in range(2, len(transitions)):
        (t0, i0), (t2, i2) = transitions[k - 2], transitions[k]
        pd.append((t2 - t0) - (i2 - i0) * t_ref_ps)
    return nd, pd


# ---------------------------------------------------------------------------
# Checker for a packet sent by the device under test
# ---------------------------------------------------------------------------
@dataclass
class ReplyCheck:
    decoded: Decoded = None
    violations: list = field(default_factory=list)   # (code, text, ref)
    measures: dict = field(default_factory=dict)

    @property
    def ok(self):
        return not self.violations

    def codes(self):
        return {v[0] for v in self.violations}


def check_device_packet(wave, host_eop_end_ps=None, t_from=None,
                        expect_pid=None, lim=LIM):
    """Check one device packet against the device transmit limits.

    host_eop_end_ps: time of the host EOP SE0->J transition in the same
    time base (for the inter-packet delay checks), or None.
    """
    rc = ReplyCheck()
    d = decode_packet(wave, t_from=t_from, se0_glitch_ps=0, max_run_bits=7.5,
                      allow_dribble=False)
    rc.decoded = d
    V = rc.violations
    if not d.found:
        V.append(("NO_RESPONSE", "no SOP from the device", "7.1.19.1 p.168"))
        return rc
    V.extend(d.errors)
    T = d.bit_ps
    rate = 1e12 / T
    rc.measures["bit_rate_bps"] = rate
    if not (lim["TLDRATE_MIN_BPS"] <= rate <= lim["TLDRATE_MAX_BPS"]):
        V.append(("RATE", "bit rate %.1f b/s outside 1.4775..1.5225 Mb/s" % rate,
                  "7.1.11 p.159; Table 7-10 TLDRATE p.182"))
    nd, pd = jitter_devs(d.transitions, BIT_PS)
    snd, spd = jitter_devs(d.transitions, T)
    m = lambda xs: max((abs(x) for x in xs), default=0.0)
    rc.measures.update(next_nom_ps=m(nd), paired_nom_ps=m(pd),
                       next_src_ps=m(snd), paired_src_ps=m(spd))
    if m(nd) > lim["TUDJ1_PS"]:
        V.append(("TUDJ1", "next-transition jitter %.0f ps > 95 ns" % m(nd),
                  "Table 7-10 TUDJ1 p.182"))
    if m(pd) > lim["TUDJ2_PS"]:
        V.append(("TUDJ2", "paired-transition jitter %.0f ps > 150 ns" % m(pd),
                  "Table 7-10 TUDJ2 p.182"))
    if m(snd) > lim["SRC_NEXT_PS"]:
        V.append(("SRC_NEXT", "consecutive jitter %.0f ps > 25 ns (actual period)" % m(snd),
                  "7.1.13.1.1 p.160"))
    if m(spd) > lim["SRC_PAIRED_PS"]:
        V.append(("SRC_PAIRED", "paired jitter %.0f ps > 10 ns (actual period)" % m(spd),
                  "7.1.13.1.1 p.160"))
    if d.se0_start is not None:
        w = d.se0_width
        rc.measures["eop_se0_ps"] = w
        if not (lim["TLEOPT_MIN_PS"] <= w <= lim["TLEOPT_MAX_PS"]):
            V.append(("EOP_WIDTH", "SE0 of EOP %.0f ps outside 1.25..1.50 us" % w,
                      "7.1.13.2.1 p.161; Table 7-10 TLEOPT p.183"))
        # differential-to-SE0 jitter against the nominal bit grid
        t_last, i_last = d.transitions[-1]
        n = int(round((d.se0_start - t_last) / T))
        dev = d.se0_start - t_last - n * BIT_PS
        rc.measures["tldeop_ps"] = dev
        if not (lim["TLDEOP_MIN_PS"] <= dev <= lim["TLDEOP_MAX_PS"]):
            V.append(("TLDEOP", "diff-to-SE0 jitter %.0f ps outside -40..+100 ns" % dev,
                      "Table 7-10 TLDEOP p.182"))
    if host_eop_end_ps is not None:
        ipd = d.sop - host_eop_end_ps
        bits = ipd / T                        # bit time of the response, 7.1.18.1
        rc.measures["ipd_bits"] = bits
        rc.measures["ipd_nominal_bits"] = ipd / BIT_PS
        if bits < lim["TIPD_BITS"]:
            V.append(("IPD_MIN", "device reply after %.3f bit times (< 2)" % bits,
                      "7.1.18.1 p.168"))
        if bits >= lim["TRSPIPD2_BITS"]:
            V.append(("IPD_MAX", "device reply after %.3f bit times (>= 7.5)" % bits,
                      "7.1.18.1 p.168; Table 7-14 TRSPIPD2 p.188"))
    if expect_pid is not None and d.pid_name not in (
            expect_pid if isinstance(expect_pid, (set, tuple, list)) else {expect_pid}):
        V.append(("PID_UNEXPECTED", "got %s, expected %s" % (d.pid_name, expect_pid),
                  "8.5 / Table 8-4..8-6 p.208-209"))
    if d.pid_name in DATAS and len(d.payload) > lim["LS_MAX_PAYLOAD"]:
        V.append(("PAYLOAD", "LS data payload %d > 8 bytes" % len(d.payload),
                  "8.4.4 p.206"))
    return rc


def check_no_response(wave, t_from, window_ps):
    """The device must stay silent (no K) in [t_from, t_from + window)."""
    for t, l in wave.edges:
        if t_from <= t < t_from + window_ps and l in ("K", "SE0", "SE1"):
            return [("UNEXPECTED_RESPONSE",
                     "device drove %s at %.0f ps after an invalid packet" % (l, t - t_from),
                     "8.3.1 p.196; 8.4.6 p.207; 8.7.3 p.237")]
    return []
