# SPDX-License-Identifier: Apache-2.0
"""Port-level cycle model of the two line helpers (src/pe_slu.v SPEC 1 and SPEC 2, src/pe_slu_loop.v).

What it does:
  SluModel is one helper as pe_proc.v connects it: the thread port (IOR/IOW of the helper's ports 0 to
  15), the event output, the DIN and AUX inputs and the transmit taps tx_v / tx_oe. clock() is one rising
  clock edge: every register of pe_slu.v and of its receive loop gets the value that the RTL gives it.
  Every right-hand side is the value before the edge (dictionary `o`); where the RTL's always block
  assigns a register twice, the later assignment is the one kept, as in the RTL.
  HelperChip is the reference model (model/pe_model.py) with the helpers on its io hook, wired as
  pe_proc.v wires them: ports 16 to 31 to the USB helper, 32 to 47 to the 10BASE-Te helper, the events to
  inputs 13 and 14, and each helper pin through one more flip-flop (OE = 1 while the threads run).

Why:
  The model test benches and the browser simulator run the reference model, which has the io hook but no
  helper behind it. This file adds the helpers without a change to the model, the RTL or the firmware.
  test/test_helpers_rtl.py runs the RTL and HelperChip in lockstep and compares the pins and the helper
  registers in every clock cycle.

Conditions:
  The thread port is read and written in the slot cycle of the IOR/IOW (pe_core.v): read() gives the
  register values before the edge of that cycle and write() acts at that edge. A per-cycle input value is
  taken to change at the falling clock edge before the rising edge that samples it (as the RTL lockstep
  bench drives the pins): the rising-edge DIN sample of cycle c is the level of cycle c, the falling-edge
  sample the level of cycle c - 1. The helpers leave reset 3 cycles before cycle 0 (project.v: two
  prestart cycles and the START cycle); these cycles run with the counter values `prestart_now`.
  A helper that the example does not use can be left out (HelperChip helpers=): it is then only allowed
  to stay untouched, because an untouched helper has no effect on the chip (its event mask is 0 after
  reset, so its event is 0, and no pin selects it); a port access to it stops the run with an error.
"""

import pe_model

SPEC_USB, SPEC_ETH = 1, 2

# Fixed configuration of the dedicated forms (pe_slu.v F_*): (SPEC 1 USB, SPEC 2 10BASE-Te).
F = {
    "mode": (0x03B9, 0x040E), "txper": (40, 3), "loop0": (0x0028, 0x688C), "loop1": (0x0000, 0xA608),
    "loop2": (0x4514, 0x5143), "loop3": (0x0080, 0x0418), "pat": (0x0001, 0x00AB), "pat2": (0x5002, 0x2807),
    "pa": (0x0000A001, 0xEDB88320), "ra": (0x0000B001, 0xDEBB20E3), "crcb": (0x0614, 0x0000),
    "crcw": (0x20D0, 0x0260), "tail": (0x2854, 0x0010), "lta": (0x0013, 0x0000), "ltb": (0x3A98, 0x0000),
    "ltc": (0x0044, 0x0000), "sq": (0x0000, 0x4403), "lp": (0x0000, 0x6302),
}

R_OFF, R_HUNT, R_DATA, R_EOPW, R_RST = 0, 1, 2, 3, 4
T_IDLE, T_ARM, T_DATA, T_CRC, T_TAIL1, T_TAIL2, T_PULSE, T_JAM = 0, 1, 2, 3, 4, 5, 6, 7
K_NONE, K_STUFF, K_DATA, K_LOAD, K_CRC, K_JAM, K_END, K_UNDER = 0, 1, 2, 3, 4, 5, 6, 7
C_NRZ, C_NRZI, C_MAN, C_CLK = 0, 1, 2, 3
M21 = (1 << 21) - 1

# Register names of pe_slu.v (and u_loop) that the RTL lockstep test compares in every cycle.
COMPARED = ("c_mode", "c_txper", "c_sel", "c_ltc", "cfg_adr", "evmask", "sticky", "tstamp", "din_prev", "q_cnt",
            "carrier", "sq_gap", "sq_good", "lp_w", "lt_a", "lt_b", "lp_en", "rx_st", "rx_lvl_prev", "pat_sh",
            "rx_run", "rx_lastbit", "rx_sh", "rx_nb", "rx_words", "rx_bits", "rx_bph", "rx_buf0", "rx_buf1",
            "rx_cnt", "rx_lastn", "crc_a", "crc_b", "crc_a_ok", "crc_b_ok", "ck_pend", "tx_st", "tx_buf0",
            "tx_buf1", "tx_cnt", "tx_sh", "tx_nb", "tx_lastw", "tx_tmr", "tx_half", "tx_bit", "tx_line",
            "tx_run", "tx_crcn", "tx_tail", "tx_jam", "tx_time", "crc_t", "jab_cnt", "jabber", "tx_v", "tx_oe")
LOOP_COMPARED = (("th", "lp_th"), ("ii", "lp_ii"), ("nacc_q", "lp_nacc"), ("init", "lp_init"),
                 ("bit_v", "lp_bv"))


def _s21(v):
    """21-bit two's complement wrap (the arithmetic of pe_slu_loop_step)."""
    v &= M21
    return v - (1 << 21) if v >> 20 else v


def _rsra(v, s):
    """(v + 2^(s-1)) >>> s for s > 0, v for s = 0 (f_rsra of pe_slu_loop_step)."""
    if s == 0:
        return v
    return _s21(v + (1 << (s - 1))) >> s


def _crc_step(c, b, poly):
    return (c >> 1) ^ (poly if (c ^ b) & 1 else 0)


