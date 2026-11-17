"""10BASE-T link-partner model: Manchester convention, round trips with
jitter and drift, DUT-transmit checker limits, error injections, link
pulses and the Link Integrity Test state machine."""
import binascii
import random
import struct

import pytest

from linemodel import tenbaset as E
from linemodel.crc import eth_fcs_ok
from linemodel.wave import Wave

BT = E.BT_PS
NS = 1000.0
MS = 1e9
DA, SA = bytes.fromhex("0200000000aa"), bytes.fromhex("0200000000bb")
RX_STRESS_B433 = 2 * E.LIM["RX_JITTER_PS"] + 1     # two crossings, each up to 13.5 ns


def independent_frames():
    """Frames whose FCS comes from binascii.crc32, not from the model."""
    rng = random.Random(8642)
    out = []
    for n in (64, 64, 64, 1518):
        body = bytes(rng.randrange(256) for _ in range(n - 4))
        out.append(body + struct.pack("<I", binascii.crc32(body) & 0xFFFFFFFF))
    return out


def frame(n, seed=1):
    r = random.Random(seed)
    return E.build_frame(DA, SA, 0x88B5, bytes(r.randrange(256) for _ in range(n - 18)))


def tx(fr, **kw):
    return E.manchester_wave(E.packet_bits(fr, **{k: v for k, v in kw.items()
                                                  if k in ("preamble_bits", "sfd", "extra_bits")}),
                             **{k: v for k, v in kw.items() if k in ("ppm", "tpidl_hi_bt", "tpidl_tail_bt")})


def one(w, **kw):
    evs = E.decode_tx(w, **kw)
    assert len(evs) == 1
    return evs[0]


def codes(ev):
    return {v[0] for v in ev.violations}


def test_manchester_convention():
    # CD0 = HI then LO, CD1 = LO then HI (7.3.1.1 p.319)
    w = E.manchester_wave([0, 1], tpidl_hi_bt=3.0)
    assert w.edges[:3] == [(0, +1), (50000, -1), (150000, +1)]


def test_preamble_has_only_mid_bit_transitions():
    # periodic 5 MHz waveform (4.2.5 p.261): edges every 100 ns
    w = E.manchester_wave(E.packet_bits(b"", sfd=False))
    ts = [t for t, l in w.edges if l != 0][1:56]
    assert all(b - a == 100000 for a, b in zip(ts, ts[1:]))


def test_build_frame_pad_and_fcs():
    fr = E.build_frame(DA, SA, 0x0800, b"hi")
    assert len(fr) == 64 and eth_fcs_ok(fr)


@pytest.mark.parametrize("ppm,seed", [(-100.0, 1), (100.0, 2), (0.0, 3)])
def test_independent_frames_round_trip_with_rx_stress(ppm, seed):
    """Frames with an independent FCS through the partner transmitter at
    +/-100 ppm and +/-13.5 ns zero-crossing jitter (7.3.2 p.324, 14.3.1.3.1
    p.527) decode bit-exact; the FCS still checks."""
    for i, fr in enumerate(independent_frames()):
        w = E.zero_crossing_jitter(tx(fr, ppm=ppm), E.LIM["RX_JITTER_PS"], seed=seed * 10 + i, mode="extreme")
        ev = one(w, jitter_lim_ps=E.LIM["RX_JITTER_PS"] + 1, b433_lim_ps=RX_STRESS_B433)
        # the rate estimate of a jittered frame has a small error; RATE at
        # exactly 100 ppm is checked without jitter in test_tx_rate_limit
        assert ev.frame == fr and codes(ev) <= {"RATE"}, ev.violations


def test_random_frames_round_trip():
    r = random.Random(99)
    for n in [64, 65, 127, 128, 511, 1000, 1517, 1518]:
        fr = frame(n, seed=n)
        w = E.zero_crossing_jitter(tx(fr, ppm=r.uniform(-100, 100)), 13500, seed=n)
        ev = one(w, jitter_lim_ps=13501, b433_lim_ps=RX_STRESS_B433)
        assert ev.frame == fr and not codes(ev)


