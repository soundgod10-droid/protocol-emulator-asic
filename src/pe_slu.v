/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Serial line unit (SLU): the bit-level work of a serial line at line rate, so that a thread works on
// words and frames. One unit serves:
//   - USB low speed: NRZI, bit stuffing (6 ones), SYNC detection, EOP (qualified SE0), CRC5 and
//     CRC16 checked at the same time, CRC16 appended, SE0/J tail, line-state timers (reset, suspend),
//     timed reply start (TXTIME, from the EOP time stamp);
//   - 10BASE-Te (FULL = 1): Manchester at one symbol per half bit, both-edge receive loop, SFD
//     detection after a lock count, CRC-32 checked and appended, TP_IDL hold, link test pulses,
//     squelch rules (edge-spacing qualifier on the squelch comparator), link-pulse classifier,
//     collision (transmit while carrier) with a 32-bit jam, jabber (2^22-cycle TX limit, 2^25-cycle
//     unjab);
//   - asynchronous NRZ lines (UART: start-bit pattern, fixed frame length) and clocked lines
//     (clock and data out, data in at a set delay after the sampling clock edge).
//
// Thread interface: 16 I/O ports (IOR, IOW; docs/isa.md section 10), one event output (any status
// flag AND its mask), pins through the pin configuration. Every output goes to a pin flip-flop
// outside this module, so each chip output still comes from a flip-flop.
//
// Ports (p_addr):
//   0 TXDATA  W push a word (LSB first on the line)   R TX buffer free (0 to 2)
//   1 RXDATA  R pop a word (0 if empty)
//   2 STATUS  R flags                                  W 1 clears a sticky flag
//   3 EVMASK  R/W flags that drive the event output
//   4 TXCTRL  W [4:0] bits of the last word (0 = 16), [5] append CRC, [6] no start when late,
//               [7] link test pulse instead of a frame, [8] abort, [9] start now, [10] flush
//             R [3:0] TX state
//   5 TXTIME  W start time (compared with now[15:0]): lead 4 to 32,767 cycles; else late (TXLATE):
//               start at write + 4, or no start with TXCTRL[6]       R armed time
//   6 RXINFO  R [4:0] bits in the last word of the frame, [6:5] RX buffer words, [15:7] words
//   7 CRCA_LO R CRC A [15:0]     8 CRCA_HI R CRC A [31:16]     9 CRCB R CRC B [7:0]
//  10 TSTAMP  R now[15:0] at the event chosen by MODE[13:12]
//  11 LINE    R [0] AUX0, [1] AUX1, [2] DIN, [3] carrier, [7:4] RX state
//  12 CFGADR  W configuration index (+1 after each CFGDAT write)
//  13 CFGDAT  W configuration word (write only)
//  14 TXLAST  W push the last word of the frame (its bit count from TXCTRL[4:0])
//
// STATUS: 0 TXDONE, 1 TXLATE, 2 TXUNDER, 3 RXWORD (level), 4 RXSOP, 5 RXEOP, 6 CRCA_OK (level,
// last frame), 7 CRCB_OK (level), 8 STUFFERR, 9 RXOVER, 10 LINEA, 11 LINEB, 12 CARRIER (level),
// 13 LINKPULSE, 14 COLLISION, 15 JABBER (level). Sticky flags stay until cleared.