def _loop_step(x, xprev, allow, th, ii, nacc, nedge, init, decided, k):
    """pe_slu_loop_step. k = (none, half, mid, tdec, kb, win, wina, acq_n, gn0, gn1, kp0, kp1, kp2, ki_en,
    ki_sh, init_edge). Returns (th, ii, nacc, nedge, init, decided, bit_v, bit_d)."""
    none, half, mid, tdec, kb, win, wina, acq_n, gn0, gn1, kp0, kp1, kp2, ki_en, ki_sh, init_edge = k
    t = _s21(th + 4096)
    ii_o, nacc_o, nedge_o, init_o, dec_o, bit_v = ii, nacc, nedge, init, decided, 0
    xe = allow and x != xprev
    if xe and not init:
        ne = 3 if nedge == 3 else nedge + 1
        nedge_o = ne
        if ne >= init_edge:
            t = _s21(mid + 2048)
            init_o, dec_o, nacc_o = 1, 0, 0
    if xe and init:
        a = _s21(t + kb)
        if a < -half:
            eps = _s21(a + none)
        elif a >= half:
            eps = _s21(a - none)
        else:
            eps = a
        w = wina if (acq_n != 0 and nacc < acq_n) else win
        if -w <= eps <= w:
            hk = 0
            if nacc < gn0:
                kp = kp0
            elif nacc < gn1:
                kp = kp1
            else:
                kp, hk = kp2, ki_en
            t = _s21(t - _rsra(eps, kp))
            if hk:
                inew = ii - _rsra(eps, ki_sh)
                ii_o = 2047 if inew > 2047 else (-2047 if inew < -2047 else inew)
            nacc_o = 63 if nacc == 63 else nacc + 1
    if t >= none:
        t = _s21(t - none + (ii_o >> 4))
        dec_o = 0
    if init_o and not dec_o and t >= tdec:
        bit_v, dec_o = 1, 1
    th_o = t & 0xFFFFF
    if th_o >> 19:
        th_o -= 1 << 20
    return th_o, ii_o, nacc_o, nedge_o, init_o, dec_o, bit_v, x


