"""USB LS line layer: coding rules, round trips, host-side error injections,
device-checker limits (just inside passes, just outside fails)."""
import random

import pytest

from linemodel import usb_ls as L
from linemodel.wave import Wave

T = L.BIT_PS
NS = 1000.0


def test_pid_check_field_and_table():
    # Table 8-1 p.196 codes; check field = ones' complement (8.3.1 p.195)
    for name, code in L.PID.items():
        b = L.pid_byte(name)
        assert b & 0xF == code and (b >> 4) == (~code & 0xF)


def test_sync_is_kjkjkjkk():
    syms = L.nrzi(L.SYNC_BITS)
    assert syms == list("KJKJKJKK")


def test_stuffing_counts_sync_one_and_end_of_packet():
    # SYNC's last 1 + five 1s = six ones -> stuffed 0 (7.1.9 p.157)
    st, idx = L.stuff(L.SYNC_BITS + [1] * 5)
    assert idx == [13] and st[13] == 0 and len(st) == 14
    # a stuffed 0 also after the last bit before EOP
    st, idx = L.stuff([1] * 6)
    assert st == [1] * 6 + [0]


def test_nrzi_rule():
    assert L.nrzi([0, 1, 1, 0], start="J") == ["K", "K", "K", "J"]


def _random_packet(rng):
    k = rng.randrange(3)
    if k == 0:
        return L.token_bits(rng.choice(["IN", "OUT", "SETUP"]), rng.randrange(128), rng.randrange(16))
    if k == 1:
        n = rng.randrange(9)
        return L.data_bits(rng.choice(["DATA0", "DATA1"]), bytes(rng.randrange(256) for _ in range(n)))
    return L.handshake_bits(rng.choice(["ACK", "NAK", "STALL"]))


@pytest.mark.parametrize("ppm", [-500.0, 0.0, 500.0])
def test_round_trip_host_rate_limits_with_receiver_stress(ppm):
    """Host at the TLDRATHS limits (+/-500 ppm, Table 7-10 p.182) with jitter
    at TDJR1/TDJR2: the receiver-view decoder recovers every packet."""
    rng = random.Random(1000 + int(ppm))
    for i in range(150):
        bits = _random_packet(rng)
        w = L.host_rx_stress(L.packet_wave(bits, ppm=ppm), seed=i)
        d = L.decode_packet(w)
        assert d.ok, d.errors
        re = (L.data_bits(d.pid_name, d.payload) if d.pid_name in L.DATAS else
              L.token_bits(d.pid_name, d.fields["addr"], d.fields["endp"]) if d.pid_name in L.TOKENS else
              L.handshake_bits(d.pid_name))
        assert re == bits


@pytest.mark.parametrize("ppm", [-500.0, 500.0])
def test_host_rate_at_tldraths_limit_measured(ppm):
    # without jitter the fitted bit time gives the rate offset to < 1 ppm
    d = L.decode_packet(L.packet_wave(L.data_bits("DATA0", bytes(range(8))), ppm=ppm))
    assert abs((L.BIT_PS / d.bit_ps - 1) * 1e6 - ppm) < 1.0


def test_host_stress_reaches_but_does_not_exceed_tdjr():
    rng = random.Random(7)
    worst_n = worst_p = 0.0
    for i in range(200):
        bits = L.data_bits("DATA0", bytes(rng.randrange(256) for _ in range(8)))
        w0 = L.packet_wave(bits)
        w = L.host_rx_stress(w0, seed=i)
        d = L.decode_packet(w)
        nd, pd = L.jitter_devs(d.transitions, T)
        worst_n = max(worst_n, max(abs(x) for x in nd))
        worst_p = max(worst_p, max(abs(x) for x in pd))
    assert worst_n <= L.LIM["TDJR1_PS"] + 1 and worst_n > 0.9 * L.LIM["TDJR1_PS"]
    assert worst_p <= L.LIM["TDJR2_PS"] + 1 and worst_p > 0.9 * L.LIM["TDJR2_PS"]


