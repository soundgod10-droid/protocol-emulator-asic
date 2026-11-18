/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// slu_fv: formal wrapper of one line-helper copy of the chip (src/pe_slu.v with SPEC 1, FULL 0: USB low
// speed; SPEC 2, FULL 1: 10BASE-Te), for formal/slu_usb.sby, slu_eth.sby, slu_cover_*.sby and the teeth.
//
// What it does:
//   Every port write and read, every written word and every pin input is free in every cycle; now counts up
//   by 1 from a free value (as the cycle counter between two STARTs); reset is free after step 0. The monitors
//   below follow the bit decisions of the transmitter (t_kind, t_bit at each bit start) and of the receiver
//   (r_data at each data bit) and check them against models written from the standards, not from the RTL:
//     H1  Each CRC bit sent is the complement of the next bit (highest power first) of the CRC of the data
//         bits sent after the first SKIP words (USB: the PID word; 10BASE-Te: preamble and SFD), computed in
//         the normal polynomial form: USB 2.0 8.3.5.2 CRC16 x^16 + x^15 + x^2 + 1, IEEE 802.3 3.2.9 CRC-32,
//         register preset to all ones.
//     H2  The receive CRC registers equal the CRC of the data bits received after the start pattern (USB:
//         after the 8 PID bits) in the same normal form; the CRC-OK compare holds exactly when that CRC
//         equals the standard's residue (USB CRC16 1000000000001101, CRC5 01100: USB 2.0 8.3.5; CRC-32
//         C704DD7B). USB also: CRC5 x^5 + x^2 + 1 (H2c).
//     H3  (USB) Bit stuffing, USB 2.0 7.1.9: after six 1 bits in a row a 0 is inserted, and only then.
//     H4  (USB) NRZI, USB 2.0 7.1.8: the line keeps its level for a 1 and changes for a 0, from J (idle);
//         checked on the outputs tx_v (VMO = J level, VPO = K level), one cycle after the line register.
//     H5  (10BASE-Te) Manchester, IEEE 802.3 7.3.1.1: the first half of each bit cell is the complement of
//         the bit, the second half the bit; checked on tx_v (TD+ = tx_v[0], TD- = tx_v[1]).
//     H6  The bit period: consecutive bit starts are exactly PER cycles apart (USB 40 = 666.7 ns at 60 MHz,
//         1.5 Mbit/s; 10BASE-Te 6 = 100 ns), and the 10BASE-Te mid-bit change TXPER cycles after the start.
//     H7  (10BASE-Te) After a collision, exactly 32 jam bits are sent before the tail (IEEE 802.3 4.4.2
//         jamSize = 32 bits).
//     H8  A frame's first symbol appears on tx_v (tx_oe rises) in the cycle with now = TXTIME.
//   The other asserts are invariants that tie the monitors to the transmitter state (needed for the
//   induction; each is also a fact about the RTL). The monitor wires mon_* are not driven here: the .sby
//   connects them to internal nets of the flattened helper. TXPER is the symbol period the monitors expect;
//   the cover jobs and teeth set it together with the helper's period register.