class SluModel:
    """One line helper (pe_slu SPEC 1: USB low speed, FULL 0; SPEC 2: 10BASE-Te, FULL 1)."""

    def __init__(self, spec):
        assert spec in (SPEC_USB, SPEC_ETH)
        self.spec = spec
        self.full = 1 if spec == SPEC_ETH else 0
        self.f = {k: v[spec - 1] for k, v in F.items()}
        self.reset()

    def reset(self):
        """Register values after the processor reset (pe_slu.v reset branch)."""
        f = self.f
        self.c_mode, self.c_txper, self.c_loop0, self.c_loop1 = f["mode"], f["txper"], f["loop0"], f["loop1"]
        self.c_loop2, self.c_loop3, self.c_pat, self.c_pat2 = f["loop2"], f["loop3"], f["pat"], f["pat2"]
        self.c_sel, self.c_pa, self.c_ra, self.c_crcb = 0xFFFF, f["pa"], f["ra"], f["crcb"]
        self.c_crcw, self.c_tail, self.c_lta, self.c_ltb = f["crcw"], f["tail"], f["lta"], f["ltb"]
        self.c_ltc, self.c_sq, self.c_lp, self.cfg_adr = f["ltc"], f["sq"], f["lp"], 0
        self.evmask = self.sticky = self.tstamp = 0
        self.din_f2 = self.din_f3 = self.din_r1 = self.din_r2 = 0
        self.din_prev = self.q_cnt = self.carrier = self.sq_gap = self.sq_good = self.lp_w = 0
        self.lt_a = self.lt_b = self.lt_a_hit = self.lt_b_hit = 0
        self.lp_en, self.rx_st, self.rx_lvl_prev, self.pat_sh = 0, R_OFF, 1, 0
        self.rx_run = self.rx_lastbit = self.rx_sh = self.rx_nb = self.rx_words = self.rx_bits = self.rx_bph = 0
        self.rx_buf0 = self.rx_buf1 = self.rx_cnt = self.rx_lastn = 0
        self.crc_a = self.crc_b = self.crc_a_ok = self.crc_b_ok = self.crc_a_byte = self.crc_b_byte = 0
        self.ck_pend = self.ck_bit_v = self.ck_bit_d = self.ck_frame = 0
        self.tx_st, self.tx_buf0, self.tx_buf1, self.tx_lst0, self.tx_lst1, self.tx_cnt = T_IDLE, 0, 0, 0, 0, 0
        self.tx_sh = self.tx_nb = self.tx_lastw = self.tx_tmr = self.tx_half = self.tx_bit = 0
        self.tx_line, self.tx_sck, self.tx_run, self.tx_runbit = 1, 0, 0, 0
        self.tx_crcn = self.tx_skipw = self.tx_tail = self.tx_jam = self.tx_time = self.tx_lastbits = 0
        self.tx_crcen = self.tx_droplate = self.tx_pulse = self.crc_t = self.tx_lead = 0
        self.jab_cnt = self.jabber = 0
        self.tx_line_q, self.tx_v, self.tx_oe = 1, 0, 0
        self.lp_th = self.lp_ii = self.lp_nacc = self.lp_nedge = self.lp_init = self.lp_dec = 0
        self.lp_xprev = self.lp_bv = self.lp_bd = 0
        # Inputs of the cycle in progress (set by the chip before the slot) and the port operation of the slot.
        self.level = 0          # pin_raw: pad levels of pins 0 to 12 in this cycle
        self.level_prev = 0     # pad levels of the cycle before (the falling-edge DIN sample)
        self.sync = 0           # pin_sync: s(c) of inputs 0 to 12
        self.op = None          # (write, helper port, value)

    # ----- thread port (pe_core IOR / IOW in the slot cycle) -----

    def _aux(self):
        s = self.c_sel
        a0, a1 = (s >> 4) & 15, (s >> 8) & 15
        return ((self.sync >> a0) & 1 if a0 < 13 else 0), ((self.sync >> a1) & 1 if a1 < 13 else 0)

    def flags(self):
        st = self.sticky
        return ((self.full & self.jabber) << 15) | (st & 0x6000) | ((self.full & self.carrier) << 12) | \
            (st & 0x0F00) | (self.crc_b_ok << 7) | (self.crc_a_ok << 6) | (st & 0x30) | \
            ((1 if self.rx_cnt else 0) << 3) | (st & 7)

    def read(self, a):
        """IOR of helper port a (0 to 15): p_rdata before the edge; a read of RXDATA pops at the edge."""
        self.op = (False, a, 0)
        if a == 0:
            return (2 - self.tx_cnt) & 3
        if a == 1:
            return self.rx_buf0 if self.rx_cnt else 0
        if a == 2:
            return self.flags()
        if a == 3:
            return self.evmask
        if a == 4:
            return self.tx_st
        if a == 5:
            return self.tx_time
        if a == 6:
            return (self.rx_words << 7) | (self.rx_cnt << 5) | self.rx_lastn
        if a == 7:
            return self.crc_a & 0xFFFF
        if a == 8:
            return self.crc_a >> 16
        if a == 9:
            return self.crc_b
        if a == 10:
            return self.tstamp
        if a == 11:
            aux0, aux1 = self._aux()
            x1 = self.tx_line_q if (self.c_mode >> 15) & 1 else self.din_r2
            return (self.rx_st << 4) | ((self.full & self.carrier) << 3) | (x1 << 2) | (aux1 << 1) | aux0
        return 0

    def write(self, a, v):
        """IOW of helper port a: acts at the edge of this cycle."""
        self.op = (True, a, v & 0xFFFF)

    def ev(self):
        """Event output of this cycle: any flag AND its mask."""
        return 1 if self.flags() & self.evmask else 0

    # ----- one rising clock edge -----

    def clock(self, now):
        """The rising edge at the end of the cycle whose counter value is `now` (16 bits), with self.level,
        self.level_prev, self.sync and self.op of that cycle."""
        o = dict(self.__dict__)            # values before the edge
        full, spec = self.full, self.spec
        op = o["op"]
        self.op = None
        p_we = op is not None and op[0]
        p_re = op is not None and not op[0]
        p_addr = op[1] if op is not None else 0
        wd = op[2] if op is not None else 0

        c_mode, c_sel, c_ltc, c_pat2, c_crcw = o["c_mode"], o["c_sel"], o["c_ltc"], o["c_pat2"], o["c_crcw"]
        c_txper, cb = o["c_txper"], o["c_crcb"]
        m_code = c_mode & 3
        m_ddr = (c_mode >> 2) & 1
        m_rx = (c_mode >> 3) & 1
        m_stuff = (c_mode >> 4) & 1
        m_ones = (c_mode >> 5) & 1
        m_k = (c_mode >> 6) & 7
        m_usb = (c_mode >> 9) & 1
        m_eop_car = (c_mode >> 10) & 1
        m_eop_len = (c_mode >> 11) & 1
        m_tsel = (c_mode >> 12) & 3
        m_loop = (c_mode >> 15) & 1
        m_half = m_code == C_MAN or m_code == C_CLK
        ltc8, ltc9 = (c_ltc >> 8) & 1, (c_ltc >> 9) & 1

        sd = c_sel & 15
        din_pin = (o["level"] >> sd) & 1 if sd < 13 else 0
        din_pin_f = (o["level_prev"] >> sd) & 1 if sd < 13 else 0
        aux0, aux1 = self._aux()
        x0 = o["tx_line_q"] if m_loop else o["din_f3"]
        x1 = o["tx_line_q"] if m_loop else o["din_r2"]
        lp_bv, lp_bd, lp_nacc, lp_init = o["lp_bv"], o["lp_bd"], o["lp_nacc"], o["lp_init"]

        # line conditions
        se0 = (not aux0) and (not aux1)
        lstate = (aux0 << 1) | aux1
        din_prev = o["din_prev"]
        din_edge = ((x0 ^ din_prev) | (x1 ^ x0)) if m_ddr else (x1 ^ din_prev)
        q_cnt = o["q_cnt"]
        eop_q = q_cnt >= (c_pat2 >> 10)
        lt_tick = (now & (((1 << (c_ltc & 15)) - 1) & 0xFFFF)) == 0
        holds = []
        for cnd in ((c_ltc >> 4) & 3, (c_ltc >> 6) & 3):
            holds.append(lstate == cnd if cnd < 3 else not din_edge)
        ca_hold, cb_hold = holds
        ca_now = ca_hold and o["c_lta"] != 0 and o["lt_a"] >= o["c_lta"]
        cb_now = cb_hold and o["c_ltb"] != 0 and o["lt_b"] >= o["c_ltb"]

        # CRC
        crc_w = c_crcw & 63
        crc_mask = 0xFFFFFFFF if crc_w >= 32 else (1 << crc_w) - 1
        crc_init = crc_mask if (c_crcw >> 6) & 1 else 0
        crcb_mask = 0xFF if cb & 0x80 else 0x7F if cb & 0x40 else 0x3F if cb & 0x20 else 0x1F if cb & 0x10 else \
            0x0F if cb & 0x08 else 0x07 if cb & 0x04 else 0x03

        # transmit, combinational
        tx_st, tx_cnt, tx_tmr = o["tx_st"], o["tx_cnt"], o["tx_tmr"]
        tx_busy = tx_st == T_DATA or tx_st == T_CRC or tx_st == T_JAM
        sym_end = tx_tmr == 0
        bit_start = tx_busy and sym_end and not (m_half and o["tx_half"])
        half_mid = tx_busy and sym_end and m_half and o["tx_half"]
        tx_go = tx_st == T_ARM and now == ((o["tx_time"] - 3) & 0xFFFF)
        jab = full and o["jabber"]
        ck_eop = o["ck_frame"] and m_code == C_CLK and not tx_busy and tx_st != T_ARM and o["ck_pend"] == 0 \
            and not o["ck_bit_v"]

        # receive step, combinational
        rx_st = o["rx_st"]
        r_dec_v = r_dec = r_sop = r_eop = r_data_v = r_data = r_stuff_err = r_stuff_drop = 0
        if m_code == C_CLK:
            r_dec_v, r_dec = o["ck_bit_v"], o["ck_bit_d"]
        elif lp_bv and not (m_usb and se0):
            r_dec_v = 1
            r_dec = (1 if lp_bd == o["rx_lvl_prev"] else 0) if m_code == C_NRZI else lp_bd
        if rx_st == R_HUNT and r_dec_v and m_code != C_CLK:
            pl = c_pat2 & 15
            pmask = 0xFFFF if pl == 15 else (1 << (pl + 1)) - 1
            r_sop = 1 if (lp_nacc >= ((o["c_loop3"] >> 6) & 63) and
                          (((((o["pat_sh"] & 0x7FFF) << 1) | r_dec) ^ o["c_pat"]) & pmask) == 0) else 0
        rx_bits, rx_nb, rx_run, rx_lastbit = o["rx_bits"], o["rx_nb"], o["rx_run"], o["rx_lastbit"]
        if rx_st == R_DATA:
            r_eop = 1 if ((m_usb and eop_q) or (not m_usb and m_eop_car and eop_q) or
                          (m_eop_len and rx_bits >= ((c_pat2 >> 4) & 63)) or ck_eop) else 0
            if r_dec_v and not r_eop:
                if m_stuff and rx_run == m_k:
                    r_stuff_drop = 1
                    r_stuff_err = r_dec if m_ones else (1 if r_dec == rx_lastbit else 0)
                else:
                    r_data_v, r_data = 1, r_dec
        rx_sh, rx_cnt = o["rx_sh"], o["rx_cnt"]
        rx_word_full = r_data_v and rx_nb == 15
        rx_word = (r_data << 15) | (rx_sh >> 1)
        rx_ps = 16 - rx_nb
        rx_part = 0 if rx_ps >= 16 else rx_sh >> rx_ps
        tx_lastbits = o["tx_lastbits"]
        ck_mode = m_code == C_CLK and m_rx
        m_estart = (c_ltc >> 10) & 1
        ck_len = 16 if tx_lastbits == 0 else tx_lastbits
        ck_lastb = ck_mode and m_estart and r_data_v and (((rx_bits & 31) + 1) & 31) == ck_len
        rx_push = rx_word_full or ck_lastb or (r_eop and rx_nb != 0)
        rx_push_w = rx_word if (rx_word_full or ck_lastb) else rx_part
        rx_pop = p_re and p_addr == 1 and rx_cnt != 0

        # transmit: next bit at a bit start
        tx_run, tx_runbit, tx_nb, tx_sh, tx_bit = o["tx_run"], o["tx_runbit"], o["tx_nb"], o["tx_sh"], o["tx_bit"]
        tx_lst0, tx_buf0, crc_t, tx_crcn = o["tx_lst0"], o["tx_buf0"], o["crc_t"], o["tx_crcn"]
        tx_lastw, tx_skipw, tx_line = o["tx_lastw"], o["tx_skipw"], o["tx_line"]
        need_stuff = m_stuff and tx_run == m_k
        stuff_bit = 0 if m_ones else 1 - tx_runbit
        len0 = tx_lastbits if (tx_lst0 and tx_lastbits != 0) else 16
        t_kind, t_bit = K_NONE, tx_bit
        if bit_start:
            if tx_st == T_JAM:
                t_kind, t_bit = (K_END if o["tx_jam"] == 0 else K_JAM), 1 - tx_bit
            elif need_stuff:
                t_kind, t_bit = K_STUFF, stuff_bit
            elif tx_st == T_CRC:
                t_kind, t_bit = (K_END if tx_crcn == 0 else K_CRC), 1 - (crc_t & 1)
            elif tx_nb != 0:
                t_kind, t_bit = K_DATA, tx_sh & 1
            elif not tx_lastw:
                t_kind, t_bit = (K_LOAD if tx_cnt != 0 else K_UNDER), tx_buf0 & 1
            elif o["tx_crcen"] and crc_w != 0:
                t_kind, t_bit = K_CRC, 1 - (crc_t & 1)
            else:
                t_kind = K_END
        t_new = t_kind in (K_STUFF, K_DATA, K_LOAD, K_CRC, K_JAM)
        tx_take_go = tx_go and not o["tx_pulse"] and not jab and tx_cnt != 0
        tx_take = tx_take_go or t_kind == K_LOAD
        tx_wr = p_we and (p_addr == 0 or p_addr == 14)
        lead = (wd - now) & 0xFFFF
        ck_estart = ck_mode and m_estart and tx_st == T_IDLE and din_prev and not x1
        ck_sop = ck_mode and not m_estart and tx_take_go
        # a start edge while a sample of the previous edge-started frame is on its way: the count starts at that
        # frame's end (ck_lastb), not at the edge
        ck_rxbusy = o["ck_pend"] != 0 or o["ck_bit_v"]
        rx_sop = r_sop or ck_sop or (ck_estart and not ck_rxbusy)
        ck_eend = ck_mode and m_estart and half_mid and tx_nb == 0 and tx_lastw

        sticky = o["sticky"]
        nst = sticky

        # ---------------- configuration and control ----------------
        if p_we and p_addr == 12:
            self.cfg_adr = wd & 31
        if p_we and p_addr == 13:
            ca = o["cfg_adr"]
            if ca == 8:
                self.c_sel = wd
            if spec == SPEC_USB:
                if ca == 0:
                    self.c_mode = (self.f["mode"] & ~0x021B) | (wd & 0x021B)
                elif ca == 1:
                    self.c_txper = wd & 0xFF
                elif ca == 18:
                    self.c_ltc = (self.f["ltc"] & ~0x1F00) | (wd & 0x1F00)
            self.cfg_adr = (ca + 1) & 31
        if p_we and p_addr == 3:
            self.evmask = wd

        # ---------------- line conditions ----------------
        self.din_prev = x1
        if m_usb:
            self.q_cnt = (63 if q_cnt == 63 else q_cnt + 1) if se0 else 0
        else:
            self.q_cnt = 0 if din_edge else (63 if q_cnt == 63 else q_cnt + 1)
        lt_a, lt_b = o["lt_a"], o["lt_b"]
        self.lt_a = 0 if not ca_hold else ((lt_a + 1) if (lt_tick and lt_a != 0xFFFF) else lt_a)
        self.lt_b = 0 if not cb_hold else ((lt_b + 1) if (lt_tick and lt_b != 0xFFFF) else lt_b)
        self.lt_a_hit, self.lt_b_hit = 1 if ca_now else 0, 1 if cb_now else 0
        carrier, sq_gap, sq_good, lp_w = o["carrier"], o["sq_gap"], o["sq_good"], o["lp_w"]
        if full:
            c_sq = o["c_sq"]
            if din_edge:
                if aux0 and (c_sq & 63) <= sq_gap <= ((c_sq >> 6) & 63):
                    self.sq_good = 15 if sq_good == 15 else sq_good + 1
                else:
                    self.sq_good = 0
                self.sq_gap = 1 if (m_ddr and (x1 ^ x0)) else 2
            else:
                self.sq_gap = 127 if sq_gap >= 126 else sq_gap + 2
            if not carrier and (c_sq >> 12) != 0 and sq_good >= (c_sq >> 12):
                self.carrier = 1
            elif carrier and eop_q:
                self.carrier = 0
                self.sq_good = 0
            self.lp_w = (63 if lp_w == 63 else lp_w + 1) if (aux0 and x1) else 0

        # ---------------- receive ----------------
        self.lp_en = 1 if (m_rx and m_code != C_CLK and (rx_st == R_HUNT or rx_st == R_DATA)) else 0
        if r_dec_v:
            self.rx_lvl_prev = lp_bd
        if rx_st == R_HUNT and not lp_init:
            self.rx_lvl_prev = x1
        if rx_st == R_OFF:
            if m_rx:
                self.rx_st = R_DATA if m_code == C_CLK else R_HUNT
        elif rx_st == R_HUNT:
            if not m_rx:
                self.rx_st = R_OFF
            elif r_dec_v:
                self.pat_sh = ((o["pat_sh"] & 0x7FFF) << 1) | r_dec
            if m_rx and (r_sop or m_code == C_CLK):
                self.rx_st = R_DATA
        elif rx_st == R_DATA:
            if not m_rx:
                self.rx_st = R_OFF
            elif r_eop:
                self.rx_st = R_EOPW if m_usb else (R_DATA if m_code == C_CLK else R_RST)
        elif rx_st == R_EOPW:
            if not se0:
                self.rx_st = R_RST
        elif rx_st == R_RST:
            self.rx_st = R_HUNT if m_rx else R_OFF
        else:
            self.rx_st = R_OFF
        if rx_sop:
            self.rx_run, self.rx_lastbit, self.rx_sh, self.rx_nb = 0, r_dec, 0, 0
            self.rx_words, self.rx_bits, self.rx_bph = 0, 0, 0
            self.crc_a, self.crc_b, self.crc_a_byte, self.crc_b_byte = crc_init, crcb_mask, 0, 0
        if r_stuff_drop:
            self.rx_run = 0 if m_ones else 1
            self.rx_lastbit = r_dec
        crc_a, crc_b, rx_bph = o["crc_a"], o["crc_b"], o["rx_bph"]
        if r_data_v:
            if m_ones:
                self.rx_run = ((rx_run + 1) & 7) if r_data else 0
            else:
                self.rx_run = ((rx_run + 1) & 7) if r_data == rx_lastbit else 1
            self.rx_lastbit = r_data
            if rx_bits >= ((c_crcw >> 10) & 63):
                self.crc_a = _crc_step(crc_a, r_data, o["c_pa"]) & crc_mask
                self.crc_b = _crc_step(crc_b, r_data, cb & 0xFF) & 0xFF & crcb_mask
            self.rx_bits = rx_bits if rx_bits == 0x3FF else rx_bits + 1
            self.rx_bph = (rx_bph + 1) & 7
            self.rx_sh = rx_word
            self.rx_nb = 0 if rx_nb == 15 else rx_nb + 1
            if rx_nb == 15:
                self.rx_words = (o["rx_words"] + 1) & 0x1FF
        if rx_st == R_DATA and not r_data_v and rx_bits != 0 and rx_bph == 0:
            self.crc_a_byte = 1 if ((crc_a ^ o["c_ra"]) & crc_mask) == 0 else 0
            self.crc_b_byte = 1 if crc_b == (cb >> 8) else 0
        if r_eop:
            self.rx_lastn = rx_nb
            self.crc_a_ok, self.crc_b_ok = o["crc_a_byte"], o["crc_b_byte"]
            self.rx_nb = 0
        if ck_lastb:
            self.rx_lastn = (rx_nb + 1) & 31
            self.rx_nb = 0
            self.rx_bits = 0
            self.rx_sh = 0
            nst |= 1 << 5
        if rx_pop:
            self.rx_buf0 = o["rx_buf1"]
        if rx_push:
            if rx_cnt == 2 and not rx_pop:
                nst |= 1 << 9
            elif rx_cnt == 0 or (rx_cnt == 1 and rx_pop):
                self.rx_buf0 = rx_push_w
            else:
                self.rx_buf1 = rx_push_w
        self.rx_cnt = rx_cnt - (1 if rx_pop else 0) + (1 if (rx_push and not (rx_cnt == 2 and not rx_pop)) else 0)
        ck_pend = o["ck_pend"] >> 1
        if half_mid and m_code == C_CLK and m_rx:
            ck_pend |= 1 << ((c_sel >> 12) & 15)
        self.ck_pend = ck_pend
        self.ck_bit_v = o["ck_pend"] & 1
        self.ck_bit_d = x0 if (c_ltc >> 12) & 1 else x1
        if m_code != C_CLK:
            self.ck_frame = 0
        elif ck_sop:
            self.ck_frame = 1
        elif ck_eop:
            self.ck_frame = 0

        # ---------------- transmit ----------------
        n_tmr = tx_tmr
        if tx_busy or tx_st == T_TAIL1:
            n_tmr = ((c_txper - 1) if sym_end else (tx_tmr - 1)) & 0xFFFF
        if p_we and p_addr == 4:
            self.tx_lastbits, self.tx_crcen = wd & 31, (wd >> 5) & 1
            self.tx_droplate, self.tx_pulse = (wd >> 6) & 1, (wd >> 7) & 1
        n_st = tx_st
        if tx_st == T_IDLE:
            self.tx_half, self.tx_line, self.tx_sck = 0, 1, ltc8
            if ck_estart:
                n_st = T_DATA
                self.tx_half = 1
                n_tmr = (c_txper - 2) if c_txper >= 2 else 0
                self.tx_nb = (ck_len - 1) & 31
                self.tx_lastw = 1
            elif p_we and p_addr == 4 and (wd >> 9) & 1:
                n_st = T_ARM
                self.tx_time = (now + 4) & 0xFFFF
            elif p_we and p_addr == 5:
                if 4 <= lead <= 32767:
                    n_st = T_ARM
                    self.tx_time = wd
                else:
                    nst |= 1 << 1
                    if not o["tx_droplate"]:
                        n_st = T_ARM
                        self.tx_time = (now + 4) & 0xFFFF
        elif tx_st == T_ARM:
            if tx_go:
                n_tmr = 0
                self.tx_half = 0
                if jab:
                    n_st = T_IDLE
                elif o["tx_pulse"]:
                    n_st = T_PULSE
                    self.tx_tail = (o["c_lp"] >> 12) & 15
                elif tx_cnt == 0:
                    nst |= 1 << 2
                    n_st = T_IDLE
                else:
                    n_st = T_DATA
                    self.tx_sh, self.tx_lastw, self.tx_nb = tx_buf0, tx_lst0, len0
                    self.tx_run, self.tx_runbit = 0, 0
                    self.tx_skipw = (c_crcw >> 7) & 7
                    self.crc_t = crc_init
                    self.tx_crcn = crc_w
        elif tx_busy:
            if half_mid:
                self.tx_half = 0
                if m_code == C_MAN:
                    self.tx_line = tx_bit
                self.tx_sck = ltc8 ^ (1 - ltc9)
            if bit_start:
                if t_new:
                    self.tx_bit = t_bit
                    self.tx_half = 1 if m_half else 0
                    if m_code == C_NRZI:
                        self.tx_line = tx_line if t_bit else 1 - tx_line
                    elif m_code == C_MAN:
                        self.tx_line = 1 - t_bit
                    else:
                        self.tx_line = t_bit
                    self.tx_sck = ltc8 ^ ltc9
                    if t_kind != K_JAM:
                        if t_kind == K_STUFF:
                            self.tx_run = 0 if m_ones else 1
                        elif m_ones:
                            self.tx_run = ((tx_run + 1) & 7) if t_bit else 0
                        else:
                            self.tx_run = ((tx_run + 1) & 7) if t_bit == tx_runbit else 1
                        self.tx_runbit = t_bit
                if t_kind == K_DATA:
                    self.tx_sh = tx_sh >> 1
                    self.tx_nb = (tx_nb - 1) & 31
                elif t_kind == K_LOAD:
                    self.tx_sh = tx_buf0 >> 1
                    self.tx_nb = (len0 - 1) & 31
                    self.tx_lastw = tx_lst0
                    if tx_skipw != 0:
                        self.tx_skipw = tx_skipw - 1
                elif t_kind == K_CRC:
                    n_st = T_CRC
                    self.crc_t = crc_t >> 1
                    self.tx_crcn = (tx_crcn - 1) & 63
                elif t_kind == K_JAM:
                    self.tx_jam = (o["tx_jam"] - 1) & 63
                elif t_kind == K_UNDER:
                    nst |= 1 << 2
                    n_st = T_TAIL1
                    self.tx_tail = o["c_tail"] & 0xFF
                elif t_kind == K_END:
                    n_st = T_TAIL1
                    self.tx_tail = o["c_tail"] & 0xFF
                if o["tx_crcen"] and ((t_kind == K_DATA and tx_skipw == 0) or (t_kind == K_LOAD and tx_skipw <= 1)):
                    self.crc_t = _crc_step(crc_t, t_bit, o["c_pa"]) & crc_mask
            if full and carrier and (tx_st == T_DATA or tx_st == T_CRC):
                nst |= 1 << 14
                n_st = T_JAM
                self.tx_jam = 32
            if jab:
                n_st = T_IDLE
            if ck_eend:
                n_st = T_IDLE
        elif tx_st == T_TAIL1:
            if o["tx_tail"] <= 1:
                if m_usb:
                    n_st = T_TAIL2
                    self.tx_tail = (o["c_tail"] >> 8) & 0xFF
                else:
                    n_st = T_IDLE
                    nst |= 1
            else:
                self.tx_tail = o["tx_tail"] - 1
        elif tx_st == T_TAIL2 or tx_st == T_PULSE:
            if o["tx_tail"] <= 1:
                n_st = T_IDLE
                nst |= 1
            else:
                self.tx_tail = o["tx_tail"] - 1
        else:
            n_st = T_IDLE
        if p_we and p_addr == 4 and (wd >> 8) & 1:
            n_st = T_IDLE
        tstamp = o["tstamp"]
        if tx_go and m_tsel == 3:
            tstamp = now
        if p_we and p_addr == 4 and (wd >> 10) & 1:
            self.tx_cnt = 0
        else:
            if tx_take:
                self.tx_buf0, self.tx_lst0 = o["tx_buf1"], o["tx_lst1"]
            if tx_wr:
                if tx_cnt == 0 or (tx_cnt == 1 and tx_take):
                    self.tx_buf0, self.tx_lst0 = wd, (1 if p_addr == 14 else 0)
                elif tx_cnt == 1 or (tx_cnt == 2 and tx_take):
                    self.tx_buf1, self.tx_lst1 = wd, (1 if p_addr == 14 else 0)
            self.tx_cnt = tx_cnt - (1 if tx_take else 0) + (1 if (tx_wr and not (tx_cnt == 2 and not tx_take)) else 0)
        if full:
            jc = o["jab_cnt"]
            if not o["jabber"]:
                if not (tx_busy or tx_st == T_TAIL1 or tx_st == T_TAIL2 or tx_st == T_PULSE):
                    njc = 0
                elif (now & 511) == 0:
                    njc = (jc + 1) & 0x1FFFF
                else:
                    njc = jc
                if (jc >> 13) & 1:
                    self.jabber = 1
                    njc = 0
            else:
                njc = ((jc + 1) & 0x1FFFF) if (now & 511) == 0 else jc
                if (jc >> 16) & 1:
                    self.jabber = 0
                    njc = 0
            self.jab_cnt = njc

        # ---------------- status ----------------
        if rx_sop:
            nst |= 1 << 4
            if m_tsel == 1:
                tstamp = now
        if r_stuff_err:
            nst |= 1 << 8
        if (r_eop and not m_usb) or (rx_st == R_EOPW and not se0):
            nst |= 1 << 5
            if m_tsel == 0:
                tstamp = now
        if ca_now and not o["lt_a_hit"]:
            nst |= 1 << 10
        if cb_now and not o["lt_b_hit"]:
            nst |= 1 << 11
        c_lp = o["c_lp"]
        if full and not (aux0 and x1) and lp_w != 0 and (c_lp & 63) <= lp_w <= ((c_lp >> 6) & 63) and not carrier:
            nst |= 1 << 13
            if m_tsel == 2:
                tstamp = now
        if p_we and p_addr == 2:
            nst = sticky & ~wd & 0xFFFF
        self.sticky = nst
        self.tstamp = tstamp

        # ---------------- outputs (registered) ----------------
        tx_sck = o["tx_sck"]
        st_eff = T_IDLE if (o["tx_lead"] and tx_st == T_DATA) else tx_st
        if st_eff == T_DATA or st_eff == T_CRC or st_eff == T_JAM:
            if m_usb:
                v = (tx_line << 1) | (1 - tx_line)
            elif m_code == C_CLK:
                v = (tx_sck << 1) | tx_bit
            else:
                v = ((1 - tx_line) << 1) | tx_line
            oe = 1
        elif st_eff == T_TAIL1:
            v = 0 if m_usb else (1 if m_code == C_MAN else (((ltc8 << 1) | tx_bit) if m_code == C_CLK else 3))
            oe = 1
        elif st_eff == T_TAIL2:
            v, oe = 2, 1
        elif st_eff == T_PULSE:
            v, oe = 1, 1
        else:
            v = 2 if m_usb else (0 if m_code == C_MAN else ((ltc8 << 1) if m_code == C_CLK else 3))
            oe = 0
        self.tx_v = ((o["tx_v"] & 3) << 2) | v
        self.tx_oe = oe
        self.tx_line_q = tx_line
        self.tx_lead = 1 if tx_go else 0
        self.tx_st = n_st
        self.tx_tmr = (n_tmr & 0xFF) if spec == SPEC_USB else n_tmr

        # ---------------- receive loop (pe_slu_loop) ----------------
        if not o["lp_en"]:
            self.lp_th = self.lp_ii = self.lp_nacc = self.lp_nedge = self.lp_init = self.lp_dec = 0
            self.lp_xprev = x1
            self.lp_bv = 0
        else:
            c0, c1, c2, c3 = o["c_loop0"], o["c_loop1"], o["c_loop2"], o["c_loop3"]
            n = c0 & 63
            none, half = n << 12, n << 11
            manch = m_code == C_MAN
            mid = half if manch else 0
            tdec = ((n << 10) + (n << 11) - 2048) if manch else (half - 2048)
            k = (none, half, mid, tdec, -2048 - mid, (c2 & 63) << 12, ((c2 >> 6) & 63) << 12, c3 & 63,
                 c1 & 63, (c1 >> 6) & 63, (c0 >> 6) & 7, (c0 >> 9) & 7, (c0 >> 12) & 7, (c2 >> 12) & 1,
                 (c1 >> 12) & 15, (c2 >> 13) & 3)
            regs = (o["lp_th"], o["lp_ii"], o["lp_nacc"], o["lp_nedge"], o["lp_init"], o["lp_dec"])
            xp = o["lp_xprev"]
            bva = bda = 0
            if m_ddr:
                a = _loop_step(x0, xp, True, *regs, k)
                bva, bda = a[6], a[7]
                if x0 != xp:
                    b = _loop_step(x1, x0, False, *a[:6], k)
                else:
                    a0 = _loop_step(x0, xp, False, *regs, k)
                    b = _loop_step(x1, x0, True, *a0[:6], k)
            else:
                b = _loop_step(x1, xp, True, *regs, k)
            self.lp_th, self.lp_ii, self.lp_nacc, self.lp_nedge, self.lp_init, self.lp_dec = b[:6]
            self.lp_xprev = x1
            self.lp_bv = 1 if (bva or b[6]) else 0
            self.lp_bd = bda if bva else b[7]

        # ---------------- DIN capture: rising edge (this level), falling edge (the level before) ----------------
        self.din_f3 = o["din_f2"]
        self.din_f2 = din_pin_f
        self.din_r2 = o["din_r1"]
        self.din_r1 = din_pin