# ---- DUT transmit checker: limits ----------------------------------------
FR = frame(64)


@pytest.mark.parametrize("j,ok", [(7999, True), (8001, False)])
def test_tx_jitter_limit(j, ok):
    w = E.zero_crossing_jitter(tx(FR), j, seed=5, mode="extreme")
    assert ("TX_JITTER" not in codes(one(w))) == ok


@pytest.mark.parametrize("j,ok", [(5400, True), (8000, False)])
def test_tx_jitter_b433_limit(j, ok):
    # B.4.3.3 p.6287: crossings 8.0 and 8.5 BT after a trigger within +/-11 ns
    # (integrated MAU). Two crossings each moved by up to j differ by up to 2j.
    w = E.zero_crossing_jitter(tx(frame(512)), j, seed=6, mode="extreme")
    ev = one(w)
    assert ("TX_JITTER_B433" not in codes(ev)) == ok
    assert ev.measures["b433_jitter_ps"] <= 2 * j + 1


@pytest.mark.parametrize("ppm,ok", [(99.9, True), (100.1, False), (-99.9, True), (-100.1, False)])
def test_tx_rate_limit(ppm, ok):
    assert ("RATE" not in codes(one(tx(frame(1518), ppm=ppm)))) == ok


@pytest.mark.parametrize("hi,ok", [(2.5, True), (2.49, False), (4.64, True), (4.66, False)])
def test_tpidl_hi_window(hi, ok):
    # 2.5 BT minimum; maximum 4.6475 BT for a 2.8 V square driver
    # (Figure 14-11 p.523)
    assert ("TPIDL_HI" not in codes(one(tx(FR, tpidl_hi_bt=hi)))) == ok


def test_tpidl_tail_and_rebound():
    assert "TPIDL_TAIL" not in codes(one(tx(FR, tpidl_tail_bt=2.0)))
    assert "TPIDL_TAIL" in codes(one(tx(FR, tpidl_tail_bt=6.0)))
    w = tx(FR, tpidl_tail_bt=1.0)
    end = w.edges[-1][0]
    w2 = Wave(w.initial, w.edges[:-1] + [(end, +1), (end + 50000, 0)], end=end + 50000)
    assert "TPIDL_REBOUND" in codes(one(w2))


def _two(gap_bt):
    a = tx(FR)
    b = tx(FR)
    pad = a.marks["idle_start"] + gap_bt * BT - a.end
    return E.decode_tx(a.concat(b, gap_ps=pad))


@pytest.mark.parametrize("gap,ok", [(96.0, True), (95.9, False)])
def test_ipg(gap, ok):
    v = E.check_tx_timing(_two(gap), 0, check_link=False)
    assert ("IPG" not in {x[0] for x in v}) == ok


TX_FAULTS = [
    ("bad FCS", lambda: tx(FR[:-1] + bytes([FR[-1] ^ 1])), "FCS"),
    ("runt 60 octets", lambda: tx(E.build_frame(DA, SA, 0x88B5, bytes(38), pad=False)), "RUNT"),
    ("long 1519 octets", lambda: tx(frame(1519)), "LONG"),
    ("dribble 3 bits", lambda: tx(FR, extra_bits=(1, 0, 1)), "OCTETS"),
    ("preamble 52 bits", lambda: tx(FR, preamble_bits=52), "PREAMBLE"),
    ("missing SFD", lambda: tx(FR, sfd=False), "NO_SFD"),
]


@pytest.mark.parametrize("name,build,code", TX_FAULTS, ids=[f[0] for f in TX_FAULTS])
def test_tx_fault_detected(name, build, code):
    assert code in codes(one(build()))


