"""Two protocols from outside the census pool, the blind arm and this repository, run on the general serial
line unit (tb.u_slu_ut: pe_slu with SPEC 0, FULL 1) by configuration only, each against a model written from
the public document; and made-up frame parameters that belong to no named protocol. Research test (branch
rt1-g1): it is not in the chip's test list.

O1  CCSDS TC Synchronization and Channel Coding, CCSDS 231.0-B-4 (July 2021). CLTU = Start Sequence EB90
    (5.2.2.2), BCH (63,56) codewords of 64 bits: 56 information bits, the 7 COMPLEMENTED parity bits of
    g(x) = x^7 + x^6 + x^2 + 1 with the shift registers initialised to zero (3.2.2, 3.3.2, figure 3-2), and
    a Filler Bit that "shall always be a zero" (3.2.3); Tail Sequence C5C5 C5C5 C5C5 C579 (5.2.4.1); the
    acquisition and idle sequences alternate 0 and 1 (PLOP, section 7). NRZ-L; the rate is a managed
    parameter of the physical layer: 1 Mbit/s = 60 cycles here.
O2  CCSDS Proximity-1 Coding and Synchronization Sublayer, CCSDS 211.2-B-3 (October 2019). PLTU = 24-bit
    ASM FAF320 (3.2.3.2), Transfer Frame, CRC-32 over the frame only (3.2.5.4) with G(X) = X^32 + X^23 +
    X^21 + X^11 + X^2 + 1, register preset all zero, first bit = highest power, no complement (annex C);
    idle data = the PN sequence 352EF853 repeated (3.3.2.2); data rates 1000 ... 1024000, 2048000 bit/s
    (3.4.2.1 note). Physical layer CCSDS 211.1-B-4 3.3.5.1: Bi-Phase-L, here a '1' is high then low. The
    unit sends at 60 MHz / 58 = 1,034.5 kbit/s (+1.0 % of 1,024,000) and receives the model's exact
    1,024,000 bit/s (58.59 cycles) with 59 samples a bit.
M   Made-up parameters, no named protocol: M1 NRZ at 24 cycles a bit, stuffing after 5 ones, CRC-12 (x^12 +
    x^11 + x^3 + x^2 + x + 1, preset ones, complemented), start pattern B5 after a 55 preamble, 40 data bits;
    M2 Manchester at 10 cycles a half bit, stuffing after 4 equal bits, the same CRC with preset 0 and not
    complemented, start pattern 9D, 56 data bits.

The checks a model makes are the document's: parity, filler, CRC, start pattern. Controls: a corrupted check
bit, a wrong stuffing count, a wrong pattern, a wrong rate.
"""

import random

import cocotb
from cocotb.triggers import FallingEdge, RisingEdge, Timer

from test_slu import (GL, DIN, AUX0, AUX1, T_NS, P_TXDATA, P_RXDATA, P_STATUS, P_TXCTRL, P_TXTIME, P_RXINFO,
                      P_TXLAST, Slu, slu_reset, slu_reset_again, mode, drive_levels, capture)

EXT0, EXT1 = 21, 22
F_TXDONE, F_RXSOP, F_RXEOP, F_CRCA, F_STUFF = 1 << 0, 1 << 4, 1 << 5, 1 << 6, 1 << 8
NOPIN = 15 << 4 | 15 << 8


# ------------------------------------------------------------------------------------------------------
# Bit helpers and the models (written from the documents; the unit's own conventions are not used here)
# ------------------------------------------------------------------------------------------------------

def bits_msb(data):
    return [(b >> (7 - i)) & 1 for b in data for i in range(8)]


def bytes_msb(bits):
    return bytes(int("".join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits) - 7, 8))