module pe_slu #(
    parameter FULL = 1
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] now,
    input  wire        p_we,
    input  wire        p_re,
    input  wire [3:0]  p_addr,
    input  wire [15:0] p_wdata,
    output reg  [15:0] p_rdata,
    input  wire [12:0] pin_raw,
    input  wire [12:0] pin_sync,
    output reg  [3:0]  tx_v,
    output reg         tx_oe,
    output wire        ev
);

  // ----------------------------------------------------------------------------------------------
  // Configuration (write only; index in CFGADR)
  // ----------------------------------------------------------------------------------------------
  // 0 MODE  [1:0] code 0 NRZ 1 NRZI 2 Manchester 3 clocked, [2] two samples per cycle, [3] RX on,
  //         [4] stuffing, [5] stuffing counts ones (else equal bits), [8:6] stuff run K,
  //         [9] USB line (TXA/TXB = VPO/VMO, AUX0/AUX1 = VP/VM, EOP = qualified SE0),
  //         [10] EOP when the carrier ends, [11] EOP after a fixed bit count,
  //         [13:12] TSTAMP event 0 EOP 1 SOP 2 link pulse 3 TX start, [14] OE active low,
  //         [15] internal loopback (TX line into RX)
  // 1 TXPER symbol period in cycles (NRZ, NRZI: one bit; Manchester, clocked: half a bit), >= 1
  // 2 LOOP0 [5:0] N samples per bit, [8:6] kp gear 1, [11:9] kp gear 2, [14:12] kp gear 3
  // 3 LOOP1 [5:0] gear 2 from edge, [11:6] gear 3 from edge, [15:12] integrator shift - 4
  // 4 LOOP2 [5:0] window, [11:6] acquisition window (samples), [12] integrator in gear 3,
  //         [14:13] edges before the initial sync
  // 5 LOOP3 [5:0] acquisition edges, [11:6] accepted edges before the pattern detector
  // 6 PAT   pattern, newest received bit in bit 0
  // 7 PAT2  [3:0] pattern length - 1, [9:4] fixed frame bits, [15:10] EOP qualifier samples
  // 8 SEL   [3:0] DIN pin, [7:4] AUX0 pin, [11:8] AUX1 pin, [15:12] clocked sample delay
  // 9,10    CRC A polynomial (reflected), low and high word
  // 11,12   CRC A residue, low and high word
  // 13 CRCB [7:0] CRC B polynomial (reflected; preset all ones), [15:8] CRC B residue
  // 14 CRCW [5:0] CRC A width, [6] CRC A preset all ones, [9:7] TX words before the CRC starts,
  //         [15:10] RX bits after the pattern before the CRC starts
  // 15 TAIL [7:0] tail 1 cycles (USB SE0; TP_IDL hold; link pulse width), [15:8] tail 2 (USB J)
  // 16 LTA  line condition A threshold, 17 LTB condition B threshold (prescaled cycles; 0 = off)
  // 18 LTC  [3:0] prescale shift, [5:4] condition A, [7:6] condition B (0 SE0, 1 J, 2 K,
  //         3 no DIN edge), [8] clocked: clock idle level, [9] clocked: data out with the leading edge
  // 19 SQ   [5:0] min and [11:6] max edge spacing (half cycles), [15:12] spacings in a row (carrier)
  // 20 LP   [5:0] min and [11:6] max link pulse width (cycles)

  reg [15:0] c_mode, c_txper, c_loop0, c_loop1, c_loop2, c_loop3, c_pat, c_pat2, c_sel;
  reg [31:0] c_pa, c_ra;
  reg [15:0] c_crcb, c_crcw, c_tail, c_lta, c_ltb, c_ltc, c_sq, c_lp;
  reg [4:0]  cfg_adr;

  localparam C_NRZ = 2'd0, C_NRZI = 2'd1, C_MAN = 2'd2, C_CLK = 2'd3;
  wire [1:0] m_code    = c_mode[1:0];
  wire       m_ddr     = c_mode[2];
  wire       m_rx      = c_mode[3];
  wire       m_stuff   = c_mode[4];
  wire       m_ones    = c_mode[5];
  wire [2:0] m_k       = c_mode[8:6];
  wire       m_usb     = c_mode[9];
  wire       m_eop_car = c_mode[10];
  wire       m_eop_len = c_mode[11];
  wire [1:0] m_tsel    = c_mode[13:12];
  wire       m_oe_low  = c_mode[14];
  wire       m_loop    = c_mode[15];
  wire       m_half    = (m_code == C_MAN) || (m_code == C_CLK);   // two symbols per bit

  // ----------------------------------------------------------------------------------------------
  // Inputs: DIN on both clock edges (raw pad input), AUX0/AUX1 from the chip synchronizers
  // ----------------------------------------------------------------------------------------------
  wire din_pin = (c_sel[3:0] < 4'd13) ? pin_raw[c_sel[3:0]] : 1'b0;
  wire aux0    = (c_sel[7:4] < 4'd13) ? pin_sync[c_sel[7:4]] : 1'b0;
  wire aux1    = (c_sel[11:8] < 4'd13) ? pin_sync[c_sel[11:8]] : 1'b0;

  reg din_f1;
  always @(negedge clk or negedge rst_n)
    if (!rst_n) din_f1 <= 1'b0; else din_f1 <= din_pin;

  reg din_f2, din_f3, din_r1, din_r2, tx_line_q;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      din_f2 <= 1'b0; din_f3 <= 1'b0; din_r1 <= 1'b0; din_r2 <= 1'b0;
    end else begin
      din_f2 <= din_f1;
      din_f3 <= din_f2;
      din_r1 <= din_pin;
      din_r2 <= din_r1;
    end
  end
  // x0 = falling-edge sample half a cycle before x1; both 2 cycles behind the pad like s(c).
  wire x0 = m_loop ? tx_line_q : din_f3;
  wire x1 = m_loop ? tx_line_q : din_r2;

  // ----------------------------------------------------------------------------------------------
  // Receive loop
  // ----------------------------------------------------------------------------------------------
  wire       lp_bv, lp_bd, lp_init;
  wire [5:0] lp_nacc;
  reg        lp_en;
  pe_slu_loop u_loop (
      .clk(clk), .rst_n(rst_n), .en(lp_en), .ddr(m_ddr), .x0(x0), .x1(x1), .n(c_loop0[5:0]),
      .manch(m_code == C_MAN), .kp0(c_loop0[8:6]), .kp1(c_loop0[11:9]), .kp2(c_loop0[14:12]),
      .gn0(c_loop1[5:0]), .gn1(c_loop1[11:6]), .ki_en(c_loop2[12]), .ki_sh(c_loop1[15:12]),
      .win(c_loop2[5:0]), .wina(c_loop2[11:6]), .acq_n(c_loop3[5:0]), .init_edge(c_loop2[14:13]),
      .bit_v(lp_bv), .bit_d(lp_bd), .nacc(lp_nacc), .locked_init(lp_init)
  );

  // ----------------------------------------------------------------------------------------------
  // Line conditions
  // ----------------------------------------------------------------------------------------------
  wire       se0      = ~aux0 & ~aux1;
  wire [1:0] lstate   = {aux0, aux1};               // 00 SE0, 01 J, 10 K, 11 SE1 (USB VP, VM)
  reg        din_prev;
  wire       din_rise = m_ddr ? ((x0 & ~din_prev) | (x1 & ~x0)) : (x1 & ~din_prev);
  wire       din_edge = m_ddr ? ((x0 ^ din_prev) | (x1 ^ x0)) : (x1 ^ din_prev);
  reg  [5:0] q_cnt;                                 // SE0 samples (USB) / cycles since a rising edge
  wire       eop_q    = (q_cnt >= c_pat2[15:10]);

  reg        carrier;
  reg  [6:0] sq_gap;
  reg  [3:0] sq_good;
  reg  [5:0] lp_w;

  reg  [15:0] lt_a, lt_b;
  reg         lt_a_hit, lt_b_hit;
  wire        lt_tick = ((now & ((16'd1 << c_ltc[3:0]) - 16'd1)) == 16'd0);
  function cond_hold(input [1:0] c, input [1:0] ls, input de);
    case (c)
      2'd0:    cond_hold = (ls == 2'b00);
      2'd1:    cond_hold = (ls == 2'b01);
      2'd2:    cond_hold = (ls == 2'b10);
      default: cond_hold = ~de;
    endcase
  endfunction
  wire ca_hold = cond_hold(c_ltc[5:4], lstate, din_edge);
  wire cb_hold = cond_hold(c_ltc[7:6], lstate, din_edge);
  wire ca_now  = ca_hold && (c_lta != 16'd0) && (lt_a >= c_lta);
  wire cb_now  = cb_hold && (c_ltb != 16'd0) && (lt_b >= c_ltb);

  // ----------------------------------------------------------------------------------------------
  // CRC (LSB first, reflected polynomial)
  // ----------------------------------------------------------------------------------------------
  wire [5:0]  crc_w    = c_crcw[5:0];
  wire [31:0] crc_mask = (crc_w >= 6'd32) ? 32'hFFFFFFFF : ((32'd1 << crc_w) - 32'd1);
  wire [31:0] crc_init = c_crcw[6] ? crc_mask : 32'd0;
  function [31:0] crc_step(input [31:0] c, input b, input [31:0] poly);
    crc_step = (c >> 1) ^ ((c[0] ^ b) ? poly : 32'd0);
  endfunction

  // ----------------------------------------------------------------------------------------------
  // Receive datapath
  // ----------------------------------------------------------------------------------------------
  localparam R_OFF = 4'd0, R_HUNT = 4'd1, R_DATA = 4'd2, R_EOPW = 4'd3, R_RST = 4'd4;
  reg  [3:0]  rx_st;
  reg         rx_lvl_prev;
  reg  [15:0] pat_sh;
  reg  [2:0]  rx_run;
  reg         rx_lastbit;
  reg  [15:0] rx_sh;
  reg  [4:0]  rx_nb;
  reg  [8:0]  rx_words;
  reg  [9:0]  rx_bits;
  reg  [15:0] rx_buf0, rx_buf1;
  reg  [1:0]  rx_cnt;
  reg  [4:0]  rx_lastn;
  reg  [31:0] crc_a;
  reg  [7:0]  crc_b;
  reg         crc_a_ok, crc_b_ok, crc_a_byte, crc_b_byte;
  reg  [15:0] ck_pend;              // clocked mode: sample times in flight
  reg         ck_bit_v, ck_bit_d;

  wire [3:0]  pat_len  = c_pat2[3:0];
  wire [15:0] pat_mask = (pat_len == 4'd15) ? 16'hFFFF : ((16'd1 << (pat_len + 4'd1)) - 16'd1);

  // Combinational receive step.
  reg         r_dec_v, r_dec, r_sop, r_eop, r_data_v, r_data, r_stuff_err, r_stuff_drop;
  always @(*) begin
    r_dec_v      = 1'b0;
    r_dec        = 1'b0;
    r_sop        = 1'b0;
    r_eop        = 1'b0;
    r_data_v     = 1'b0;
    r_data       = 1'b0;
    r_stuff_err  = 1'b0;
    r_stuff_drop = 1'b0;
    if (m_code == C_CLK) begin
      r_dec_v = ck_bit_v;
      r_dec   = ck_bit_d;
    end else if (lp_bv && !(m_usb && se0)) begin
      r_dec_v = 1'b1;
      r_dec   = (m_code == C_NRZI) ? (lp_bd == rx_lvl_prev) : lp_bd;
    end
    if (rx_st == R_HUNT && r_dec_v && m_code != C_CLK) begin
      r_sop = (lp_nacc >= c_loop3[11:6]) && ((({pat_sh[14:0], r_dec} ^ c_pat) & pat_mask) == 16'd0);
    end
    if (rx_st == R_DATA) begin
      r_eop = (m_usb && eop_q) || (!m_usb && m_eop_car && eop_q) ||
              (m_eop_len && (rx_bits >= {4'd0, c_pat2[9:4]}));
      if (r_dec_v && !r_eop) begin
        if (m_stuff && (rx_run == m_k)) begin
          r_stuff_drop = 1'b1;
          r_stuff_err  = m_ones ? r_dec : (r_dec == rx_lastbit);
        end else begin
          r_data_v = 1'b1;
          r_data   = r_dec;
        end
      end
    end
  end

  wire        rx_word_full = r_data_v && (rx_nb == 5'd15);
  wire [15:0] rx_word      = {r_data, rx_sh[15:1]};
  wire [15:0] rx_part      = rx_sh >> (5'd16 - rx_nb);
  wire        rx_push      = (rx_word_full || (r_eop && rx_nb != 5'd0));
  wire [15:0] rx_push_w    = rx_word_full ? rx_word : rx_part;
  wire        rx_pop       = p_re && (p_addr == 4'd1) && (rx_cnt != 2'd0);

  // ----------------------------------------------------------------------------------------------
  // Transmit datapath
  // ----------------------------------------------------------------------------------------------
  localparam T_IDLE = 4'd0, T_ARM = 4'd1, T_DATA = 4'd2, T_CRC = 4'd3, T_TAIL1 = 4'd4,
             T_TAIL2 = 4'd5, T_PULSE = 4'd6, T_JAM = 4'd7;
  reg  [3:0]  tx_st;
  reg  [15:0] tx_buf0, tx_buf1;
  reg         tx_lst0, tx_lst1;
  reg  [1:0]  tx_cnt;
  reg  [15:0] tx_sh;
  reg  [4:0]  tx_nb;
  reg         tx_lastw;
  reg  [15:0] tx_tmr;
  reg         tx_half;
  reg         tx_bit;
  reg         tx_line;
  reg         tx_sck;
  reg  [2:0]  tx_run;
  reg         tx_runbit;
  reg  [5:0]  tx_crcn;
  reg  [2:0]  tx_skipw;
  reg  [7:0]  tx_tail;
  reg  [5:0]  tx_jam;
  reg  [15:0] tx_time;
  reg  [4:0]  tx_lastbits;
  reg         tx_crcen, tx_droplate, tx_pulse;
  reg  [31:0] crc_t;
  reg         tx_lead;              // first cycle after the start
  reg  [16:0] jab_cnt;
  reg         jabber;

  wire        tx_busy   = (tx_st == T_DATA) || (tx_st == T_CRC) || (tx_st == T_JAM);
  wire        sym_end   = (tx_tmr == 16'd0);
  wire        bit_start = tx_busy && sym_end && !(m_half && tx_half);
  wire        half_mid  = tx_busy && sym_end && m_half && tx_half;
  // Start 3 cycles before TXTIME: the first symbol is on tx_v in cycle TXTIME and, after the pin
  // flip-flop, on the pad from TXTIME + 1, as an OUT event at time TXTIME (docs/isa.md 4.4).
  wire        tx_go     = (tx_st == T_ARM) && (now == tx_time - 16'd3);
  wire        need_stuff = m_stuff && (tx_run == m_k);
  wire        stuff_bit = m_ones ? 1'b0 : ~tx_runbit;
  wire [4:0]  len0      = (tx_lst0 && tx_lastbits != 5'd0) ? tx_lastbits : 5'd16;

  // Next bit at a bit start (combinational).
  localparam K_NONE = 3'd0, K_STUFF = 3'd1, K_DATA = 3'd2, K_LOAD = 3'd3, K_CRC = 3'd4,
             K_JAM = 3'd5, K_END = 3'd6, K_UNDER = 3'd7;
  reg  [2:0]  t_kind;
  reg         t_bit;
  always @(*) begin
    t_kind = K_NONE;
    t_bit  = tx_bit;
    if (bit_start) begin
      if (tx_st == T_JAM) begin
        t_kind = (tx_jam == 6'd0) ? K_END : K_JAM;
        t_bit  = ~tx_bit;
      end else if (need_stuff) begin
        t_kind = K_STUFF;
        t_bit  = stuff_bit;
      end else if (tx_st == T_CRC) begin
        t_kind = (tx_crcn == 6'd0) ? K_END : K_CRC;
        t_bit  = ~crc_t[0];
      end else if (tx_nb != 5'd0) begin
        t_kind = K_DATA;
        t_bit  = tx_sh[0];
      end else if (!tx_lastw) begin
        t_kind = (tx_cnt != 2'd0) ? K_LOAD : K_UNDER;
        t_bit  = tx_buf0[0];
      end else if (tx_crcen && crc_w != 6'd0) begin
        t_kind = K_CRC;
        t_bit  = ~crc_t[0];
      end else begin
        t_kind = K_END;
      end
    end
  end
  wire t_new  = (t_kind == K_STUFF) || (t_kind == K_DATA) || (t_kind == K_LOAD) ||
                (t_kind == K_CRC) || (t_kind == K_JAM);
  wire t_dbit = (t_kind == K_DATA) || (t_kind == K_LOAD);    // a data bit (goes into the CRC)
  wire tx_take_go   = tx_go && !tx_pulse && !(FULL && jabber) && (tx_cnt != 2'd0);
  wire tx_take_bit  = (t_kind == K_LOAD);
  wire tx_take      = tx_take_go || tx_take_bit;
  wire tx_wr        = p_we && ((p_addr == 4'd0) || (p_addr == 4'd14));
  wire [15:0] lead  = p_wdata - now;

  // ----------------------------------------------------------------------------------------------
  // Status, events, time stamp, port reads
  // ----------------------------------------------------------------------------------------------
  reg  [15:0] sticky;
  reg  [15:0] evmask;
  reg  [15:0] tstamp;
  wire [15:0] flags = {(FULL != 0) & jabber, sticky[14:13], (FULL != 0) & carrier, sticky[11:8],
                       crc_b_ok, crc_a_ok, sticky[5:4], (rx_cnt != 2'd0), sticky[2:0]};
  assign ev = |(flags & evmask);

  always @(*) begin
    case (p_addr)
      4'd0:    p_rdata = {14'd0, 2'd2 - tx_cnt};
      4'd1:    p_rdata = (rx_cnt != 2'd0) ? rx_buf0 : 16'd0;
      4'd2:    p_rdata = flags;
      4'd3:    p_rdata = evmask;
      4'd4:    p_rdata = {12'd0, tx_st};
      4'd5:    p_rdata = tx_time;
      4'd6:    p_rdata = {rx_words, rx_cnt, rx_lastn};
      4'd7:    p_rdata = crc_a[15:0];
      4'd8:    p_rdata = crc_a[31:16];
      4'd9:    p_rdata = {8'd0, crc_b};
      4'd10:   p_rdata = tstamp;
      4'd11:   p_rdata = {8'd0, rx_st, (FULL != 0) & carrier, x1, aux1, aux0};
      default: p_rdata = 16'd0;
    endcase
  end

  // ----------------------------------------------------------------------------------------------
  // Registers
  // ----------------------------------------------------------------------------------------------
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      c_mode <= 16'd0; c_txper <= 16'd1; c_loop0 <= 16'd0; c_loop1 <= 16'd0; c_loop2 <= 16'd0;
      c_loop3 <= 16'd0; c_pat <= 16'd0; c_pat2 <= 16'd0; c_sel <= 16'hFFFF; c_pa <= 32'd0;
      c_ra <= 32'd0; c_crcb <= 16'd0; c_crcw <= 16'd0; c_tail <= 16'd0; c_lta <= 16'd0;
      c_ltb <= 16'd0; c_ltc <= 16'd0; c_sq <= 16'd0; c_lp <= 16'd0; cfg_adr <= 5'd0;
      evmask <= 16'd0; sticky <= 16'd0; tstamp <= 16'd0;
      din_prev <= 1'b0; q_cnt <= 6'd0; carrier <= 1'b0; sq_gap <= 7'd0; sq_good <= 4'd0;
      lp_w <= 6'd0; lt_a <= 16'd0; lt_b <= 16'd0; lt_a_hit <= 1'b0; lt_b_hit <= 1'b0;
      lp_en <= 1'b0; rx_st <= R_OFF; rx_lvl_prev <= 1'b1; pat_sh <= 16'd0; rx_run <= 3'd0;
      rx_lastbit <= 1'b0; rx_sh <= 16'd0; rx_nb <= 5'd0; rx_words <= 9'd0; rx_bits <= 10'd0;
      rx_buf0 <= 16'd0; rx_buf1 <= 16'd0; rx_cnt <= 2'd0; rx_lastn <= 5'd0;
      crc_a <= 32'd0; crc_b <= 8'd0; crc_a_ok <= 1'b0; crc_b_ok <= 1'b0;
      crc_a_byte <= 1'b0; crc_b_byte <= 1'b0; ck_pend <= 16'd0; ck_bit_v <= 1'b0; ck_bit_d <= 1'b0;
      tx_st <= T_IDLE; tx_buf0 <= 16'd0; tx_buf1 <= 16'd0; tx_lst0 <= 1'b0; tx_lst1 <= 1'b0;
      tx_cnt <= 2'd0; tx_sh <= 16'd0; tx_nb <= 5'd0; tx_lastw <= 1'b0; tx_tmr <= 16'd0;
      tx_half <= 1'b0; tx_bit <= 1'b0; tx_line <= 1'b1; tx_sck <= 1'b0; tx_run <= 3'd0;
      tx_runbit <= 1'b0; tx_crcn <= 6'd0; tx_skipw <= 3'd0; tx_tail <= 8'd0; tx_jam <= 6'd0;
      tx_time <= 16'd0; tx_lastbits <= 5'd0; tx_crcen <= 1'b0; tx_droplate <= 1'b0;
      tx_pulse <= 1'b0; crc_t <= 32'd0; tx_lead <= 1'b0; jab_cnt <= 17'd0; jabber <= 1'b0;
      tx_line_q <= 1'b1; tx_v <= 4'd0; tx_oe <= 1'b0;
    end else begin
      // ---------------- configuration and control ----------------
      if (p_we && p_addr == 4'd12) cfg_adr <= p_wdata[4:0];
      if (p_we && p_addr == 4'd13) begin
        cfg_adr <= cfg_adr + 5'd1;
        case (cfg_adr)
          5'd0:  c_mode  <= p_wdata;
          5'd1:  c_txper <= p_wdata;
          5'd2:  c_loop0 <= p_wdata;
          5'd3:  c_loop1 <= p_wdata;
          5'd4:  c_loop2 <= p_wdata;
          5'd5:  c_loop3 <= p_wdata;
          5'd6:  c_pat   <= p_wdata;
          5'd7:  c_pat2  <= p_wdata;
          5'd8:  c_sel   <= p_wdata;
          5'd9:  c_pa[15:0]  <= p_wdata;
          5'd10: c_pa[31:16] <= p_wdata;
          5'd11: c_ra[15:0]  <= p_wdata;
          5'd12: c_ra[31:16] <= p_wdata;
          5'd13: c_crcb  <= p_wdata;
          5'd14: c_crcw  <= p_wdata;
          5'd15: c_tail  <= p_wdata;
          5'd16: c_lta   <= p_wdata;
          5'd17: c_ltb   <= p_wdata;
          5'd18: c_ltc   <= p_wdata;
          5'd19: c_sq    <= p_wdata;
          5'd20: c_lp    <= p_wdata;
          default: ;
        endcase
      end
      if (p_we && p_addr == 4'd3) evmask <= p_wdata;

      // ---------------- line conditions ----------------
      din_prev <= x1;
      if (m_usb) q_cnt <= se0 ? ((q_cnt == 6'd63) ? 6'd63 : q_cnt + 6'd1) : 6'd0;
      else       q_cnt <= din_rise ? 6'd0 : ((q_cnt == 6'd63) ? 6'd63 : q_cnt + 6'd1);
      if (!ca_hold)     lt_a <= 16'd0;
      else if (lt_tick) lt_a <= (lt_a == 16'hFFFF) ? lt_a : lt_a + 16'd1;
      if (!cb_hold)     lt_b <= 16'd0;
      else if (lt_tick) lt_b <= (lt_b == 16'hFFFF) ? lt_b : lt_b + 16'd1;
      lt_a_hit <= ca_now;
      lt_b_hit <= cb_now;

      if (FULL) begin
        if (din_edge) begin
          if (aux0 && (sq_gap >= {1'b0, c_sq[5:0]}) && (sq_gap <= {1'b0, c_sq[11:6]}))
            sq_good <= (sq_good == 4'hF) ? sq_good : sq_good + 4'd1;
          else
            sq_good <= 4'd0;
          sq_gap <= (m_ddr && (x1 ^ x0)) ? 7'd1 : 7'd2;
        end else begin
          sq_gap <= (sq_gap >= 7'd126) ? 7'd127 : sq_gap + 7'd2;
        end
        if (!carrier && (c_sq[15:12] != 4'd0) && (sq_good >= c_sq[15:12])) begin
          carrier <= 1'b1;
        end else if (carrier && eop_q) begin
          carrier <= 1'b0;
          sq_good <= 4'd0;
        end
        if (aux0 && x1) lp_w <= (lp_w == 6'd63) ? lp_w : lp_w + 6'd1;
        else            lp_w <= 6'd0;
      end

      // ---------------- receive ----------------
      lp_en <= m_rx && (m_code != C_CLK) && (rx_st == R_HUNT || rx_st == R_DATA);
      if (r_dec_v) rx_lvl_prev <= lp_bd;
      if (rx_st == R_HUNT && !lp_init) rx_lvl_prev <= x1;   // first level of a new frame
      case (rx_st)
        R_OFF:  if (m_rx) rx_st <= (m_code == C_CLK) ? R_DATA : R_HUNT;
        R_HUNT: begin
          if (!m_rx) rx_st <= R_OFF;
          else if (r_dec_v) pat_sh <= {pat_sh[14:0], r_dec};
          if (m_rx && r_sop) rx_st <= R_DATA;
        end
        R_DATA: begin
          if (!m_rx) rx_st <= R_OFF;
          else if (r_eop) rx_st <= m_usb ? R_EOPW : (m_code == C_CLK ? R_DATA : R_RST);
        end
        R_EOPW: if (!se0) rx_st <= R_RST;                     // USB: EOP ends at the first J
        R_RST:  rx_st <= m_rx ? R_HUNT : R_OFF;               // one cycle with the loop off
        default: rx_st <= R_OFF;
      endcase
      if (r_sop) begin
        rx_run     <= 3'd0;
        rx_lastbit <= r_dec;
        rx_sh      <= 16'd0;
        rx_nb      <= 5'd0;
        rx_words   <= 9'd0;
        rx_bits    <= 10'd0;
        crc_a      <= crc_init;
        crc_b      <= 8'hFF;
        crc_a_byte <= 1'b0;
        crc_b_byte <= 1'b0;
      end
      if (r_stuff_drop) begin
        rx_run     <= m_ones ? 3'd0 : 3'd1;
        rx_lastbit <= r_dec;
      end
      if (r_data_v) begin
        if (m_ones) rx_run <= r_data ? rx_run + 3'd1 : 3'd0;
        else        rx_run <= (r_data == rx_lastbit) ? rx_run + 3'd1 : 3'd1;
        rx_lastbit <= r_data;
        if (rx_bits >= {4'd0, c_crcw[15:10]}) begin
          crc_a <= crc_step(crc_a, r_data, c_pa) & crc_mask;
          crc_b <= crc_step({24'd0, crc_b}, r_data, {24'd0, c_crcb[7:0]});
        end
        rx_bits <= (rx_bits == 10'h3FF) ? rx_bits : rx_bits + 10'd1;
        rx_sh   <= rx_word;
        rx_nb   <= (rx_nb == 5'd15) ? 5'd0 : rx_nb + 5'd1;
        if (rx_nb == 5'd15) rx_words <= rx_words + 9'd1;
      end
      // CRC residue at each octet boundary of the data (dribble bits do not count)
      if (rx_st == R_DATA && !r_data_v && rx_bits != 10'd0 && rx_bits[2:0] == 3'd0) begin
        crc_a_byte <= ((crc_a ^ c_ra) & crc_mask) == 32'd0;
        crc_b_byte <= (crc_b == c_crcb[15:8]);
      end
      if (r_eop) begin
        rx_lastn <= rx_nb;
        crc_a_ok <= crc_a_byte;
        crc_b_ok <= crc_b_byte;
        rx_nb    <= 5'd0;
      end
      // RX buffer (2 words)
      if (rx_pop) rx_buf0 <= rx_buf1;
      if (rx_push) begin
        if (rx_cnt == 2'd2 && !rx_pop) begin
          sticky[9] <= 1'b1;                                 // word lost
        end else if ((rx_cnt == 2'd0) || (rx_cnt == 2'd1 && rx_pop)) begin
          rx_buf0 <= rx_push_w;
        end else begin
          rx_buf1 <= rx_push_w;
        end
      end
      rx_cnt <= rx_cnt - (rx_pop ? 2'd1 : 2'd0) +
                ((rx_push && !(rx_cnt == 2'd2 && !rx_pop)) ? 2'd1 : 2'd0);

      // Clocked mode: sample DIN at the set delay after each sampling clock edge.
      ck_pend  <= {1'b0, ck_pend[15:1]};
      if (half_mid && m_code == C_CLK && m_rx) ck_pend[c_sel[15:12]] <= 1'b1;
      ck_bit_v <= ck_pend[0];
      ck_bit_d <= x1;

      // ---------------- transmit ----------------
      if (tx_busy || tx_st == T_TAIL1) tx_tmr <= sym_end ? (c_txper - 16'd1) : (tx_tmr - 16'd1);
      if (p_we && p_addr == 4'd4) begin
        tx_lastbits <= p_wdata[4:0];
        tx_crcen    <= p_wdata[5];
        tx_droplate <= p_wdata[6];
        tx_pulse    <= p_wdata[7];
      end
      case (tx_st)
        T_IDLE: begin
          tx_half <= 1'b0;
          tx_line <= 1'b1;
          tx_sck  <= c_ltc[8];
          if (p_we && p_addr == 4'd4 && p_wdata[9]) begin
            tx_st   <= T_ARM;
            tx_time <= now + 16'd4;
          end else if (p_we && p_addr == 4'd5) begin
            if (lead >= 16'd4 && lead <= 16'd32767) begin
              tx_st   <= T_ARM;
              tx_time <= p_wdata;
            end else begin
              sticky[1] <= 1'b1;
              if (!tx_droplate) begin
                tx_st   <= T_ARM;
                tx_time <= now + 16'd4;
              end
            end
          end
        end
        T_ARM: begin
          if (tx_go) begin
            tx_tmr  <= 16'd0;
            tx_half <= 1'b0;
            if (FULL && jabber) begin
              tx_st <= T_IDLE;
            end else if (tx_pulse) begin
              tx_st   <= T_PULSE;
              tx_tail <= {4'd0, c_lp[15:12]};               // link test pulse width (cycles)
            end else if (tx_cnt == 2'd0) begin
              sticky[2] <= 1'b1;
              tx_st     <= T_IDLE;
            end else begin
              tx_st     <= T_DATA;
              tx_sh     <= tx_buf0;
              tx_lastw  <= tx_lst0;
              tx_nb     <= len0;
              tx_run    <= 3'd0;
              tx_runbit <= 1'b0;
              tx_skipw  <= c_crcw[9:7];
              crc_t     <= crc_init;
              tx_crcn   <= crc_w;
            end
          end
        end
        T_DATA, T_CRC, T_JAM: begin
          if (half_mid) begin
            tx_half <= 1'b0;
            if (m_code == C_MAN) tx_line <= tx_bit;          // second half of a Manchester bit
            tx_sck <= c_ltc[8] ^ ~c_ltc[9];                  // clocked: second edge of the bit
          end
          if (bit_start) begin
            if (t_new) begin
              tx_bit  <= t_bit;
              tx_half <= m_half;
              case (m_code)
                C_NRZ:  tx_line <= t_bit;
                C_NRZI: tx_line <= t_bit ? tx_line : ~tx_line;
                C_MAN:  tx_line <= ~t_bit;
                default: tx_line <= t_bit;
              endcase
              tx_sck <= c_ltc[8] ^ c_ltc[9];                 // clocked: first edge of the bit
              if (t_kind != K_JAM) begin
                if (t_kind == K_STUFF) begin
                  tx_run    <= m_ones ? 3'd0 : 3'd1;
                end else if (m_ones) begin
                  tx_run    <= t_bit ? tx_run + 3'd1 : 3'd0;
                end else begin
                  tx_run    <= (t_bit == tx_runbit) ? tx_run + 3'd1 : 3'd1;
                end
                tx_runbit <= t_bit;
              end
            end
            case (t_kind)
              K_DATA: begin
                tx_sh <= tx_sh >> 1;
                tx_nb <= tx_nb - 5'd1;
              end
              K_LOAD: begin
                tx_sh    <= tx_buf0 >> 1;
                tx_nb    <= len0 - 5'd1;
                tx_lastw <= tx_lst0;
                if (tx_skipw != 3'd0) tx_skipw <= tx_skipw - 3'd1;
              end
              K_CRC: begin
                tx_st   <= T_CRC;
                crc_t   <= crc_t >> 1;
                tx_crcn <= tx_crcn - 6'd1;
              end
              K_JAM:   tx_jam <= tx_jam - 6'd1;
              K_UNDER: begin
                sticky[2] <= 1'b1;
                tx_st     <= T_TAIL1;
                tx_tail   <= c_tail[7:0];
              end
              K_END: begin
                tx_st   <= T_TAIL1;
                tx_tail <= c_tail[7:0];
              end
              default: ;
            endcase
            // CRC over data bits from the first word after the skipped ones (a K_LOAD bit belongs
            // to the next word, whose skip count is tx_skipw - 1)
            if (tx_crcen && (((t_kind == K_DATA) && (tx_skipw == 3'd0)) ||
                             ((t_kind == K_LOAD) && (tx_skipw <= 3'd1))))
              crc_t <= crc_step(crc_t, t_bit, c_pa) & crc_mask;
          end
          if (FULL && carrier && (tx_st == T_DATA || tx_st == T_CRC)) begin
            sticky[14] <= 1'b1;                              // collision: jam, then the tail
            tx_st  <= T_JAM;
            tx_jam <= 6'd31;
          end
          if (FULL && jabber) tx_st <= T_IDLE;
        end
        T_TAIL1: begin
          if (tx_tail <= 8'd1) begin                     // tail 1 lasts c_tail[7:0] cycles
            if (m_usb) begin
              tx_st   <= T_TAIL2;
              tx_tail <= c_tail[15:8];
            end else begin
              tx_st     <= T_IDLE;
              sticky[0] <= 1'b1;
            end
          end else begin
            tx_tail <= tx_tail - 8'd1;
          end
        end
        T_TAIL2, T_PULSE: begin
          if (tx_tail <= 8'd1) begin
            tx_st     <= T_IDLE;
            sticky[0] <= 1'b1;
          end else begin
            tx_tail <= tx_tail - 8'd1;
          end
        end
        default: tx_st <= T_IDLE;
      endcase
      if (p_we && p_addr == 4'd4 && p_wdata[8]) tx_st <= T_IDLE;     // abort
      if (tx_go && m_tsel == 2'd3) tstamp <= now;

      // TX buffer (2 words): the engine takes from slot 0, the thread writes behind it.
      if (p_we && p_addr == 4'd4 && p_wdata[10]) begin
        tx_cnt <= 2'd0;
      end else begin
        if (tx_take) begin
          tx_buf0 <= tx_buf1;
          tx_lst0 <= tx_lst1;
        end
        if (tx_wr) begin
          if ((tx_cnt == 2'd0) || (tx_cnt == 2'd1 && tx_take)) begin
            tx_buf0 <= p_wdata;
            tx_lst0 <= (p_addr == 4'd14);
          end else if (tx_cnt == 2'd1 || (tx_cnt == 2'd2 && tx_take)) begin
            tx_buf1 <= p_wdata;
            tx_lst1 <= (p_addr == 4'd14);
          end
        end
        tx_cnt <= tx_cnt - (tx_take ? 2'd1 : 2'd0) +
                  ((tx_wr && !(tx_cnt == 2'd2 && !tx_take)) ? 2'd1 : 2'd0);
      end

      // Jabber (FULL): 512-cycle ticks of transmit time; 8,192 ticks (2^22 cycles, 69.9 ms at
      // 60 MHz; IEEE 802.3 14.2.1.6 xmit_max 20-150 ms) set it; 65,536 idle ticks (2^25 cycles,
      // 0.559 s; unjab 0.25-0.75 s) clear it.
      if (FULL) begin
        if (!jabber) begin
          if (!(tx_busy || tx_st == T_TAIL1 || tx_st == T_TAIL2 || tx_st == T_PULSE))
            jab_cnt <= 17'd0;
          else if (now[8:0] == 9'd0)
            jab_cnt <= jab_cnt + 17'd1;
          if (jab_cnt[13]) begin
            jabber  <= 1'b1;
            jab_cnt <= 17'd0;
          end
        end else begin
          if (now[8:0] == 9'd0) jab_cnt <= jab_cnt + 17'd1;
          if (jab_cnt[16]) begin
            jabber  <= 1'b0;
            jab_cnt <= 17'd0;
          end
        end
      end

      // ---------------- status ----------------
      if (r_sop) begin
        sticky[4] <= 1'b1;
        if (m_tsel == 2'd1) tstamp <= now;
      end
      if (r_stuff_err) sticky[8] <= 1'b1;
      if ((r_eop && !m_usb) || (rx_st == R_EOPW && !se0)) begin
        sticky[5] <= 1'b1;
        if (m_tsel == 2'd0) tstamp <= now;
      end
      if (ca_now && !lt_a_hit) sticky[10] <= 1'b1;
      if (cb_now && !lt_b_hit) sticky[11] <= 1'b1;
      if (FULL && !(aux0 && x1) && (lp_w != 6'd0) && (lp_w >= c_lp[5:0]) && (lp_w <= c_lp[11:6]) &&
          !carrier) begin
        sticky[13] <= 1'b1;
        if (m_tsel == 2'd2) tstamp <= now;
      end
      if (p_we && p_addr == 4'd2) sticky <= sticky & ~p_wdata;

      // ---------------- outputs (registered) ----------------
      tx_line_q <= tx_line;
      tx_lead   <= tx_go;
      case ((tx_lead && tx_st == T_DATA) ? T_IDLE : tx_st)  // first T_DATA cycle: no bit on tx_line yet
        T_DATA, T_CRC, T_JAM: begin
          // (the first cycle of T_DATA loads the first bit; tx_line holds it from the next cycle)
          if (m_usb)                tx_v[1:0] <= {tx_line, ~tx_line};  // VMO = J, VPO = K
          else if (m_code == C_CLK) tx_v[1:0] <= {tx_sck, tx_bit};     // clock, data
          else                      tx_v[1:0] <= {~tx_line, tx_line};  // TD-, TD+ (TXB, TXA)
          tx_oe <= 1'b1;
        end
        T_TAIL1: begin
          if (m_usb)                tx_v[1:0] <= 2'b00;                // SE0
          else if (m_code == C_MAN) tx_v[1:0] <= 2'b01;                // TP_IDL: TD+ held high
          else if (m_code == C_CLK) tx_v[1:0] <= {c_ltc[8], tx_bit};
          else                      tx_v[1:0] <= 2'b11;
          tx_oe <= 1'b1;
        end
        T_TAIL2: begin
          tx_v[1:0] <= 2'b10;                                          // USB J
          tx_oe <= 1'b1;
        end
        T_PULSE: begin
          tx_v[1:0] <= 2'b01;                                          // link test pulse
          tx_oe <= 1'b1;
        end
        default: begin
          if (m_usb)                tx_v[1:0] <= 2'b10;
          else if (m_code == C_MAN) tx_v[1:0] <= 2'b00;
          else if (m_code == C_CLK) tx_v[1:0] <= {c_ltc[8], 1'b0};
          else                      tx_v[1:0] <= 2'b11;
          tx_oe <= 1'b0;
        end
      endcase
      tx_v[3:2] <= tx_v[1:0];      // tap B: the same levels one cycle later
    end
  end

  wire _unused = &{1'b0, m_oe_low, t_dbit};

endmodule