module slu_fv #(
    parameter SPEC  = 1,
    parameter TXPER = (SPEC == 1) ? 40 : 3,
    parameter SKIP  = (SPEC == 1) ? 1 : 4
) (
    input wire        clk,
    input wire        rst_n,
    input wire        p_we,
    input wire        p_re,
    input wire [3:0]  p_addr,
    input wire [15:0] p_wdata,
    input wire [12:0] pin_raw,
    input wire [12:0] pin_sync,
    // Replaces the falling-edge DIN sample din_f1 (connected by the .sby): any value in every cycle, so the
    // receive path sees every possible half-cycle sample and the model needs one clock edge only.
    input wire        din_f1_free,
    // Free receive strobes for the receive-CRC teeth (slu_teeth.sby connects r_sop, r_data_v, r_data to them,
    // so the CRC datapath is reached without a locked receive loop); unused in the proofs.
    input wire        sop_free,
    input wire        rdv_free,
    input wire        rd_free
);
  localparam FULL = (SPEC == 2) ? 1 : 0;
  localparam W    = (SPEC == 1) ? 16 : 32;
  localparam [31:0] POLY  = (SPEC == 1) ? 32'h00008005 : 32'h04C11DB7;   // normal form, x^W term left out
  localparam [31:0] RESID = (SPEC == 1) ? 32'h0000800D : 32'hC704DD7B;   // remainder of a good frame
  localparam RXSTART = (SPEC == 1) ? 8 : 0;                              // data bits before the CRC
  localparam PER     = (SPEC == 1) ? TXPER : 2 * TXPER;

  localparam [3:0] T_ARM = 4'd1, T_DATA = 4'd2, T_CRC = 4'd3, T_JAM = 4'd7;
  localparam [2:0] K_STUFF = 3'd1, K_DATA = 3'd2, K_LOAD = 3'd3, K_CRC = 3'd4, K_JAM = 3'd5, K_END = 3'd6;

  initial assume (!rst_n);
  // The receiver gives a start pattern (R_HUNT) and data bits (R_DATA) in different states, never in the
  // same cycle; the free strobes of the receive-CRC teeth keep that rule.
  always @* assume (!(sop_free && rdv_free));

  reg [15:0] now;
  always @(posedge clk) now <= now + 16'd1;

  wire [15:0] p_rdata;
  wire [3:0]  tx_v;
  wire        tx_oe, ev;
  pe_slu #(
      .FULL(FULL),
      .SPEC(SPEC)
  ) u_slu (
      .clk     (clk),
      .rst_n   (rst_n),
      .now     (now),
      .p_we    (p_we),
      .p_re    (p_re),
      .p_addr  (p_addr),
      .p_wdata (p_wdata),
      .p_rdata (p_rdata),
      .pin_raw (pin_raw),
      .pin_sync(pin_sync),
      .tx_v    (tx_v),
      .tx_oe   (tx_oe),
      .ev      (ev)
  );

  // Internal nets of the helper, connected by the .sby after flattening (not driven here; kept, so that
  // a wire that one helper form does not read still exists for the connect).
  (* keep *) wire [3:0]  mon_st;      // tx_st
  (* keep *) wire        mon_bs;      // bit_start
  (* keep *) wire        mon_hm;      // half_mid
  (* keep *) wire [2:0]  mon_kind;    // t_kind
  (* keep *) wire        mon_bit;     // t_bit
  (* keep *) wire        mon_new;     // t_new
  (* keep *) wire        mon_line;    // tx_line
  (* keep *) wire        mon_crcen;   // tx_crcen
  (* keep *) wire [2:0]  mon_skipw;   // tx_skipw
  (* keep *) wire [2:0]  mon_run;     // tx_run
  (* keep *) wire [5:0]  mon_jam;     // tx_jam
  (* keep *) wire [15:0] mon_tmr;     // tx_tmr
  (* keep *) wire        mon_half;    // tx_half
  (* keep *) wire        mon_txbit;   // tx_bit
  (* keep *) wire        mon_lead;    // tx_lead
  (* keep *) wire [15:0] mon_time;    // tx_time
  (* keep *) wire [31:0] mon_crct;    // crc_t
  (* keep *) wire [31:0] mon_crca;    // crc_a
  (* keep *) wire [7:0]  mon_crcb;    // crc_b
  (* keep *) wire [31:0] mon_ra;      // c_ra (CRC A residue register)
  (* keep *) wire [15:0] mon_crcbc;   // c_crcb (CRC B polynomial and residue register)
  (* keep *) wire        mon_rdv;     // r_data_v
  (* keep *) wire        mon_rd;      // r_data
  (* keep *) wire        mon_sop;     // r_sop
  (* keep *) wire [9:0]  mon_rxbits;  // rx_bits
  (* keep *) wire [15:0] mon_cmode;   // c_mode
  (* keep *) wire [15:0] mon_ctxper;  // c_txper
  (* keep *) wire [15:0] mon_ccrcw;   // c_crcw
  (* keep *) wire [31:0] mon_cpa;     // c_pa

  // Fixed configuration of the helper form (src/pe_slu.v F_*): set at reset and never written for SPEC 1
  // and 2 (only SEL is writable). Asserted as invariants, except in the cover and tooth jobs, which force
  // some of these registers (SLU_FORCED).
  localparam [15:0] F_MODE  = (SPEC == 1) ? 16'h03B9 : 16'h040E;
  localparam [15:0] F_TXPER = (SPEC == 1) ? 16'd40 : 16'd3;
  localparam [15:0] F_CRCW  = (SPEC == 1) ? 16'h20D0 : 16'h0260;
  localparam [31:0] F_PA    = (SPEC == 1) ? 32'h0000A001 : 32'hEDB88320;
  localparam [31:0] F_RA    = (SPEC == 1) ? 32'h0000B001 : 32'hDEBB20E3;
  localparam [15:0] F_CRCB  = (SPEC == 1) ? 16'h0614 : 16'h0000;