def pack_words(bits):
    """Bit i of the stream in bit i % 16 of word i // 16 (the unit sends bit 0 of a word first)."""
    words = [0] * ((len(bits) + 15) // 16)
    for i, b in enumerate(bits):
        words[i // 16] |= b << (i % 16)
    return words, (len(bits) % 16) or 16


def crc_msb(bits, width, poly, init=0):
    """Shift-left register; the first bit is the coefficient of the highest power (CCSDS annex C form)."""
    reg, mask = init, (1 << width) - 1
    for b in bits:
        fb = ((reg >> (width - 1)) & 1) ^ b
        reg = ((reg << 1) & mask) ^ (poly & mask if fb else 0)
    return reg


def crc_bits(reg, width):
    return [(reg >> (width - 1 - i)) & 1 for i in range(width)]


def unit_residue(bits, width, poly_ref, preset_ones):
    """The value a reflected shift-right register (the unit's form) holds after the bits: used only to set the
    residue word of the unit, which is a constant for a given code."""
    mask = (1 << width) - 1
    c = mask if preset_ones else 0
    for b in bits:
        c = ((c >> 1) ^ (poly_ref if (c ^ b) & 1 else 0)) & mask
    return c


def stuff(bits, k, ones):
    out, run, last = [], 0, None
    for b in bits:
        out.append(b)
        if ones:
            run = run + 1 if b else 0
        else:
            run = run + 1 if b == last else 1
        last = b
        if run == k:
            s = 0 if ones else 1 - b
            out.append(s)
            run, last = (0 if ones else 1), s
    return out


def destuff(bits, k, ones):
    out, run, last, err = [], 0, None, False
    i = 0
    while i < len(bits):
        b = bits[i]
        if run == k:
            exp = 0 if ones else 1 - last
            if b != exp:
                err = True
            run, last = (0 if ones else 1), b
            i += 1
            continue
        out.append(b)
        if ones:
            run = run + 1 if b else 0
        else:
            run = run + 1 if b == last else 1
        last = b
        i += 1
    return out, err


# O1: CCSDS 231.0-B-4
TC_START = bits_msb(bytes.fromhex("EB90"))
TC_TAIL = bits_msb(bytes.fromhex("C5C5C5C5C5C5C579"))
TC_G = 0x45                                   # x^7 + x^6 + x^2 + 1 below x^7: x^6, x^2, 1


def tc_codeword(info56):
    par = crc_msb(info56, 7, TC_G)            # registers initialised to zero (3.3.2 note)
    return list(info56) + [1 - b for b in crc_bits(par, 7)] + [0]


def tc_codeword_ok(cw):
    return cw[56:63] == [1 - b for b in crc_bits(crc_msb(cw[:56], 7, TC_G), 7)] and cw[63] == 0


def tc_decode(stream):
    """Search the start sequence (5.3, state S2), then decode 64-bit codewords until one is rejected (the
    tail sequence is a non-correctable pattern, 5.2.4.1). Returns (found, [info blocks], rejected at index)."""
    s = "".join(map(str, stream))
    p = s.find("".join(map(str, TC_START)))
    if p < 0:
        return False, [], None
    blocks, i = [], p + 16
    while i + 64 <= len(stream):
        cw = stream[i:i + 64]
        if not tc_codeword_ok(cw):
            return True, blocks, len(blocks)
        blocks.append(cw[:56])
        i += 64
    return True, blocks, None


# O2: CCSDS 211.2-B-3 and 211.1-B-4
PROX_ASM = bytes.fromhex("FAF320")
PROX_G = 0x00A00805                           # X^23 + X^21 + X^11 + X^2 + 1 below X^32
PROX_PN = bytes.fromhex("352EF853")


def prox_pltu(frame):
    return bits_msb(PROX_ASM) + bits_msb(frame) + crc_bits(crc_msb(bits_msb(frame), 32, PROX_G), 32)


def prox_decode(stream, nframe):
    s = "".join(map(str, stream))
    p = s.find("".join(map(str, bits_msb(PROX_ASM))))
    if p < 0:
        return False, None, False
    body = stream[p + 24:p + 24 + 8 * nframe + 32]
    if len(body) < 8 * nframe + 32:
        return True, None, False
    ok = crc_msb(body, 32, PROX_G) == 0       # remainder of frame + CRC is zero (annex C)
    return True, bytes_msb(body[:8 * nframe]), ok


def biphase_l(bits):
    """211.1-B-4 3.3.5: Bi-Phase-L; a '1' is the first half high, the second half low."""
    out = []
    for b in bits:
        out += [1, 0] if b else [0, 1]
    return out


def manchester_ieee(bits):
    out = []
    for b in bits:
        out += [0, 1] if b else [1, 0]
    return out


# ------------------------------------------------------------------------------------------------------
# Unit helpers
# ------------------------------------------------------------------------------------------------------

async def pins_idle(dut, level):
    dut.slu_pins.value = (level << DIN)
    await Timer(1000, unit="ns")


def schedule(levels, sym_ns, t0=2000.0, idle=1):
    sch = [(t0 - 500.0, {DIN: idle})]
    for i, lv in enumerate(levels):
        sch.append((t0 + i * sym_ns, {DIN: lv}))
    sch.append((t0 + len(levels) * sym_ns, {DIN: idle}))
    return sch, t0 + len(levels) * sym_ns


def oe_runs(caps):
    """(first index, length) of each run of tx_oe = 1 in a capture."""
    runs, start = [], None
    for i, c in enumerate(caps):
        if c[1] and start is None:
            start = i
        if not c[1] and start is not None:
            runs.append((start, i - start))
            start = None
    if start is not None:
        runs.append((start, len(caps) - start))
    return runs


def levels_of(caps, first, per, n, sel, phase):
    return [(caps[first + per * i + phase][0] >> sel) & 1 for i in range(n) if first + per * i + phase < len(caps)]


async def wait_idle(dut):
    u = dut.u_slu_ut
    while int(u.tx_st.value) != 0:
        await FallingEdge(dut.clk)
    return int(dut.slu_now.value)


async def send_chain(dut, s, frames, per_bit, caps_len):
    """frames: list of dict(words, last, crc, skip, bits). Each frame starts when the unit is idle again: at the
    planned time (the previous start + its bits x per_bit) when the lead is still 4 or more, else at once.
    Returns the capture and a list of (planned, armed as 'time' or 'now')."""
    caps, log = [], []
    cocotb.start_soon(capture(dut, caps_len, caps))
    t_prev, bits_prev = None, 0
    for k, f in enumerate(frames):
        await s.cfg({14: f["crcw"]})
        await s.wr(P_TXCTRL, (f["last"] % 16) | (f["crc"] << 5))
        words = list(f["words"])
        pre = words[:2]
        rest = words[2:]
        for i, w in enumerate(pre):
            await s.wr(P_TXLAST if (i == len(words) - 1) else P_TXDATA, w)
        c = await wait_idle(dut)
        if t_prev is None:
            planned = (c + 40) & 0xFFFF
            await s.wr(P_TXTIME, planned)
            log.append((planned, "time"))
        else:
            planned = (t_prev + bits_prev * per_bit) & 0xFFFF
            if ((planned - (c + 1)) & 0xFFFF) >= 4 and ((planned - (c + 1)) & 0xFFFF) < 1000:
                await s.wr(P_TXTIME, planned)
                log.append((planned, "time"))
            else:
                await s.wr(P_TXCTRL, (f["last"] % 16) | (f["crc"] << 5) | (1 << 9))
                log.append((planned, "now"))
        t_prev, bits_prev = planned, f["bits"]
        feed = rest
        while feed:
            free = await s.rd(P_TXDATA)
            if free:
                w = feed.pop(0)
                await s.wr(P_TXLAST if not feed else P_TXDATA, w)
        if rest:
            while (await s.rd(P_TXDATA)) != 2:       # the frame has taken every word
                pass
    while len(caps) < caps_len:
        await RisingEdge(dut.clk)
    return caps, log


async def receive(dut, s, sch, t_end, max_eops=8, stop_after=None):
    """Drive the line and collect, per end of frame, (status, words popped so far). Returns the list."""
    await s.wr(P_STATUS, 0xFFFF)
    cocotb.start_soon(drive_levels(dut, sch))
    events, words = [], []
    t_stop = t_end + 3000.0
    t = 0.0
    sop = False
    while t < t_stop:
        st = await s.rd(P_STATUS)
        info = await s.rd(P_RXINFO)
        while (info >> 5) & 3:
            words.append(await s.rd(P_RXDATA, pop=True))
            info = await s.rd(P_RXINFO)
        if st & F_RXSOP:
            sop = True
        if st & F_RXEOP:
            events.append((st, list(words), info & 0x1F))
            await s.wr(P_STATUS, F_RXEOP | F_RXSOP)
            if stop_after is not None and len(events) >= stop_after:
                break
        t += 4 * T_NS
        await Timer(round(2 * T_NS * 1000), unit="ps")
    return events, sop, words


def words_to_bits(words, total):
    return [(words[i // 16] >> (i % 16)) & 1 for i in range(min(total, 16 * len(words)))]


# ------------------------------------------------------------------------------------------------------
# O1: CCSDS TC CLTU at 1 Mbit/s
# ------------------------------------------------------------------------------------------------------

@cocotb.test(skip=GL)
async def test_rt1_o1_tc_cltu(dut):
    rng = random.Random(231)
    per = 60
    infos = [[rng.randint(0, 1) for _ in range(56)] for _ in range(3)]
    cws = [tc_codeword(i) for i in infos]
    res7 = unit_residue(cws[0], 7, 0x51, False)
    assert res7 == unit_residue(cws[1], 7, 0x51, False), "test fault: the residue of a complemented CRC is not a constant"
    log = []

    # ---- transmit: start sequence + 3 codewords + tail sequence, one unit frame per codeword ----
    s = await slu_reset(dut)
    await s.cfg({0: mode(0, tsel=3), 1: per, 8: NOPIN, 9: 0x51, 10: 0, 11: res7, 12: 0, 15: per - 4,
                 EXT1: 1 << 3})                                  # NRZ, CRC-7 (reflected 0x51), tail level 0
    frames = []
    w, last = pack_words(TC_START + infos[0])
    frames.append(dict(words=w, last=last, crc=1, crcw=7 | (1 << 7), bits=16 + 64))
    for info in infos[1:]:
        w, last = pack_words(info)
        frames.append(dict(words=w, last=last, crc=1, crcw=7, bits=64))
    w, last = pack_words(TC_TAIL)
    frames.append(dict(words=w, last=last, crc=0, crcw=7, bits=64))
    caps, arm = await send_chain(dut, s, frames, per, per * (16 + 4 * 64) + 800)
    runs = oe_runs(caps)
    stream, gaps, prev_end = [], [], None
    for first, length in runs:
        n = (length + per // 2) // per
        stream += levels_of(caps, first, per, n, 0, per // 2)
        if prev_end is not None:
            gaps.append(first - prev_end)
        prev_end = first + length
    found, blocks, rej = tc_decode(stream)
    low_filler = all(c[0] & 1 == 0 for first, length in runs[:3] for c in caps[first + length - (per - 4):first + length])
    log.append(f"TX: {len(runs)} unit frames, armed {[a[1] for a in arm]}, gaps between frames {gaps} cycles; model: "
               f"start found {found}, {len(blocks)} codewords decoded, rejected at {rej}; filler low for {per - 4} "
               f"of {per} cycles: {low_filler}")
    assert found and blocks == infos and rej == 3, log[-1]
    assert gaps == [4, 4, 4] or all(0 <= g <= 8 for g in gaps), log[-1]

    # ---- receive the model's CLTU: pattern EB90, blocks of 64 bits, CRC-7 complemented ----
    s = await slu_reset_again(dut)
    rx_cfg = {0: mode(0, rx=1, eop_len=1), 1: per, 2: per, 3: 0, 4: 20 | (20 << 6) | (1 << 13), 5: (2 << 6),
              6: 0xEB90, 7: 15, 8: DIN | NOPIN, 9: 0x51, 10: 0, 11: res7, 12: 0, 14: 7,
              EXT0: 64, EXT1: 1 << 8}
    await s.cfg(rx_cfg)
    acq = [0, 1] * 8
    cltu = TC_START + sum(cws, []) + TC_TAIL
    sch, t_end = schedule(acq + cltu + acq, per * T_NS)
    events, sop, _ = await receive(dut, s, sch, t_end, stop_after=4)
    await s.cfg({0: mode(0)})                                      # stop the block receive after the tail
    flags = [bool(e[0] & F_CRCA) for e in events]
    got = [words_to_bits(events[i][1][4 * i:4 * i + 4], 64) for i in range(min(3, len(events)))]
    log.append(f"RX: {len(events)} ends of frame, CRCA_OK per block {flags}, words per event "
               f"{[len(e[1]) for e in events]}; codewords equal {got == cws[:len(got)]}")
    assert sop and len(events) >= 4 and flags[:4] == [True, True, True, False] and got == cws, log[-1]

    # ---- controls ----
    s = await slu_reset_again(dut)
    await s.cfg(rx_cfg)
    bad = list(cws[1])
    bad[60] ^= 1                                                   # a parity bit of codeword 2
    sch, t_end = schedule(acq + TC_START + cws[0] + bad + cws[2] + TC_TAIL + acq, per * T_NS)
    ev_bad, _, _ = await receive(dut, s, sch, t_end, stop_after=4)
    await s.cfg({0: mode(0)})
    flags_bad = [bool(e[0] & F_CRCA) for e in ev_bad]
    s = await slu_reset_again(dut)
    await s.cfg(dict(rx_cfg, **{6: 0xEB91}))                       # wrong start pattern
    sch, t_end = schedule(acq + cltu + acq, per * T_NS)
    ev_pat, sop_pat, _ = await receive(dut, s, sch, t_end, stop_after=1)
    await s.cfg({0: mode(0)})
    s = await slu_reset_again(dut)
    await s.cfg(rx_cfg)                                            # wrong rate: bits 15 % longer
    sch, t_end = schedule(acq + cltu + acq, per * 1.15 * T_NS)
    ev_rate, _, _ = await receive(dut, s, sch, t_end, stop_after=4)
    await s.cfg({0: mode(0)})
    flags_rate = [bool(e[0] & F_CRCA) for e in ev_rate]
    log.append(f"controls: corrupted parity bit -> CRCA_OK {flags_bad}; wrong pattern -> SOP {sop_pat}, "
               f"{len(ev_pat)} frames; rate +15 % -> CRCA_OK {flags_rate}")
    dut._log.info("O1 CCSDS TC CLTU: " + " | ".join(log))
    assert flags_bad[:3] == [True, False, True], log[-1]
    assert not sop_pat and not ev_pat, log[-1]
    assert not any(flags_rate[:3]), log[-1]


# ------------------------------------------------------------------------------------------------------
# O2: CCSDS Proximity-1 PLTU, Bi-Phase-L
# ------------------------------------------------------------------------------------------------------

@cocotb.test(skip=GL)
async def test_rt1_o2_prox1_pltu(dut):
    rng = random.Random(211)
    half, n_rx = 29, 59
    frame = bytes(rng.randint(0, 255) for _ in range(10))
    pltu = prox_pltu(frame)
    log = []

    # ---- transmit on TXB (the complement output gives Bi-Phase-L), CRC-32 not complemented ----
    s = await slu_reset(dut)
    await s.cfg({0: mode(2, tsel=3), 1: half, 8: NOPIN, 9: 0x0500, 10: 0xA010, 11: 0, 12: 0, 15: 1,
                 EXT1: 1 << 1})
    words, last = pack_words(bits_msb(PROX_PN[:1] + PROX_ASM + frame))   # 8 idle bits, ASM, frame: 2 skipped words
    frames = [dict(words=words, last=last, crc=1, crcw=32 | (2 << 7), bits=32 + 8 * len(frame) + 32)]
    caps, arm = await send_chain(dut, s, frames, 2 * half, 2 * half * (32 + 8 * len(frame) + 32) + 600)
    runs = oe_runs(caps)
    first, length = runs[0]
    nbits = (length + half) // (2 * half)
    first_half = levels_of(caps, first, 2 * half, nbits, 1, half // 2)
    second_half = levels_of(caps, first, 2 * half, nbits, 1, half + half // 2)
    bits = first_half
    good_symbols = sum(1 for a, b in zip(first_half, second_half) if a != b)
    found, got, ok = prox_decode(bits, len(frame))
    log.append(f"TX: {len(runs)} run(s) of {length} cycles, {nbits} symbols, {good_symbols} with two different halves; "
               f"model: ASM found {found}, frame equal {got == frame}, CRC-32 ok {ok}")
    assert found and got == frame and ok and good_symbols == nbits, log[-1]

    # ---- receive the model's PLTU (exact 1,024,000 bit/s), receive line inverted, 59 samples a bit ----
    rx_cfg = {0: mode(2, rx=1, eop_len=1), 1: half, 2: n_rx, 3: 0, 4: 12 | (12 << 6) | (1 << 13), 5: (2 << 6),
              6: 0xF320, 7: 15, 8: DIN | NOPIN, 9: 0x0500, 10: 0xA010, 11: 0, 12: 0, 14: 32,
              EXT0: 8 * len(frame) + 32, EXT1: 1 << 9}
    half_ns = 1e9 / 1024000.0 / 2
    res = {}
    for name, pre in (("PN idle", bits_msb(PROX_PN * 2)), ("PN idle, other phase", [0] + bits_msb(PROX_PN * 2)),
                      ("alternating preamble", [1, 0] * 16)):
        s = await slu_reset_again(dut)
        await s.cfg(rx_cfg)
        stream = pre + pltu + bits_msb(PROX_PN)
        sch, t_end = schedule(biphase_l(stream), half_ns, idle=0)
        events, sop, words = await receive(dut, s, sch, t_end, stop_after=1)
        got = bytes_msb(words_to_bits(words, 8 * len(frame))) if events else None
        res[name] = (sop, len(events), bool(events and events[0][0] & F_CRCA), got == frame)
    log.append("RX (SOP, frames, CRCA_OK, frame equal): " + "; ".join(f"{k}: {v}" for k, v in res.items()))
    assert res["alternating preamble"] == (True, 1, True, True), log[-1]

    # ---- controls: a corrupted CRC bit; a wrong pattern ----
    s = await slu_reset_again(dut)
    await s.cfg(rx_cfg)
    bad = list(pltu)
    bad[-5] ^= 1
    sch, t_end = schedule(biphase_l([1, 0] * 16 + bad + bits_msb(PROX_PN)), half_ns, idle=0)
    ev_bad, _, _ = await receive(dut, s, sch, t_end, stop_after=1)
    s = await slu_reset_again(dut)
    await s.cfg(dict(rx_cfg, **{6: 0xF321}))
    sch, t_end = schedule(biphase_l([1, 0] * 16 + pltu + bits_msb(PROX_PN)), half_ns, idle=0)
    ev_pat, sop_pat, _ = await receive(dut, s, sch, t_end, stop_after=1)
    log.append(f"controls: corrupted CRC bit -> {len(ev_bad)} frame(s), CRCA_OK "
               f"{bool(ev_bad and ev_bad[0][0] & F_CRCA)}; wrong pattern -> SOP {sop_pat}, {len(ev_pat)} frames")
    dut._log.info("O2 CCSDS Proximity-1 PLTU: " + " | ".join(log))
    assert ev_bad and not (ev_bad[0][0] & F_CRCA), log[-1]
    assert not sop_pat and not ev_pat, log[-1]


# ------------------------------------------------------------------------------------------------------
# M: made-up frame parameters (no named protocol)
# ------------------------------------------------------------------------------------------------------

async def madeup_case(dut, s, name, code, per_sym, k, ones, crc_inv, preset_ones, pat, data, rng, log):
    """One made-up frame format both ways. code 0 NRZ (per_sym = bit), 2 Manchester (per_sym = half bit)."""
    width, poly, poly_ref = 12, 0x80F, 0xF01
    nbits = 8 * len(data)
    per_bit = per_sym if code == 0 else 2 * per_sym
    crc_init = 0xFFF if preset_ones else 0
    mode_tx = mode(code, stuff=1, ones=ones, k=k, tsel=3)
    mode_rx = mode(code, rx=1, stuff=1, ones=ones, k=k, eop_len=1)
    ext1 = (0 if crc_inv else 1 << 1)
    # ---- unit transmits, the model decodes ----
    await s.cfg({0: mode_tx, 1: per_sym, 8: NOPIN, 9: poly_ref, 10: 0, 11: 0, 12: 0, 15: 1, EXT1: ext1})
    words, last = pack_words(bits_msb(bytes([0x55, pat]) + data))
    frames = [dict(words=words, last=last, crc=1, crcw=width | (preset_ones << 6) | (1 << 7), bits=16 + nbits + width)]
    ncyc = per_bit * (16 + nbits + width + 8) * 6 // 5 + 400
    caps, _ = await send_chain(dut, s, frames, per_bit, ncyc)
    first, length = oe_runs(caps)[0]
    nsym = (length + per_bit // 2) // per_bit
    if code == 0:
        line = levels_of(caps, first, per_bit, nsym, 0, per_bit // 2)
    else:
        line = levels_of(caps, first, per_bit, nsym, 0, per_sym + per_sym // 2)   # IEEE: the second half is the bit
    dest, serr = destuff(line, k, ones)
    s_line = "".join(map(str, dest))
    p = s_line.find("".join(map(str, bits_msb(bytes([0x55, pat])))))
    body = dest[p + 16:p + 16 + nbits + width] if p >= 0 else []
    crc_exp = crc_bits(crc_msb(bits_msb(data), width, poly, crc_init), width)
    if crc_inv:
        crc_exp = [1 - b for b in crc_exp]
    tx_ok = p >= 0 and body[:nbits] == bits_msb(data) and body[nbits:] == crc_exp and not serr
    log.append(f"{name} TX: {len(line)} line bits, {len(line) - len(dest)} stuff bits, model decode ok {tx_ok}")
    # ---- the model transmits, the unit receives ----
    s = await slu_reset_again(dut)
    res = unit_residue(bits_msb(data) + crc_exp, width, poly_ref, preset_ones)
    rx_cfg = {0: mode_rx, 1: per_sym, 2: per_bit, 3: 0, 4: (per_bit // 3) | ((per_bit // 3) << 6) | (1 << 13),
              5: (2 << 6), 6: pat, 7: 7, 8: DIN | NOPIN, 9: poly_ref, 10: 0, 11: res, 12: 0,
              14: width | (preset_ones << 6), EXT0: nbits + width, EXT1: ext1}
    await s.cfg(rx_cfg)
    payload = bits_msb(bytes([0x55, pat]) + data) + crc_exp
    line_m = stuff(payload, k, ones)
    lv = line_m if code == 0 else manchester_ieee(line_m)
    sch, t_end = schedule([0, 1] * 8 + lv + [1, 0] * 8, per_sym * T_NS)
    events, sop, words = await receive(dut, s, sch, t_end, stop_after=1)
    got = words_to_bits(words, nbits) if events else []
    rx_ok = bool(events) and bool(events[0][0] & F_CRCA) and got == bits_msb(data) and not (events[0][0] & F_STUFF)
    log.append(f"{name} RX: SOP {sop}, {len(events)} frame(s), status {events[0][0]:04x} data equal "
               f"{got == bits_msb(data)}" if events else f"{name} RX: SOP {sop}, no frame")
    # ---- controls: corrupted CRC; wrong stuffing count at the receiver; wrong pattern ----
    ctl = {}
    bad = list(payload)
    bad[-1] ^= 1
    s = await slu_reset_again(dut)
    await s.cfg(rx_cfg)
    lvb = stuff(bad, k, ones)
    sch, t_end = schedule([0, 1] * 8 + (lvb if code == 0 else manchester_ieee(lvb)) + [1, 0] * 8, per_sym * T_NS)
    ev, _, _ = await receive(dut, s, sch, t_end, stop_after=1)
    ctl["corrupted CRC"] = bool(ev) and not (ev[0][0] & F_CRCA)
    s = await slu_reset_again(dut)
    await s.cfg(dict(rx_cfg, **{0: mode(code, rx=1, stuff=1, ones=ones, k=k + 1, eop_len=1)}))
    sch, t_end = schedule([0, 1] * 8 + lv + [1, 0] * 8, per_sym * T_NS)
    ev, _, _ = await receive(dut, s, sch, t_end, stop_after=1)
    ctl["stuff count k+1"] = (not ev) or (not (ev[0][0] & F_CRCA)) or bool(ev[0][0] & F_STUFF)
    s = await slu_reset_again(dut)
    await s.cfg(dict(rx_cfg, **{6: pat ^ 1}))
    sch, t_end = schedule([0, 1] * 8 + lv + [1, 0] * 8, per_sym * T_NS)
    ev, sp, _ = await receive(dut, s, sch, t_end, stop_after=1)
    ctl["wrong pattern"] = (not sp) and (not ev)
    log.append(f"{name} controls rejected: {ctl}")
    return tx_ok, rx_ok, all(ctl.values())


@cocotb.test(skip=GL)
async def test_rt1_m_madeup(dut):
    rng = random.Random(77)
    log = []
    s = await slu_reset(dut)
    # M1: NRZ 24 cycles a bit, stuffing after 5 ones, CRC-12 preset ones complemented, pattern B5, 40 data bits.
    # Data (a) starts with a 0 (the run of ones at the pattern end does not reach the data), (b) starts with
    # five ones right after the pattern: the sender counts the run from the frame start, the unit's receiver
    # from the first data bit (pe_slu.v: rx_run is 0 at rx_sop).
    r = {}
    r["M1a"] = await madeup_case(dut, s, "M1a", 0, 24, 5, 1, True, True, 0xB5, bytes([0x78, 0x1F, 0xFF, 0x00, 0xA5]), rng, log)
    s = await slu_reset_again(dut)
    r["M1b"] = await madeup_case(dut, s, "M1b", 0, 24, 5, 1, True, True, 0xB5, bytes([0xF8, 0x1F, 0xFF, 0x00, 0xA5]), rng, log)
    # M2: Manchester 10 cycles a half bit, stuffing after 4 equal bits, CRC-12 preset 0 not complemented,
    # pattern 9D, 56 data bits.
    s = await slu_reset_again(dut)
    r["M2a"] = await madeup_case(dut, s, "M2a", 2, 10, 4, 0, False, False, 0x9D,
                                 bytes([0x5A, 0xFF, 0x00, 0x0F, 0xF0, 0x33, 0xCC]), rng, log)
    s = await slu_reset_again(dut)
    r["M2b"] = await madeup_case(dut, s, "M2b", 2, 10, 4, 0, False, False, 0x9D,
                                 bytes([0xE5, 0xFF, 0x00, 0x0F, 0xF0, 0x33, 0xCC]), rng, log)
    dut._log.info("made-up formats (TX ok, RX ok, controls rejected): " + "; ".join(f"{k}: {v}" for k, v in r.items())
                  + " | " + " | ".join(log))
    assert r["M1a"] == (True, True, True) and r["M2a"] == (True, True, True), r