# ---- host-side error injections: each detected by the decoder -----------
INJECTIONS = [
    ("bad CRC5", lambda: L.packet_wave(L.token_bits("IN", 3, 1, bad_crc=True)), {"CRC5"}),
    ("bad CRC16", lambda: L.packet_wave(L.data_bits("DATA0", b"\x80\x06\x00\x01\x00\x00\x40\x00", bad_crc=True)), {"CRC16"}),
    ("wrong PID check", lambda: L.packet_wave(L.token_bits("SETUP", 0, 0, bad_pid=True)), {"PID"}),
    ("stuffing violation", lambda: L.packet_wave(L.data_bits("DATA0", b"\xff" * 4), L.Inject(stuff_violation=True)), {"STUFF"}),
    ("stuffing violation, no stuffed bit", lambda: L.packet_wave(L.token_bits("IN", 0, 0), L.Inject(stuff_violation=True)), {"STUFF"}),
    ("missing EOP", lambda: L.packet_wave(L.token_bits("IN", 3, 1), L.Inject(eop="none")), {"NO_EOP"}),
    ("short EOP < TLST", lambda: L.packet_wave(L.token_bits("IN", 3, 1), L.Inject(eop_se0_ps=200 * NS)), {"NO_EOP"}),
    ("truncated token", lambda: L.packet_wave(L.token_bits("IN", 3, 1), L.Inject(truncate_bits=20)), {"LEN"}),
    ("truncated data", lambda: L.packet_wave(L.data_bits("DATA0", bytes(8)), L.Inject(truncate_bits=40)), {"CRC16"}),
]


@pytest.mark.parametrize("name,build,codes", INJECTIONS, ids=[i[0] for i in INJECTIONS])
def test_host_injection_detected(name, build, codes):
    d = L.decode_packet(build())
    assert d.found
    assert codes & d.codes(), (name, d.errors)


def test_eop_at_tleopr_is_accepted():
    # 670 ns SE0 must be accepted (7.1.13.2.1 p.161)
    d = L.decode_packet(L.packet_wave(L.token_bits("IN", 3, 1), L.Inject(eop_se0_ps=670 * NS)))
    assert d.ok


def test_dribble_is_accepted():
    # up to six bit times without transition before EOP (7.1.9.1 p.157)
    d = L.decode_packet(L.packet_wave(L.handshake_bits("ACK"), L.Inject(dribble_bits=1)))
    assert d.ok and d.pid_name == "ACK"


# ---- device checker ------------------------------------------------------
def dev_wave(bits=None, ppm=0.0, reply_bits=3.0, **kw):
    bits = bits if bits is not None else L.data_bits("DATA1", b"\xff" * 8)
    bit = T / (1 + ppm * 1e-6)
    host_eop = 0.0
    w = L.packet_wave(bits, ppm=ppm, lead_idle_ps=reply_bits * bit, **kw)
    return w, host_eop


def codes(w, ref=0.0):
    return L.check_device_packet(w, host_eop_end_ps=ref).codes()


def test_device_nominal_passes():
    w, ref = dev_wave()
    assert codes(w, ref) == set()


@pytest.mark.parametrize("rate,ok", [(1.4775e6 * (1 + 2e-5), True), (1.4775e6 * (1 - 2e-5), False),
                                     (1.5225e6 * (1 - 2e-5), True), (1.5225e6 * (1 + 2e-5), False)])
def test_device_rate_limits(rate, ok):
    ppm = (rate / L.BIT_RATE - 1) * 1e6
    w, ref = dev_wave(ppm=ppm, bits=L.handshake_bits("ACK"))
    assert ("RATE" not in codes(w, ref)) == ok


@pytest.mark.parametrize("width,ok", [(1250 * NS + 1000, True), (1250 * NS - 1000, False),
                                      (1500 * NS - 1000, True), (1500 * NS + 1000, False)])
def test_device_eop_width(width, ok):
    w, ref = dev_wave(bits=L.handshake_bits("NAK"), inj=L.Inject(eop_se0_ps=width))
    c = codes(w, ref)
    assert ("EOP_WIDTH" not in c) == ok


@pytest.mark.parametrize("bits,code,ok", [(7.49, "IPD_MAX", True), (7.51, "IPD_MAX", False),
                                          (2.01, "IPD_MIN", True), (1.99, "IPD_MIN", False)])
def test_device_reply_time(bits, code, ok):
    w, ref = dev_wave(bits=L.handshake_bits("ACK"), reply_bits=bits)
    assert (code not in codes(w, ref)) == ok


def _shift_transition(w, k, dt):
    """Move the k-th J<->K transition by dt."""
    n = [-1]

    def f(i, t, o, nw):
        if {L._norm(o), nw} == {"J", "K"}:
            n[0] += 1
            if n[0] == k:
                return dt
        return 0.0
    return w.with_edge_offsets(f)


def test_device_tudj1_limit():
    # -1.5 % device (just inside TLDRATE), 0xFF data: runs of 7 bit times;
    # extra driver shift delta makes next-transition deviation 95 ns -/+ 0.5
    ppm = (1.4775e6 * (1 + 1e-6) / L.BIT_RATE - 1) * 1e6
    w, ref = dev_wave(ppm=ppm)
    Tm = T / (1 + ppm * 1e-6)
    d = L.decode_packet(w, se0_glitch_ps=0)
    k = next(i for i in range(1, len(d.transitions))
             if d.transitions[i][1] - d.transitions[i - 1][1] == 7)
    delta = 95 * NS - 7 * (Tm - T)
    assert delta < 25 * NS                      # still inside the 25 ns driver limit
    c_in = codes(_shift_transition(w, k, delta - 500), ref)
    c_out = codes(_shift_transition(w, k, delta + 500), ref)
    assert "TUDJ1" not in c_in and "TUDJ1" in c_out
    assert "SRC_NEXT" not in c_out


