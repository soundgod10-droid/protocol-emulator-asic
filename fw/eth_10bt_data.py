# SPDX-License-Identifier: Apache-2.0
"""Data memory image of fw/eth_10bt.s: variable start values, the housekeeping step table and the
preamble words of the transmit buffer.

The host writes these words (WRITE_MEM) before START. Addresses come from the assembled firmware's
symbols. Timer lengths are in units of 128 clock cycles (2.133 us at 60 MHz), chosen inside the IEEE 802.3
ranges of 14.2.1.7 and 14.2.1.1:

    link pulse period 16 ms (silence 8 to 24 ms)     7,500 units = 16.000 ms
    link_test_min 4 ms (2 to 7 ms)                   1,875 units =  4.000 ms
    link_test_max 64 ms (25 to 150 ms)              30,000 units = 64.000 ms
    link_loss 96 ms (50 to 150 ms)                  45,000 units = 96.000 ms
    lc_max 4 (2 to 10)

scale > 1 divides the four timer lengths (not lc_max, the interframe gap or the backoff slot), so that a
simulation can run the link state machine in less time; the firmware is the same.
"""

UNIT_CYCLES = 128
CLOCK_HZ = 60e6
TIMERS_MS = {"C_NLP": 16.0, "C_LMIN": 4.0, "C_LMAX": 64.0, "C_LLOSS": 96.0}
LC_MAX = 4
PREAMBLE = [0x5555, 0x5555, 0x5555, 0xD555]   # 7 octets 0x55 and the SFD 0xD5, first bit = bit 0


def units(ms, scale=1):
    return int(round(ms * 1e-3 * CLOCK_HZ / UNIT_CYCLES / scale))


def build(symbols, scale=1, seed=0xACE1):
    """symbols: name -> value of the assembled fw/eth_10bt.s ((value, kind) pairs are accepted).
    Returns (mem, info): mem = [(address, words)] for WRITE_MEM; info = timer lengths in units and in
    cycles, buffer sizes."""
    s = {k: (v[0] if isinstance(v, tuple) else v) for k, v in symbols.items()}
    t = {name: units(ms, scale) for name, ms in TIMERS_MS.items()}
    assert all(0 < v < 0x10000 for v in t.values())
    rxend = 1536
    txend = s["TXBUF"] + 4 + 204
    init = {
        "V_TL": 0, "V_PEND": 0, "V_TNLP": t["C_NLP"], "V_TMIN": t["C_LMIN"], "V_TMAX": t["C_LMAX"],
        "V_TLOSS": t["C_LLOSS"], "V_TBO": 0,
        "V_LINK": 0,                         # power on: LINK TEST PASS (Figure 14-6)
        "V_CNT": 0, "V_TXS": 0, "V_TXN": 0, "V_TXC": 0, "V_ATT": 0, "V_MASK": 0,
        "V_RND": seed & 0xFFFF or 1, "V_IDLEAT": 0, "V_TMP": 0, "V_PH": 0, "V_UPN": 0, "V_UPP": 0,
        "V_HLN": 0, "V_HLP": 0, "V_HLO": 0, "V_EVQ": 0, "V_EVR": 0,
        "C_NLP": t["C_NLP"], "C_LMIN": t["C_LMIN"], "C_LMAX": t["C_LMAX"], "C_LLOSS": t["C_LLOSS"],
        "C_LCMAX": LC_MAX,
        "C_POLY": 0xB400,                    # x^16 + x^14 + x^13 + x^11 + 1 (maximal length)
        "C_RXEND": rxend, "C_LPBIT": 1 << 13,
        "C_RXCLR": (1 << 4) | (1 << 5) | (1 << 9) | (1 << 13),
        "C_TXCLR": 1 | (1 << 1) | (1 << 2) | (1 << 14),
        "C_TXEND": txend, "C_TXMAXO": 2 * 204,
    }
    base = s["V_BASE"]
    assert len(init) == 512 - base, "%d start values for %d variables" % (len(init), 512 - base)
    by_addr = {s[name]: v for name, v in init.items()}
    assert sorted(by_addr) == list(range(base, 512)), "start values do not cover V_BASE..511"
    assert s["V_TBO"] == s["V_TNLP"] + 4
    pht = [s[n] for n in ("phA", "phB", "phC", "phD", "phE", "phF")]
    assert txend <= s["RXBUF"] - 1 and s["INIT"] + len(init) <= s["PHT"] and s["PHT"] + 6 <= s["TXBUF"]
    mem = [
        (s["INIT"], [by_addr[a] for a in range(base, 512)]),
        (s["PHT"], pht),
        (s["TXBUF"], PREAMBLE),
    ]
    info = {"timers_units": t, "timers_cycles": {k: v * UNIT_CYCLES for k, v in t.items()},
            "lc_max": LC_MAX, "tx_max_octets": 2 * 204, "rx_max_words": rxend - s["RXBUF"], "scale": scale}
    return mem, info