def test_preamble_53_bits_passes():
    assert "PREAMBLE" not in codes(one(tx(FR, preamble_bits=53)))


# ---- link pulses -------------------------------------------------------
@pytest.mark.parametrize("w_ps,ok", [(85000, True), (84000, False), (137000, True), (138500, False)])
def test_nlp_width_window(w_ps, ok):
    # Figure 14-13 p.524 with the template origin at the pulse start, 2.8 V
    # square pulse: 0.85 BT .. 1.375 BT
    ev = one(E.nlp_wave(w_ps))
    assert ev.kind == "pulse" and ("NLP_WIDTH" not in codes(ev)) == ok


def _pulse_train(silences, width=None):
    p = E.LinkPartnerTx(nlp_width_ps=width)
    p.idle(sum(silences) + 1 * MS, silences=silences)
    return p


@pytest.mark.parametrize("sil,code,ok", [(8.0, "NLP_EARLY", True), (7.99, "NLP_EARLY", False),
                                         (24.0, "NLP_MISSING", True), (24.01, "NLP_MISSING", False)])
def test_nlp_silence_window(sil, code, ok):
    p = _pulse_train([16 * MS, sil * MS, 16 * MS])
    evs = E.decode_tx(p.wave)
    v = E.check_tx_timing(evs, p.wave.end, check_link=False)
    assert (code not in {x[0] for x in v}) == ok


def test_dut_nlp_train_keeps_every_partner_corner_up():
    p = _pulse_train([16 * MS] * 30)
    assert E.check_tx_timing(E.decode_tx(p.wave), p.wave.end) == []


def test_dut_nlp_too_fast_fails_link_corners():
    p = _pulse_train([5 * MS] * 60)
    c = {x[0] for x in E.check_tx_timing(E.decode_tx(p.wave), p.wave.end)}
    assert "NLP_EARLY" in c and "LINK_NOT_UP" in c


def test_dut_nlp_too_slow_drops_link():
    p = _pulse_train([16 * MS] * 12 + [160 * MS] + [16 * MS] * 3)
    c = {x[0] for x in E.check_tx_timing(E.decode_tx(p.wave), p.wave.end)}
    assert "NLP_MISSING" in c and "LINK_DROP" in c


# ---- Link Integrity Test state machine (Figure 14-6 p.518) -------------
def pulses(period_ms, n, t0_ms=0.0):
    return [("pulse", (t0_ms + period_ms * (k + 1)) * MS) for k in range(n)]


def test_lit_reaches_pass_after_lc_max_pulses():
    tr = E.LinkIntegrity(50 * MS, 3, 25 * MS, 2 * MS).run(pulses(16, 3), 90 * MS)
    st = [s for _, s in tr]
    assert st.count(E.FAIL_COUNT) == 3 and st[-1] == E.PASS
    assert E.LinkIntegrity.state_at(tr, 47.9 * MS) != E.PASS
    assert E.LinkIntegrity.state_at(tr, 48.1 * MS) == E.PASS


def test_lit_link_loss():
    li = E.LinkIntegrity(50 * MS, 2, 25 * MS, 2 * MS, start=E.PASS)
    tr = li.run(pulses(16, 2), 200 * MS)
    assert E.LinkIntegrity.state_at(tr, 32 * MS + 49.9 * MS) == E.PASS
    assert E.LinkIntegrity.state_at(tr, 32 * MS + 50.1 * MS) == E.FAIL


def test_lit_packet_ends_fail():
    tr = E.LinkIntegrity(50 * MS, 10, 25 * MS, 2 * MS).run([("active", 10 * MS, 10.1 * MS)], 20 * MS)
    assert E.LinkIntegrity.state_at(tr, 10.05 * MS) == E.FAIL_EXTEND
    assert E.LinkIntegrity.state_at(tr, 10.2 * MS) == E.PASS