def test_device_tudj2_limit():
    ppm = (1.4775e6 * (1 + 1e-6) / L.BIT_RATE - 1) * 1e6
    w, ref = dev_wave(ppm=ppm)
    Tm = T / (1 + ppm * 1e-6)
    d = L.decode_packet(w, se0_glitch_ps=0)
    k = next(i for i in range(2, len(d.transitions))
             if d.transitions[i][1] - d.transitions[i - 2][1] == 14)
    delta = 150 * NS - 14 * (Tm - T)
    assert delta < 10 * NS
    c_in = codes(_shift_transition(w, k, delta - 500), ref)
    c_out = codes(_shift_transition(w, k, delta + 500), ref)
    assert "TUDJ2" not in c_in and "TUDJ2" in c_out
    assert "TUDJ1" not in c_out and "SRC_PAIRED" not in c_out


def test_device_source_jitter_consecutive_limit():
    # duty distortion: J->K edges +d/2, K->J edges -d/2 -> next dev = d, paired 0
    w, ref = dev_wave()

    def duty(d):
        return w.with_edge_offsets(lambda i, t, o, n: (d / 2 if n == "K" else -d / 2)
                                   if {L._norm(o), n} == {"J", "K"} else 0.0)
    c_in, c_out = codes(duty(24.5 * NS), ref), codes(duty(25.5 * NS), ref)
    assert "SRC_NEXT" not in c_in and "SRC_NEXT" in c_out
    assert "SRC_PAIRED" not in c_out


def test_device_source_jitter_paired_limit():
    w, ref = dev_wave()
    c_in = codes(_shift_transition(w, 10, 9.5 * NS), ref)
    c_out = codes(_shift_transition(w, 10, 10.5 * NS), ref)
    assert "SRC_PAIRED" not in c_in and "SRC_PAIRED" in c_out
    assert "SRC_NEXT" not in c_out


@pytest.mark.parametrize("dt,ok", [(-39 * NS, True), (-41 * NS, False), (99 * NS, True), (101 * NS, False)])
def test_device_tldeop(dt, ok):
    w, ref = dev_wave(bits=L.handshake_bits("ACK"))
    se0, end = w.marks["eop_se0"], w.marks["eop_end"]
    w2 = w.with_edge_offsets(lambda i, t, o, n: dt if abs(t - se0) < 1 or abs(t - end) < 1 else 0.0)
    c = codes(w2, ref)
    assert ("TLDEOP" not in c) == ok
    assert "EOP_WIDTH" not in c


DEVICE_FAULTS = [
    ("bad CRC16", lambda: dev_wave(L.data_bits("DATA1", b"\x12\x01", bad_crc=True)), "CRC16"),
    ("stuffing error", lambda: dev_wave(L.data_bits("DATA1", b"\xff" * 2), inj=L.Inject(stuff_violation=True)), "STUFF"),
    ("wrong PID check", lambda: dev_wave(L.handshake_bits("ACK", bad_pid=True)), "PID"),
    ("payload 9 bytes", lambda: dev_wave(L.data_bits("DATA0", bytes(9))), "PAYLOAD"),
    ("missing EOP", lambda: dev_wave(L.handshake_bits("NAK"), inj=L.Inject(eop="none")), "NO_EOP"),
    ("truncated", lambda: dev_wave(L.data_bits("DATA0", bytes(4)), inj=L.Inject(truncate_bits=30)), "LEN"),
]


@pytest.mark.parametrize("name,build,code", DEVICE_FAULTS, ids=[f[0] for f in DEVICE_FAULTS])
def test_device_fault_detected(name, build, code):
    w, ref = build()
    assert code in codes(w, ref)


def test_device_eop_not_followed_by_j():
    w, ref = dev_wave(bits=L.handshake_bits("ACK"))
    end = w.marks["eop_end"]
    ed = [(t, ("K" if abs(t - end) < 1 else l)) for t, l in w.edges]
    assert "EOP_J" in codes(Wave(w.initial, ed, end=w.end), ref)


def test_no_response_detected():
    assert "NO_RESPONSE" in codes(Wave("J", [], end=20 * T), 0.0)
    w, _ = dev_wave(bits=L.handshake_bits("ACK"))
    assert L.check_no_response(w, 0.0, 18 * T)
    assert not L.check_no_response(Wave("J", [], end=20 * T), 0.0, 18 * T)