class _Ports:
    """The io hook of PeModel: helper ports 16 to 47 and the events of inputs 13 and 14."""

    def __init__(self, chip):
        self.chip = chip

    def _helper(self, port):
        h = self.chip.helpers.get("usb" if port < 32 else "eth")
        if h is None:
            raise pe_model.ModelError(f"port {port}: this run models only the {', '.join(self.chip.helpers)} "
                                      f"helper (slu_model.HelperChip helpers=)")
        return h

    def read(self, port):
        return self._helper(port).read(port & 15)

    def write(self, port, value):
        self._helper(port).write(port & 15, value)

    def events(self):
        h = self.chip.helpers
        return (h["usb"].ev() if "usb" in h else 0) | ((h["eth"].ev() if "eth" in h else 0) << 1)


PIN_HDRV, PIN_HETH = 1 << 4, 1 << 5    # pin configuration word (docs/host_port.md): helper drive, 10BASE-Te helper


class HelperChip(pe_model.PeModel):
    """PeModel with port-level models of the line helpers, wired as pe_proc.v.

    helper_cfg: for each output pin 0 to 14, None or the pin configuration word of a helper pin (PIN_HDRV,
    PIN_HETH for the 10BASE-Te helper, signal in bits 6 to 8: 0 TXA, 1 TXB, 2 TXC, 3 TXD, 4 OE, 5 not OE).
    helpers: the helpers modelled, ("usb",), ("eth",) or both (see the module text)."""

    def __init__(self, image, start_pc=(0, 0, 0, 0), run=(True, False, False, False), owner=None, open_drain=None,
                 helper_cfg=None, helpers=("usb", "eth"), prestart_now=(0xFFFD, 0xFFFE, 0xFFFF)):
        cfg = list(helper_cfg) if helper_cfg is not None else [None] * pe_model.N_OUT
        self.helper_cfg = [None if w is None else (bool(w & PIN_HETH), (w >> 6) & 7) for w in cfg]
        self.helpers = {name: SluModel(SPEC_USB if name == "usb" else SPEC_ETH) for name in helpers}
        for p, hc in enumerate(self.helper_cfg):
            if hc is not None and ("eth" if hc[0] else "usb") not in self.helpers:
                raise ValueError(f"pin {p} selects a helper that this run does not model")
        hp = [p for p, hc in enumerate(self.helper_cfg) if hc is not None]
        super().__init__(image, start_pc, run, owner, open_drain, helper_pins=hp, io=_Ports(self))
        if prestart_now is not None:
            self.prestart(prestart_now)

    def prestart(self, nows):
        """The cycles from the release of the processor reset to START (counter values `nows`): the helpers
        run with SEL at its reset value (no pin read), the helper pins take their values."""
        for now in nows:
            hs = self._helper_pins()
            for h in self.helpers.values():
                h.level_prev, h.level, h.sync = h.level, 0, 0
                h.clock(now)
            self._set_helper_pins(hs)

    def _helper_pins(self):
        out = {}
        for p, hc in enumerate(self.helper_cfg):
            if hc is None:
                continue
            h = self.helpers["eth" if hc[0] else "usb"]
            sig = hc[1]
            out[p] = (h.tx_v >> sig) & 1 if sig < 4 else (h.tx_oe if sig == 4 else 1 - h.tx_oe)
        return out

    def _set_helper_pins(self, values):
        for p, v in values.items():
            bit = 1 << p
            self.pin_v = (self.pin_v | bit) if v else (self.pin_v & ~bit)
            self.pin_oe |= bit          # OE of a helper pin is 1 while the threads run (pe_proc hrun)

    def step(self):
        c = self.cycle
        fmask = sum(1 << p for p in pe_model.FILTER_PINS)
        s = (self.sync2 & ~fmask) | self.filt
        oe = self.pin_oe & 0xFF
        level = (((self.uio_in & ~oe) | (self.pin_v & oe)) & 0xFF) | ((self.ui_in & 0x1F) << 8)
        for h in self.helpers.values():
            h.level_prev, h.level, h.sync = h.level, level, s & 0x1FFF
        hs = self._helper_pins()
        pins = super().step()
        for h in self.helpers.values():
            h.clock(c & 0xFFFF)
        self._set_helper_pins(hs)
        return pins