def test_lit_early_pulse_resets_count_in_fail():
    li = E.LinkIntegrity(50 * MS, 3, 25 * MS, 7 * MS)
    tr = li.run(pulses(16, 2) + [("pulse", 33 * MS)] + pulses(16, 3, t0_ms=33), 200 * MS)
    resets = [t for t, s in tr if s == E.FAIL_RESET]
    assert any(abs(t - 33 * MS) < 1 for t in resets)
    assert E.LinkIntegrity.state_at(tr, 33 * MS + 48.1 * MS) == E.PASS


def test_lit_early_pulse_ignored_in_pass():
    li = E.LinkIntegrity(50 * MS, 2, 25 * MS, 7 * MS, start=E.PASS)
    tr = li.run([("pulse", 16 * MS), ("pulse", 17 * MS)], 200 * MS)
    # the 17 ms pulse is ignored: link_loss counts from 16 ms
    assert E.LinkIntegrity.state_at(tr, 65.9 * MS) == E.PASS
    assert E.LinkIntegrity.state_at(tr, 66.1 * MS) == E.FAIL


def test_expected_link_state_for_partner_stimuli():
    good = pulses(16, 20)
    assert E.expected_link_state(good, 330 * MS) == "PASS"
    # pulses closer than link_test_min: a MAU that starts in LINK TEST FAIL
    # RESET never comes up; one that is already in PASS ignores every other
    # pulse and may stay up (2 ms timer), so the result is EITHER
    assert E.expected_link_state(pulses(1.9, 200), 330 * MS, starts=(E.FAIL_RESET,)) == "FAIL"
    assert E.expected_link_state(pulses(1.9, 200), 330 * MS) == "EITHER"
    assert E.expected_link_state(pulses(151, 3), 460 * MS) == "FAIL"      # > link_test_max, link_loss
    mixed = pulses(16, 12) + pulses(100, 2, t0_ms=192)                     # 100 ms gaps
    assert E.expected_link_state(mixed, 392 * MS - 1) == "EITHER"


# ---- partner stream and expected receive results ----------------------
def test_partner_stream_is_compliant_by_own_checker():
    p = E.LinkPartnerTx()
    p.idle(40 * MS)
    for n in (64, 1518, 64):
        p.frame(frame(n, seed=n))
    p.idle(40 * MS)
    evs = E.decode_tx(p.wave)
    assert [e.kind for e in evs].count("packet") == 3
    assert all(not e.violations for e in evs)
    assert E.check_tx_timing(evs, p.wave.end, check_link=False) == []


def test_partner_nlp_outside_window_detected():
    p = E.LinkPartnerTx()
    p.idle(60 * MS, silences=[16 * MS, 6 * MS, 30 * MS])
    v = {x[0] for x in E.check_tx_timing(E.decode_tx(p.wave), p.wave.end, check_link=False)}
    assert {"NLP_EARLY", "NLP_MISSING"} <= v


def test_expected_rx_verdicts():
    good = frame(64)
    bad = good[:-1] + bytes([good[-1] ^ 0x80])
    runt = E.build_frame(DA, SA, 0x88B5, bytes(38), pad=False)
    assert E.expected_rx(good)[0] == "DELIVER"
    assert E.expected_rx(good, jitter_ps=13500, ppm=200)[0] == "DELIVER"
    assert E.expected_rx(bad)[0] == "DISCARD"
    assert E.expected_rx(runt)[0] == "DISCARD"
    assert E.expected_rx(good, extra_bits=(1, 1, 0))[0] == "DELIVER"
    assert E.expected_rx(bad, extra_bits=(1, 1, 0))[0] == "DISCARD"
    assert E.expected_rx(good, sfd=False)[0] == "DISCARD"
    assert E.expected_rx(good, preamble_bits=54)[0] == "DELIVER"
    assert E.expected_rx(good, preamble_bits=50)[0] == "EITHER"
    assert E.expected_rx(frame(1519))[0] == "EITHER"