`ifndef SLU_FORCED
  always @* begin
    assert (mon_cmode == F_MODE);
    assert (mon_ctxper == F_TXPER);
    assert (mon_ccrcw == F_CRCW);
    assert (mon_cpa == F_PA);
    assert (mon_ra == F_RA);
    assert (mon_crcbc == F_CRCB);
  end
`endif

  // Bit reversal: the RTL keeps the CRC in the reflected (LSB-first) form.
  function [31:0] rev(input [31:0] x);
    integer i;
    begin
      rev = 32'd0;
      for (i = 0; i < W; i = i + 1) rev[i] = x[W-1-i];
    end
  endfunction
  function [4:0] rev5(input [4:0] x);
    rev5 = {x[0], x[1], x[2], x[3], x[4]};
  endfunction

  reg f_past = 1'b0;
  always @(posedge clk) f_past <= 1'b1;

  // ---------------------------------------------------------------------------------------------------
  // Transmit monitors. A frame starts in the first T_DATA cycle after T_ARM; its first bit start is in
  // that cycle (the symbol timer is 0 after the start).
  // ---------------------------------------------------------------------------------------------------
  reg [3:0] st_q;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) st_q <= 4'd0;
    else st_q <= mon_st;
  wire first = (mon_st == T_DATA) && (st_q == T_ARM);
  wire m_on  = (mon_st == T_DATA) || (mon_st == T_CRC);
  wire busy  = m_on || (mon_st == T_JAM);

  reg [W-1:0] m_s;              // CRC of the covered data bits sent so far, normal form
  reg [2:0]   m_w;              // words started in this frame (saturating)
  reg [2:0]   m_run;            // 1 bits in a row on the line (USB)
  reg         m_lvl;            // NRZI line level (USB)
  reg         m_b, m_h, m_mv;   // Manchester: present bit, second half, a bit has started
  reg [15:0]  m_c;              // cycles since the last bit start
  reg         m_cv;             // a bit has started in this frame
  reg [5:0]   m_j;              // jam bits sent

  wire [W-1:0] s_cur   = first ? {W{1'b1}} : m_s;
  wire [2:0]   w_cur   = first ? 3'd1 : m_w;
  wire [2:0]   run_cur = first ? 3'd0 : m_run;
  wire         lvl_cur = first ? 1'b1 : m_lvl;
  wire         mv_cur  = first ? 1'b0 : m_mv;
  wire         jfirst  = (mon_st == T_JAM) && (st_q != T_JAM);
  // The bit timing restarts at a jam: a collision seen in the cycle of the frame's end decision starts the
  // jam at the next symbol boundary (half a bit later), which IEEE 802.3 allows (the jam content is free).
  wire         cv_cur  = first ? 1'b0 : m_cv;
  wire [15:0]  c_cur   = m_c;
  reg          m_jx;                                 // the next bit start is the first one of a jam
  wire         jx_cur  = jfirst ? 1'b1 : (first ? 1'b0 : m_jx);
  wire [5:0]   j_cur   = jfirst ? 6'd0 : m_j;

  wire covered = mon_crcen && (((mon_kind == K_DATA) && (w_cur > SKIP)) ||
                               ((mon_kind == K_LOAD) && ({1'b0, w_cur} + 4'd1 > SKIP)));
  wire [W-1:0] s_step = {s_cur[W-2:0], 1'b0} ^ ((s_cur[W-1] ^ mon_bit) ? POLY[W-1:0] : {W{1'b0}});

  always @(posedge clk) begin
    m_s   <= s_cur;
    m_w   <= w_cur;
    m_run <= run_cur;
    m_lvl <= lvl_cur;
    m_mv  <= busy && mv_cur;
    m_cv  <= busy && cv_cur;
    m_c   <= (c_cur == 16'hFFFF) ? c_cur : c_cur + 16'd1;
    m_jx  <= busy && jx_cur && !mon_bs;
    m_j   <= j_cur;
    if (mon_hm) m_h <= 1'b1;
    if (mon_bs) begin
      m_c  <= 16'd0;
      m_cv <= 1'b1;
      if (m_on && covered) m_s <= s_step;
      else if (m_on && mon_kind == K_CRC) m_s <= {s_cur[W-2:0], 1'b0};
      if (mon_kind == K_LOAD) m_w <= (w_cur == 3'd7) ? 3'd7 : w_cur + 3'd1;
      if (mon_new && mon_kind != K_JAM) m_run <= mon_bit ? run_cur + 3'd1 : 3'd0;
      if (mon_new) begin
        m_lvl <= mon_bit ? lvl_cur : ~lvl_cur;
        m_b   <= mon_bit;
        m_h   <= 1'b0;
        m_mv  <= 1'b1;
      end
      if (mon_kind == K_JAM) m_j <= j_cur + 6'd1;
    end
  end

  // Expected outputs one cycle after the line register (the first T_DATA cycle shows the idle state).
  reg        vq_ok;
  reg  [1:0] vq;
  wire       man_e = m_h ? m_b : ~m_b;
  always @(posedge clk) begin
    vq_ok <= rst_n && busy && !(mon_lead && mon_st == T_DATA) && ((SPEC == 1) || mv_cur);
    vq    <= (SPEC == 1) ? {lvl_cur, ~lvl_cur} : {~man_e, man_e};
  end

  reg oe_q, lead_q;
  always @(posedge clk) begin
    oe_q   <= tx_oe;
    lead_q <= mon_lead;
  end

  // Receive monitors (declared here, used below): the CRC of the data bits after the start pattern.
  reg [W-1:0] m_r;
  reg [4:0]   m_r5;
  reg [9:0]   m_nb;
  reg         m_rv;

  always @* begin
    if (rst_n) begin
      if (m_on) begin
        // H1 and its invariants
        if (mon_bs && mon_kind == K_CRC) assert (mon_bit == ~s_cur[W-1]);
        assert (mon_crct == rev({{(32-W){1'b0}}, s_cur}));
        assert (mon_skipw == (({1'b0, w_cur} - 4'd1 >= SKIP) ? 3'd0 : SKIP[2:0] - (w_cur - 3'd1)));
        assert (w_cur != 3'd0);
      end
      if (busy) begin
        // H6 and its invariants
        if (mon_bs) assert (!cv_cur || jx_cur || (c_cur == PER - 1));
        if (cv_cur) begin
          if (SPEC == 1) assert (mon_tmr + c_cur == TXPER - 1);
          else if (!jx_cur) assert (mon_tmr + c_cur == (mon_half ? TXPER - 1 : 2 * TXPER - 1));
          else assert (mon_tmr + c_cur == TXPER - 1 || (!mon_half && mon_tmr + c_cur == 2 * TXPER - 1));
        end else begin
          // only the first cycle of a frame has no bit start behind it
          assert (first && mon_tmr == 16'd0);
        end
      end
      // H8: a frame starts on tx_v at TXTIME (a link test pulse is not a frame: st_q 6 is T_PULSE), with its
      // invariants: tx_oe is the registered "a symbol is on the line" of the cycle before, and the first
      // T_DATA cycle is 2 cycles before TXTIME.
      if (f_past && tx_oe && !oe_q && st_q != 4'd6) assert (now == mon_time);
      if (f_past) assert (tx_oe == ((st_q >= 4'd2) && (st_q <= 4'd7) && !(lead_q && st_q == T_DATA)));
      if (mon_lead) assert (now == mon_time - 16'd2);
      // H2: receive CRC registers
      if (m_rv) begin
        assert (mon_crca == rev({{(32-W){1'b0}}, m_r}));
        assert (((((mon_crca ^ mon_ra) & rev({{(32-W){1'b0}}, {W{1'b1}}})) == 32'd0)) == (m_r == RESID[W-1:0]));
        assert (mon_rxbits == m_nb);
      end
    end
  end

  generate
    if (SPEC == 1) begin : g_usb
      always @* begin
        if (rst_n) begin
          if (m_on) begin
            // H3: stuffing after six 1 bits, and only then
            if (mon_bs && mon_new) begin
              if (run_cur == 3'd6) assert ((mon_kind == K_STUFF) && !mon_bit);
              if (mon_kind == K_STUFF) assert (run_cur == 3'd6);
            end
            assert (run_cur <= 3'd6);
            assert (mon_run == run_cur);
            // H4: NRZI level in the line register
            assert (mon_line == lvl_cur);
          end
          if (mon_st == T_ARM) assert (mon_line);
          // H4 on the outputs
          if (vq_ok) assert (tx_v[1:0] == vq);
          // H2c: CRC5
          if (m_rv) begin
            assert (mon_crcb == {3'b000, rev5(m_r5)});
            assert ((mon_crcb == mon_crcbc[15:8]) == (m_r5 == 5'b01100));
          end
        end
      end
    end else begin : g_eth
      always @* begin
        if (rst_n) begin
          if (busy) begin
            // H5: Manchester halves in the line register and on the outputs
            if (mv_cur) begin
              assert (mon_line == man_e);
              assert (mon_txbit == m_b);
              assert (mon_half == !m_h);
            end
            if (mon_hm) assert (c_cur == TXPER - 1);
            // the first bit start of a frame begins a bit (the first word has 1 to 16 bits)
            assert (cv_cur == mv_cur);
          end
          if (vq_ok) assert (tx_v[1:0] == vq);
          // H7: 32 jam bits
          if (mon_st == T_JAM) begin
            assert (mon_jam + j_cur == 6'd32);
            if (mon_bs && mon_kind == K_END) assert (j_cur == 6'd32);
          end
        end
      end
    end
  endgenerate

  // ---------------------------------------------------------------------------------------------------
  // Receive monitors: the CRC of the data bits after the start pattern (r_sop), from the RXSTART-th bit.
  // ---------------------------------------------------------------------------------------------------
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      m_rv <= 1'b0;
    end else begin
      if (mon_sop) begin
        m_r  <= {W{1'b1}};
        m_r5 <= 5'b11111;
        m_nb <= 10'd0;
        m_rv <= 1'b1;
      end
      if (mon_rdv) begin
        if (m_nb >= RXSTART) begin
          m_r  <= {m_r[W-2:0], 1'b0} ^ ((m_r[W-1] ^ mon_rd) ? POLY[W-1:0] : {W{1'b0}});
          m_r5 <= {m_r5[3:0], 1'b0} ^ ((m_r5[4] ^ mon_rd) ? 5'b00101 : 5'b00000);
        end
        m_nb <= (m_nb == 10'h3FF) ? m_nb : m_nb + 10'd1;
      end
    end
  end

  // ---------------------------------------------------------------------------------------------------
  // H9 (10BASE-Te, slu_eth.sby only: SLU_JABBER): the unjab time runs from the jabber onset. While jabber is
  // set, the jabber counter equals the 512-cycle ticks since the onset, so jabber ends exactly 65,536 ticks
  // (2^25 cycles, 0.559 s at 60 MHz; IEEE 802.3 14.2.1.6 unjab 0.25-0.75 s) after it began.
  // ---------------------------------------------------------------------------------------------------
`ifdef SLU_JABBER
  (* keep *) wire        mon_jabber;  // jabber
  (* keep *) wire [16:0] mon_jabcnt;  // jab_cnt
  reg [16:0] m_jt;
  always @(posedge clk) begin
    if (!mon_jabber) m_jt <= 17'd0;
    else if (now[8:0] == 9'd0) m_jt <= m_jt + 17'd1;
  end
  always @* if (rst_n && mon_jabber) assert (mon_jabcnt == m_jt);
  // H10: jabber is set only by transmit time without a break: while jabber is clear, the counter equals the
  // 512-cycle ticks since the last cycle with no transmit activity (frame, CRC, jam, tails or link pulse), so
  // jabber comes exactly 8,192 ticks (2^22 cycles, 69.9 ms at 60 MHz; IEEE 802.3 14.2.1.6 xmit_max 20-150 ms)
  // into an unbroken transmission and never from idle time.
  reg [16:0] m_ja;
  wire       m_act = (mon_st >= 4'd2) && (mon_st <= 4'd7);
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) m_ja <= 17'd0;
    else if (mon_jabber || !m_act) m_ja <= 17'd0;
    else if (now[8:0] == 9'd0) m_ja <= m_ja + 17'd1;
  end
  always @* if (rst_n && !mon_jabber) assert (mon_jabcnt == m_ja);
`endif

  // ---------------------------------------------------------------------------------------------------
  // Covers (cover jobs only): a CRC bit sent, a stuffed bit (USB), a collision that starts a jam
  // (10BASE-Te). The end of a 32-bit jam needs about 75 steps even with 1-cycle symbols, beyond the cover
  // budget; H7 rests on its k-induction proof (jam counter + bits sent = 32).
  // ---------------------------------------------------------------------------------------------------
`ifdef SLU_COVER
  always @* begin
    cover (rst_n && m_on && mon_bs && mon_kind == K_CRC);
    if (SPEC == 1) cover (rst_n && m_on && mon_bs && mon_kind == K_STUFF);
    if (SPEC == 2) cover (rst_n && jfirst);
  end
`endif

endmodule
