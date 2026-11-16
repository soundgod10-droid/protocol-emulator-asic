/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Serial line unit (SLU): bit-level work of a serial line at line rate, so that the firmware works
// on words and frames. One unit serves USB low speed (NRZI, bit stuffing, SYNC, EOP, CRC5 and
// CRC16, line states, timed reply), 10BASE-Te (Manchester at 3 cycles per half bit, both-edge
// receive loop, SFD gate, CRC-32, TP_IDL, link test pulses, squelch rules, collision, jam, jabber;
// FULL = 1 only), asynchronous NRZ lines (UART) and clocked lines (SPI-style: clock out, data out,
// data in at a set delay).
//
// Thread interface: 16 I/O ports (IOR, IOW; docs/isa.md section 10), one event output (any status
// flag AND its mask), the pins through the pin configuration. Every output goes to a pin flip-flop
// outside this module, so each chip output still comes from a flip-flop.
//
// Ports (p_addr):
//   0 TXDATA  W push a word (LSB first on the line)   R TX buffer free (0 to 2)
//   1 RXDATA  R pop a word (0 if empty)
//   2 STATUS  R flags                                  W 1 clears a sticky flag
//   3 EVMASK  R/W which flags drive the event output
//   4 TXCTRL  W [4:0] bits of the last word (0 = 16), [5] append CRC, [6] drop a late start,
//               [7] link test pulse instead of a frame, [8] abort, [9] start now, [10] flush
//             R [3:0] TX state
//   5 TXTIME  W start time (now[15:0] compare): lead 4 to 32,767 cycles, else late (STATUS TXLATE;
//               start at write + 4, or no start with TXCTRL[6]); R armed time
//   6 RXINFO  R [4:0] bits of the last word of the frame, [6:5] RX buffer words, [15:7] words
//   7 CRCA_LO R CRC A state [15:0]       8 CRCA_HI R CRC A state [31:16]
//   9 CRCB    R CRC B state [7:0]
//  10 TSTAMP  R now[15:0] at the last selected event (configuration MODE[13:12])
//  11 LINE    R [1:0] AUX1, AUX0 now, [2] DIN now, [3] carrier, [7:4] RX state
//  12 CFGADR  W configuration index (auto-increment after each CFGDAT write)
//  13 CFGDAT  W configuration word (write only)
//  14 TXLAST  W push the last word of the frame (bit count from TXCTRL[4:0])
//
// STATUS flags: 0 TXDONE, 1 TXLATE, 2 TXUNDER, 3 RXWORD (level), 4 RXSOP, 5 RXEOP, 6 CRCA_OK
// (level, last frame), 7 CRCB_OK (level), 8 STUFFERR, 9 RXOVER, 10 LINEA, 11 LINEB, 12 CARRIER
// (level), 13 LINKPULSE, 14 COLLISION, 15 JABBER (level).
//
// Configuration words (index): see the localparams CF_*.

module pe_slu #(
    parameter FULL = 1              // 1: 10BASE-Te blocks (squelch, pulse, collision, jam, jabber)
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] now,
    input  wire        p_we,
    input  wire        p_re,
    input  wire [3:0]  p_addr,
    input  wire [15:0] p_wdata,
    output reg  [15:0] p_rdata,
    input  wire [12:0] pin_raw,      // pad inputs before any synchronizer (DIN capture)
    input  wire [12:0] pin_sync,     // synchronized inputs, s(c) (AUX0, AUX1)
    output reg  [3:0]  tx_v,         // role outputs TXA, TXB, TXC, TXD (to pin flip-flops)
    output reg         tx_oe,        // role output OE (polarity MODE[14])
    output wire        ev
);

  // ---------------------------------------------------------------------------------------------
  // Configuration (write only)
  // ---------------------------------------------------------------------------------------------
  localparam CF_MODE   = 5'd0;   // [1:0] code 0 NRZ 1 NRZI 2 Manchester 3 clocked, [2] ddr, [3] rx on,
                                 // [4] stuffing, [5] stuff counts ones (else equal bits), [8:6] K,
                                 // [9] USB line (J/K/SE0 on TXA/TXB, line states on AUX), [10] EOP by
                                 // carrier off, [11] EOP after a fixed bit count, [13:12] TSTAMP event
                                 // 0 EOP 1 SOP 2 link pulse 3 TX start, [14] OE active low, [15] loopback
  localparam CF_TXPER  = 5'd1;   // TX symbol period in cycles (bit; half bit for Manchester and clocked)
  localparam CF_LOOP0  = 5'd2;   // [5:0] N, [8:6] kp0, [11:9] kp1, [14:12] kp2
  localparam CF_LOOP1  = 5'd3;   // [5:0] gear 1 edges, [11:6] gear 2 edges, [15:12] integrator shift
  localparam CF_LOOP2  = 5'd4;   // [5:0] window, [11:6] acquisition window, [12] integrator on,
                                 // [14:13] edges before the initial sync
  localparam CF_LOOP3  = 5'd5;   // [5:0] acquisition edges, [11:6] edges before the pattern detector
  localparam CF_PAT    = 5'd6;   // pattern, newest received bit in bit 0
  localparam CF_PAT2   = 5'd7;   // [3:0] pattern length - 1, [9:4] fixed frame bits, [15:10] EOP qualifier
  localparam CF_SEL    = 5'd8;   // [3:0] DIN pin, [7:4] AUX0 pin, [11:8] AUX1 pin, [15:12] clocked
                                 // sample delay (cycles after the sampling clock edge)
  localparam CF_PA_LO  = 5'd9;   // CRC A polynomial, reflected (LSB-first serial)
  localparam CF_PA_HI  = 5'd10;
  localparam CF_RA_LO  = 5'd11;  // CRC A residue
  localparam CF_RA_HI  = 5'd12;
  localparam CF_CRCB   = 5'd13;  // [7:0] CRC B polynomial (reflected), [15:8] CRC B residue
  localparam CF_CRCW   = 5'd14;  // [5:0] CRC A width (TX append), [6] CRC A preset 1s, [9:7] TX words
                                 // not in the CRC, [15:10] RX bits after the pattern not in the CRC
  localparam CF_TAIL   = 5'd15;  // [7:0] tail 1 cycles (USB SE0, 10BASE-T TP_IDL hold, pulse width),
                                 // [15:8] tail 2 cycles (USB J)
  localparam CF_LTA    = 5'd16;  // line condition A threshold (prescaled cycles)
  localparam CF_LTB    = 5'd17;  // line condition B threshold
  localparam CF_LTC    = 5'd18;  // [3:0] prescale shift, [5:4] condition A, [7:6] condition B
                                 // (0 SE0, 1 J, 2 K, 3 no DIN edge), [8] clocked CPOL, [9] CPHA
  localparam CF_SQ     = 5'd19;  // squelch: [5:0] min spacing, [11:6] max spacing (half cycles),
                                 // [15:12] spacings in a row for carrier on
  localparam CF_LP     = 5'd20;  // link pulse: [5:0] min width, [11:6] max width (cycles)

  reg [15:0] c_mode, c_txper, c_loop0, c_loop1, c_loop2, c_loop3, c_pat, c_pat2, c_sel;
  reg [31:0] c_pa, c_ra;
  reg [15:0] c_crcb, c_crcw, c_tail, c_lta, c_ltb, c_ltc, c_sq, c_lp;
  reg [4:0]  cfg_adr;

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
  localparam C_NRZ = 2'd0, C_NRZI = 2'd1, C_MAN = 2'd2, C_CLK = 2'd3;

  // ---------------------------------------------------------------------------------------------
  // Inputs: DIN captured on both clock edges, AUX0/AUX1 from the chip synchronizers
  // ---------------------------------------------------------------------------------------------
  wire din_pin = c_sel[3:0] < 4'd13 ? pin_raw[c_sel[3:0]] : 1'b0;
  wire aux0    = c_sel[7:4] < 4'd13 ? pin_sync[c_sel[7:4]] : 1'b0;
  wire aux1    = c_sel[11:8] < 4'd13 ? pin_sync[c_sel[11:8]] : 1'b0;

  reg din_f1;                      // falling-edge sample
  always @(negedge clk or negedge rst_n)
    if (!rst_n) din_f1 <= 1'b0; else din_f1 <= din_pin;

  reg din_f2, din_f3, din_r1, din_r2;
  reg tx_line_q;                   // loopback source: the encoded TX line, registered
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
  // x0: the falling-edge sample half a cycle before x1 (both 2 cycles behind the pin, as s(c)).
  wire x0 = m_loop ? tx_line_q : din_f3;
  wire x1 = m_loop ? tx_line_q : din_r2;

  // ---------------------------------------------------------------------------------------------
  // Receive loop
  // ---------------------------------------------------------------------------------------------
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

  // ---------------------------------------------------------------------------------------------
  // Line conditions: USB line state, EOP qualifier, carrier (squelch rules), link pulses, timers
  // ---------------------------------------------------------------------------------------------
  wire       se0     = ~aux0 & ~aux1;              // USB: VP = VM = 0
  wire [1:0] lstate  = {aux0, aux1};                // 00 SE0, 01 J (VP0 VM1), 10 K, 11 SE1
  reg  [1:0] lstate_q;
  reg  [5:0] q_cnt;                                 // samples of SE0 (USB) or since a rising DIN edge
  reg        din_prev;
  wire       din_rise = m_ddr ? ((x0 & ~din_prev) | (x1 & ~x0)) : (x1 & ~din_prev);
  wire       din_edge = m_ddr ? ((x0 ^ din_prev) | (x1 ^ x0)) : (x1 ^ din_prev);
  wire [5:0] q_thr   = c_pat2[15:10];
  wire       eop_q   = (q_cnt >= q_thr);            // SE0 qualified, or idle (no rising edge)

  // Squelch qualifier and link-pulse classifier (FULL).
  reg        carrier;
  reg  [6:0] sq_gap;                                // half-cycle samples since the last DIN edge
  reg  [3:0] sq_good;
  reg  [5:0] lp_w;                                  // cycles of a positive pulse (AUX0 and DIN)
  reg        lp_ev;

  // Line timers A and B (prescaled).
  reg  [15:0] lt_a, lt_b;
  reg         lt_a_hit, lt_b_hit;
  wire [3:0]  lt_sh = c_ltc[3:0];
  wire        lt_tick = ((now & ((16'd1 << lt_sh) - 16'd1)) == 16'd0);
  function cond_hold(input [1:0] c, input [1:0] ls, input de);
    case (c)
      2'd0: cond_hold = (ls == 2'b00);
      2'd1: cond_hold = (ls == 2'b01);
      2'd2: cond_hold = (ls == 2'b10);
      default: cond_hold = ~de;
    endcase
  endfunction
  wire ca_hold = cond_hold(c_ltc[5:4], lstate, din_edge);
  wire cb_hold = cond_hold(c_ltc[7:6], lstate, din_edge);

  // ---------------------------------------------------------------------------------------------
  // CRC engines (LSB-first serial, reflected polynomial)
  // ---------------------------------------------------------------------------------------------
  reg  [31:0] crc_a;     // RX
  reg  [7:0]  crc_b;     // RX, second polynomial (USB CRC5 beside CRC16)
  reg  [31:0] crc_t;     // TX
  wire [31:0] crc_init = c_crcw[6] ? 32'hFFFFFFFF : 32'h0;
  wire [5:0]  crc_w    = c_crcw[5:0];
  wire [31:0] crc_mask = (crc_w >= 6'd32) ? 32'hFFFFFFFF : ((32'd1 << crc_w) - 32'd1);

  // ---------------------------------------------------------------------------------------------
  // Receive datapath
  // ---------------------------------------------------------------------------------------------
  localparam R_OFF = 4'd0, R_HUNT = 4'd1, R_DATA = 4'd2, R_EOPW = 4'd3;
  reg  [3:0]  rx_st;
  reg         rx_lvl_prev;          // previous decided level (NRZI decode)
  reg  [15:0] pat_sh;
  reg  [2:0]  rx_run;               // stuffing run
  reg         rx_lastbit;
  reg  [15:0] rx_sh;
  reg  [4:0]  rx_nb;                // bits in rx_sh
  reg  [8:0]  rx_words;
  reg  [9:0]  rx_bits;              // data bits since the pattern (saturating), for skip and length
  reg  [15:0] rx_buf0, rx_buf1;
  reg  [1:0]  rx_cnt;
  reg  [4:0]  rx_lastn;
  reg         crc_a_ok, crc_b_ok, crc_a_byte, crc_b_byte;

  wire [3:0]  pat_len = c_pat2[3:0];
  wire [15:0] pat_mask = (pat_len == 4'd15) ? 16'hFFFF : ((16'd1 << (pat_len + 4'd1)) - 16'd1);
  wire [5:0]  rx_fixed = c_pat2[9:4];
  wire [5:0]  rx_skip  = c_crcw[15:10];
  wire        hunt_ok  = (lp_nacc >= c_loop3[11:6]);

  // ---------------------------------------------------------------------------------------------
  // Transmit datapath
  // ---------------------------------------------------------------------------------------------
  localparam T_IDLE = 4'd0, T_ARM = 4'd1, T_DATA = 4'd2, T_CRC = 4'd3, T_TAIL1 = 4'd4,
             T_TAIL2 = 4'd5, T_PULSE = 4'd6, T_JAM = 4'd7;
  reg  [3:0]  tx_st;
  reg  [15:0] tx_buf0, tx_buf1;
  reg         tx_lst0, tx_lst1;
  reg  [1:0]  tx_cnt;
  reg  [15:0] tx_sh;
  reg  [4:0]  tx_nb;                // bits left in tx_sh
  reg         tx_lastw;             // tx_sh is the last word
  reg  [15:0] tx_tmr;               // symbol timer
  reg         tx_half;              // Manchester / clocked: second half of the bit
  reg         tx_bit;               // bit on the line (before line coding)
  reg         tx_line;              // coded line level (NRZI state, Manchester half)
  reg  [2:0]  tx_run;
  reg         tx_runbit;
  reg  [5:0]  tx_crcn;              // CRC bits left
  reg  [2:0]  tx_skipw;             // words left that are not in the CRC
  reg  [7:0]  tx_tail;
  reg  [5:0]  tx_jam;
  reg  [15:0] tx_time;
  reg  [4:0]  tx_lastbits;
  reg         tx_crcen, tx_droplate, tx_pulse;
  reg         tx_sck;               // clocked mode: clock level
  reg  [3:0]  ck_dly;               // clocked mode: sample delay counter
  reg         ck_pend;

  // Jabber (FULL): TX time limit 2^22 cycles (69.9 ms), unjab after 2^25 idle cycles (0.559 s).
  reg  [16:0] jab_cnt;
  reg         jabber;

  // ---------------------------------------------------------------------------------------------
  // Status, events, time stamp
  // ---------------------------------------------------------------------------------------------
  reg  [15:0] st_sticky;            // sticky flags (bits 0-2, 4, 5, 8-11, 13, 14)
  reg  [15:0] evmask;
  reg  [15:0] tstamp;
  wire [15:0] flags = {FULL ? jabber : 1'b0, st_sticky[14:13], FULL ? carrier : 1'b0,
                       st_sticky[11:8], crc_b_ok, crc_a_ok, st_sticky[5:4], (rx_cnt != 2'd0),
                       st_sticky[2:0]};
  assign ev = |(flags & evmask);

  // Port reads (combinational; RXDATA pops at the clock edge of the IOR).
  always @(*) begin
    case (p_addr)
      4'd0:  p_rdata = {14'd0, 2'd2 - tx_cnt};
      4'd1:  p_rdata = (rx_cnt != 2'd0) ? rx_buf0 : 16'd0;
      4'd2:  p_rdata = flags;
      4'd3:  p_rdata = evmask;
      4'd4:  p_rdata = {12'd0, tx_st};
      4'd5:  p_rdata = tx_time;
      4'd6:  p_rdata = {rx_words, rx_cnt, rx_lastn};
      4'd7:  p_rdata = crc_a[15:0];
      4'd8:  p_rdata = crc_a[31:16];
      4'd9:  p_rdata = {8'd0, crc_b};
      4'd10: p_rdata = tstamp;
      4'd11: p_rdata = {8'd0, rx_st, FULL ? carrier : 1'b0, x1, aux1, aux0};
      default: p_rdata = 16'd0;
    endcase
  end

  // ---------------------------------------------------------------------------------------------
  // Sequential logic
  // ---------------------------------------------------------------------------------------------
  wire        w_tx    = p_we && (p_addr == 4'd0 || p_addr == 4'd14);
  wire        tx_pop;                         // TX engine takes a word from the buffer
  wire [15:0] lead    = p_wdata - now;
  wire        tx_go   = (tx_st == T_ARM) && (now == tx_time);
  wire        sym_end = (tx_tmr == 16'd0);

  // Next data bit for the TX engine (stuffing first, then data, then CRC).
  wire        need_stuff = m_stuff && (tx_run == m_k);
  wire        stuff_bit  = m_ones ? 1'b0 : ~tx_runbit;

  reg         rx_bit_v;      // a data bit (after NRZI decode and destuffing) this cycle
  reg         rx_bit_d;

  assign tx_pop = (tx_st == T_DATA || tx_st == T_ARM) && 1'b0;   // (words are taken in the always block)

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      c_mode <= 16'd0; c_txper <= 16'd0; c_loop0 <= 16'd0; c_loop1 <= 16'd0; c_loop2 <= 16'd0;
      c_loop3 <= 16'd0; c_pat <= 16'd0; c_pat2 <= 16'd0; c_sel <= 16'hFFFF; c_pa <= 32'd0;
      c_ra <= 32'd0; c_crcb <= 16'd0; c_crcw <= 16'd0; c_tail <= 16'd0; c_lta <= 16'd0;
      c_ltb <= 16'd0; c_ltc <= 16'd0; c_sq <= 16'd0; c_lp <= 16'd0; cfg_adr <= 5'd0;
      evmask <= 16'd0; st_sticky <= 16'd0; tstamp <= 16'd0;
      lp_en <= 1'b0; lstate_q <= 2'b00; q_cnt <= 6'd0; din_prev <= 1'b0;
      carrier <= 1'b0; sq_gap <= 7'd0; sq_good <= 4'd0; lp_w <= 6'd0; lp_ev <= 1'b0;
      lt_a <= 16'd0; lt_b <= 16'd0; lt_a_hit <= 1'b0; lt_b_hit <= 1'b0;
      crc_a <= 32'd0; crc_b <= 8'd0; crc_t <= 32'd0;
      rx_st <= R_OFF; rx_lvl_prev <= 1'b1; pat_sh <= 16'd0; rx_run <= 3'd0; rx_lastbit <= 1'b0;
      rx_sh <= 16'd0; rx_nb <= 5'd0; rx_words <= 9'd0; rx_bits <= 10'd0;
      rx_buf0 <= 16'd0; rx_buf1 <= 16'd0; rx_cnt <= 2'd0; rx_lastn <= 5'd0;
      crc_a_ok <= 1'b0; crc_b_ok <= 1'b0; crc_a_byte <= 1'b0; crc_b_byte <= 1'b0;
      tx_st <= T_IDLE; tx_buf0 <= 16'd0; tx_buf1 <= 16'd0; tx_lst0 <= 1'b0; tx_lst1 <= 1'b0;
      tx_cnt <= 2'd0; tx_sh <= 16'd0; tx_nb <= 5'd0; tx_lastw <= 1'b0; tx_tmr <= 16'd0;
      tx_half <= 1'b0; tx_bit <= 1'b0; tx_line <= 1'b1; tx_run <= 3'd0; tx_runbit <= 1'b0;
      tx_crcn <= 6'd0; tx_skipw <= 3'd0; tx_tail <= 8'd0; tx_jam <= 6'd0; tx_time <= 16'd0;
      tx_lastbits <= 5'd0; tx_crcen <= 1'b0; tx_droplate <= 1'b0; tx_pulse <= 1'b0;
      tx_sck <= 1'b0; ck_dly <= 4'd0; ck_pend <= 1'b0; tx_line_q <= 1'b0;
      jab_cnt <= 17'd0; jabber <= 1'b0;
      tx_v <= 4'd0; tx_oe <= 1'b0;
    end else begin
      // ---------------- configuration and control ports ----------------
      if (p_we && p_addr == 4'd12) cfg_adr <= p_wdata[4:0];
      if (p_we && p_addr == 4'd13) begin
        cfg_adr <= cfg_adr + 5'd1;
        case (cfg_adr)
          CF_MODE:  c_mode  <= p_wdata;
          CF_TXPER: c_txper <= p_wdata;
          CF_LOOP0: c_loop0 <= p_wdata;
          CF_LOOP1: c_loop1 <= p_wdata;
          CF_LOOP2: c_loop2 <= p_wdata;
          CF_LOOP3: c_loop3 <= p_wdata;
          CF_PAT:   c_pat   <= p_wdata;
          CF_PAT2:  c_pat2  <= p_wdata;
          CF_SEL:   c_sel   <= p_wdata;
          CF_PA_LO: c_pa[15:0]  <= p_wdata;
          CF_PA_HI: c_pa[31:16] <= p_wdata;
          CF_RA_LO: c_ra[15:0]  <= p_wdata;
          CF_RA_HI: c_ra[31:16] <= p_wdata;
          CF_CRCB:  c_crcb  <= p_wdata;
          CF_CRCW:  c_crcw  <= p_wdata;
          CF_TAIL:  c_tail  <= p_wdata;
          CF_LTA:   c_lta   <= p_wdata;
          CF_LTB:   c_ltb   <= p_wdata;
          CF_LTC:   c_ltc   <= p_wdata;
          CF_SQ:    c_sq    <= p_wdata;
          CF_LP:    c_lp    <= p_wdata;
          default: ;
        endcase
      end
      if (p_we && p_addr == 4'd3) evmask <= p_wdata;
      if (p_we && p_addr == 4'd2) st_sticky <= st_sticky & ~p_wdata;

      // ---------------- line conditions ----------------
      lstate_q <= lstate;
      din_prev <= x1;
      if (m_usb) begin
        q_cnt <= se0 ? ((q_cnt == 6'd63) ? 6'd63 : q_cnt + 6'd1) : 6'd0;
      end else begin
        q_cnt <= din_rise ? 6'd0 : ((q_cnt == 6'd63) ? 6'd63 : q_cnt + 6'd1);
      end
      if (lt_tick) begin
        lt_a <= ca_hold ? ((lt_a == 16'hFFFF) ? lt_a : lt_a + 16'd1) : 16'd0;
        lt_b <= cb_hold ? ((lt_b == 16'hFFFF) ? lt_b : lt_b + 16'd1) : 16'd0;
      end else begin
        if (!ca_hold) lt_a <= 16'd0;
        if (!cb_hold) lt_b <= 16'd0;
      end
      lt_a_hit <= ca_hold && (lt_a >= c_lta) && (c_lta != 16'd0);
      lt_b_hit <= cb_hold && (lt_b >= c_ltb) && (c_ltb != 16'd0);
      if (ca_hold && (lt_a >= c_lta) && (c_lta != 16'd0) && !lt_a_hit) st_sticky[10] <= 1'b1;
      if (cb_hold && (lt_b >= c_ltb) && (c_ltb != 16'd0) && !lt_b_hit) st_sticky[11] <= 1'b1;

      if (FULL) begin
        // Carrier: a run of DIN edge spacings inside [min, max] half cycles while AUX0 (squelch
        // comparator) is active; off when no rising edge for the EOP qualifier time.
        if (din_edge) begin
          if (aux0 && (sq_gap >= {1'b0, c_sq[5:0]}) && (sq_gap <= {1'b0, c_sq[11:6]})) begin
            if (sq_good != 4'hF) sq_good <= sq_good + 4'd1;
          end else begin
            sq_good <= 4'd0;
          end
          sq_gap <= (m_ddr && (x1 ^ x0)) ? 7'd1 : 7'd2;
        end else begin
          sq_gap <= (sq_gap >= 7'd126) ? 7'd127 : sq_gap + 7'd2;
        end
        if (!carrier && (sq_good >= c_sq[15:12]) && (c_sq[15:12] != 4'd0)) carrier <= 1'b1;
        else if (carrier && eop_q) begin
          carrier <= 1'b0;
          sq_good <= 4'd0;
        end
        // Link test pulse: AUX0 and DIN high for [min, max] cycles, no carrier.
        lp_ev <= 1'b0;
        if (aux0 && x1) begin
          if (lp_w != 6'd63) lp_w <= lp_w + 6'd1;
        end else begin
          if ((lp_w >= c_lp[5:0]) && (lp_w <= c_lp[11:6]) && (lp_w != 6'd0) && !carrier) begin
            st_sticky[13] <= 1'b1;
            lp_ev <= 1'b1;
            if (m_tsel == 2'd2) tstamp <= now;
          end
          lp_w <= 6'd0;
        end
      end

      // ---------------- receive ----------------
      rx_bit_v = 1'b0;
      rx_bit_d = 1'b0;
      lp_en <= m_rx && (rx_st != R_EOPW);
      case (rx_st)
        R_OFF: begin
          if (m_rx) rx_st <= R_HUNT;
          pat_sh <= 16'd0;
        end
        R_HUNT, R_DATA: begin
          if (!m_rx) begin
            rx_st <= R_OFF;
          end else if (lp_bv && !(m_usb && se0)) begin
            // decoded bit: NRZI 1 = no change; Manchester and NRZ: the decided level
            rx_lvl_prev <= lp_bd;
            rx_bit_d = (m_code == C_NRZI) ? (lp_bd == rx_lvl_prev) : lp_bd;
            if (rx_st == R_HUNT) begin
              pat_sh <= {pat_sh[14:0], rx_bit_d};
              if (hunt_ok && ((({pat_sh[14:0], rx_bit_d} ^ c_pat) & pat_mask) == 16'd0)) begin
                rx_st <= R_DATA;
                st_sticky[4] <= 1'b1;
                if (m_tsel == 2'd1) tstamp <= now;
                rx_run <= 3'd0;
                rx_lastbit <= rx_bit_d;
                rx_sh <= 16'd0;
                rx_nb <= 5'd0;
                rx_words <= 9'd0;
                rx_bits <= 10'd0;
                crc_a <= crc_init;
                crc_b <= 8'hFF;
                crc_a_byte <= 1'b0;
                crc_b_byte <= 1'b0;
              end
            end else begin
              // destuffing: after K bits in a run the next bit is a stuff bit
              if (m_stuff && (rx_run == m_k)) begin
                if (m_ones ? rx_bit_d : (rx_bit_d == rx_lastbit)) st_sticky[8] <= 1'b1;
                rx_run <= 3'd1;
                rx_lastbit <= rx_bit_d;
                if (m_ones) rx_run <= 3'd0;
              end else begin
                rx_bit_v = 1'b1;
                if (m_ones) rx_run <= rx_bit_d ? rx_run + 3'd1 : 3'd0;
                else rx_run <= (rx_bit_d == rx_lastbit) ? rx_run + 3'd1 : 3'd1;
                rx_lastbit <= rx_bit_d;
              end
            end
          end
          if (rx_bit_v) begin
            // CRC (after the skipped bits), byte-boundary residue checks
            if (rx_bits >= {4'd0, rx_skip}) begin
              crc_a <= ((crc_a >> 1) ^ (((crc_a[0] ^ rx_bit_d)) ? c_pa : 32'd0)) & crc_mask;
              crc_b <= (crc_b >> 1) ^ (((crc_b[0] ^ rx_bit_d)) ? c_crcb[7:0] : 8'd0);
            end
            if (rx_bits != 10'h3FF) rx_bits <= rx_bits + 10'd1;
            rx_sh <= {rx_bit_d, rx_sh[15:1]};
            if (rx_nb == 5'd15) begin
              rx_nb <= 5'd0;
              rx_words <= rx_words + 9'd1;
              if (rx_cnt == 2'd2) st_sticky[9] <= 1'b1;
              else if (rx_cnt == 2'd1) rx_buf1 <= {rx_bit_d, rx_sh[15:1]};
              else rx_buf0 <= {rx_bit_d, rx_sh[15:1]};
            end else begin
              rx_nb <= rx_nb + 5'd1;
            end
          end
          // residue at each octet boundary (dribble bits after the last octet do not count)
          if (rx_st == R_DATA && rx_bits[2:0] == 3'd0 && rx_bits != 10'd0) begin
            crc_a_byte <= ((crc_a ^ c_ra) & crc_mask) == 32'd0;
            crc_b_byte <= (crc_b == c_crcb[15:8]);
          end
          // end of frame
          if (rx_st == R_DATA && ((m_usb && eop_q) || (m_eop_car && eop_q && !m_usb) ||
                                  (m_eop_len && rx_bits >= {4'd0, rx_fixed}))) begin
            rx_st <= m_usb ? R_EOPW : R_HUNT;
            rx_lastn <= rx_nb;
            crc_a_ok <= crc_a_byte;
            crc_b_ok <= crc_b_byte;
            if (rx_nb != 5'd0) begin
              if (rx_cnt == 2'd2) st_sticky[9] <= 1'b1;
              else if (rx_cnt == 2'd1) rx_buf1 <= rx_sh >> (5'd16 - rx_nb);
              else rx_buf0 <= rx_sh >> (5'd16 - rx_nb);
            end
            if (!m_usb) begin
              st_sticky[5] <= 1'b1;
              if (m_tsel == 2'd0) tstamp <= now;
              pat_sh <= 16'd0;
            end
          end
        end
        R_EOPW: begin
          // USB: EOP ends at the first sample that is not SE0 (the J); the reply time counts from it
          if (!se0) begin
            rx_st <= R_HUNT;
            st_sticky[5] <= 1'b1;
            if (m_tsel == 2'd0) tstamp <= now;
            pat_sh <= 16'd0;
            rx_lvl_prev <= 1'b1;
          end
        end
        default: rx_st <= R_OFF;
      endcase
      if (rx_st == R_HUNT && !lp_init) rx_lvl_prev <= x1;

      // RX buffer: count update from pushes this cycle and a pop by IOR RXDATA
      begin : rxcount
        reg push, push2, pop;
        push  = rx_bit_v && (rx_nb == 5'd15) && (rx_cnt != 2'd2);
        push2 = (rx_st == R_DATA) && (rx_nb != 5'd0) &&
                ((m_usb && eop_q) || (m_eop_car && eop_q && !m_usb) ||
                 (m_eop_len && rx_bits >= {4'd0, rx_fixed})) && (rx_cnt != 2'd2);
        pop   = p_re && (p_addr == 4'd1) && (rx_cnt != 2'd0);
        if (pop) begin
          rx_buf0 <= rx_buf1;
          if (push || push2) begin
            // the pushed word goes into the slot that becomes free
            if (rx_cnt == 2'd2) rx_buf1 <= push ? {rx_bit_d, rx_sh[15:1]} : (rx_sh >> (5'd16 - rx_nb));
            else rx_buf0 <= push ? {rx_bit_d, rx_sh[15:1]} : (rx_sh >> (5'd16 - rx_nb));
          end
          rx_cnt <= rx_cnt - 2'd1 + ((push || push2) ? 2'd1 : 2'd0);
        end else if (push || push2) begin
          rx_cnt <= rx_cnt + 2'd1;
        end
      end

      // ---------------- transmit ----------------
      // TXCTRL
      if (p_we && p_addr == 4'd4) begin
        tx_lastbits <= p_wdata[4:0];
        tx_crcen    <= p_wdata[5];
        tx_droplate <= p_wdata[6];
        tx_pulse    <= p_wdata[7];
        if (p_wdata[10]) begin
          tx_cnt <= 2'd0;
        end
        if (p_wdata[8]) begin
          tx_st <= T_IDLE;
        end else if (p_wdata[9] && tx_st == T_IDLE) begin
          tx_st   <= T_ARM;
          tx_time <= now + 16'd4;
        end
      end
      // TXTIME
      if (p_we && p_addr == 4'd5 && tx_st == T_IDLE) begin
        if (lead >= 16'd4 && lead <= 16'd32767) begin
          tx_time <= p_wdata;
          tx_st   <= T_ARM;
        end else begin
          st_sticky[1] <= 1'b1;
          if (!tx_droplate) begin
            tx_time <= now + 16'd4;
            tx_st   <= T_ARM;
          end
        end
      end

      // TX engine
      tx_tmr <= sym_end ? 16'd0 : tx_tmr - 16'd1;
      case (tx_st)
        T_IDLE: begin
          tx_line <= (m_code == C_NRZI) ? 1'b1 : 1'b1;
          tx_half <= 1'b0;
          tx_sck  <= c_ltc[8];
        end
        T_ARM: begin
          if (tx_go) begin
            if (m_tsel == 2'd3) tstamp <= now;
            if (FULL && jabber) begin
              tx_st <= T_IDLE;
            end else if (tx_pulse) begin
              tx_st   <= T_PULSE;
              tx_tail <= c_tail[7:0];
            end else if (tx_cnt == 2'd0) begin
              st_sticky[2] <= 1'b1;
              tx_st <= T_IDLE;
            end else begin
              tx_st    <= T_DATA;
              tx_sh    <= tx_buf0;
              tx_lastw <= tx_lst0;
              tx_nb    <= (tx_lst0 && tx_lastbits != 5'd0) ? tx_lastbits : 5'd16;
              tx_buf0  <= tx_buf1;
              tx_lst0  <= tx_lst1;
              tx_cnt   <= tx_cnt - 2'd1;
              tx_run   <= 3'd0;
              tx_runbit <= 1'b0;
              tx_skipw <= c_crcw[9:7];
              crc_t    <= crc_init;
              tx_tmr   <= 16'd0;
              tx_half  <= 1'b0;
              tx_line  <= 1'b1;
              tx_crcn  <= 6'd0;
            end
          end
        end
        T_DATA, T_CRC, T_JAM: begin
          if (sym_end) begin
            tx_tmr <= c_txper - 16'd1;
            if ((m_code == C_MAN || m_code == C_CLK) && tx_half) begin
              // second half of the bit
              tx_half <= 1'b0;
              if (m_code == C_MAN) tx_line <= tx_bit;
              else tx_sck <= ~tx_sck;
            end else begin
              // a new bit
              tx_half <= 1'b1;
              if (tx_st == T_JAM) begin
                tx_bit <= ~tx_bit;
                if (tx_jam == 6'd0) begin
                  tx_st   <= T_TAIL1;
                  tx_tail <= c_tail[7:0];
                end else begin
                  tx_jam <= tx_jam - 6'd1;
                end
              end else if (need_stuff) begin
                tx_bit    <= stuff_bit;
                tx_run    <= m_ones ? 3'd0 : 3'd1;
                tx_runbit <= stuff_bit;
              end else if (tx_st == T_CRC) begin
                tx_bit    <= ~crc_t[0];
                crc_t     <= crc_t >> 1;
                tx_run    <= m_ones ? (~crc_t[0] ? tx_run + 3'd1 : 3'd0)
                                    : ((~crc_t[0] == tx_runbit) ? tx_run + 3'd1 : 3'd1);
                tx_runbit <= ~crc_t[0];
                if (tx_crcn == 6'd1) tx_st <= T_TAIL1;
                tx_crcn   <= tx_crcn - 6'd1;
                tx_tail   <= c_tail[7:0];
              end else if (tx_nb == 5'd0) begin
                // no bit left: end of data (handled below at the next symbol)
                tx_bit <= tx_bit;
              end else begin
                tx_bit    <= tx_sh[0];
                tx_sh     <= tx_sh >> 1;
                tx_nb     <= tx_nb - 5'd1;
                tx_run    <= m_ones ? (tx_sh[0] ? tx_run + 3'd1 : 3'd0)
                                    : ((tx_sh[0] == tx_runbit) ? tx_run + 3'd1 : 3'd1);
                tx_runbit <= tx_sh[0];
                if (tx_crcen && tx_skipw == 3'd0)
                  crc_t <= (crc_t >> 1) ^ ((crc_t[0] ^ tx_sh[0]) ? c_pa : 32'd0);
                if (tx_nb == 5'd1) begin
                  // the word ends with this bit: next word, CRC or tail
                  if (tx_skipw != 3'd0) tx_skipw <= tx_skipw - 3'd1;
                  if (tx_lastw) begin
                    if (tx_crcen) begin
                      tx_st   <= T_CRC;
                      tx_crcn <= crc_w;
                    end else begin
                      tx_st   <= T_TAIL1;
                      tx_tail <= c_tail[7:0];
                    end
                  end else if (tx_cnt != 2'd0) begin
                    tx_sh    <= tx_buf0;
                    tx_lastw <= tx_lst0;
                    tx_nb    <= (tx_lst0 && tx_lastbits != 5'd0) ? tx_lastbits : 5'd16;
                    tx_buf0  <= tx_buf1;
                    tx_lst0  <= tx_lst1;
                    tx_cnt   <= tx_cnt - 2'd1;
                  end else begin
                    st_sticky[2] <= 1'b1;        // underrun: end the frame
                    tx_st   <= T_TAIL1;
                    tx_tail <= c_tail[7:0];
                  end
                end
              end
              if (m_code == C_MAN) tx_line <= ~(need_stuff ? stuff_bit : tx_sh[0]);
              if (m_code == C_NRZI && !need_stuff && !(tx_st == T_CRC) && (tx_st != T_JAM))
                tx_line <= tx_sh[0] ? tx_line : ~tx_line;
              if (m_code == C_NRZI && need_stuff) tx_line <= ~tx_line;
              if (m_code == C_NRZI && tx_st == T_CRC && !need_stuff)
                tx_line <= ~crc_t[0] ? tx_line : ~tx_line;
              if (m_code == C_CLK) tx_sck <= ~tx_sck;
            end
          end
          // collision: jam 32 bits, then the tail (FULL)
          if (FULL && carrier && (tx_st == T_DATA || tx_st == T_CRC)) begin
            st_sticky[14] <= 1'b1;
            tx_st  <= T_JAM;
            tx_jam <= 6'd31;
          end
          if (FULL && jabber) tx_st <= T_IDLE;
        end
        T_TAIL1: begin
          // wait for the last symbol to end, then tail 1 (SE0 / TP_IDL hold), then tail 2 (USB J)
          if (sym_end && tx_half && (m_code == C_MAN || m_code == C_CLK)) begin
            tx_tmr  <= c_txper - 16'd1;
            tx_half <= 1'b0;
            if (m_code == C_MAN) tx_line <= tx_bit;
            else tx_sck <= ~tx_sck;
          end else if (sym_end) begin
            if (tx_tail == 8'd0) begin
              if (m_usb) begin
                tx_st   <= T_TAIL2;
                tx_tail <= c_tail[15:8];
              end else begin
                tx_st <= T_IDLE;
                st_sticky[0] <= 1'b1;
              end
            end else begin
              tx_tail <= tx_tail - 8'd1;
            end
          end
        end
        T_TAIL2: begin
          if (tx_tail == 8'd0) begin
            tx_st <= T_IDLE;
            st_sticky[0] <= 1'b1;
          end else begin
            tx_tail <= tx_tail - 8'd1;
          end
        end
        T_PULSE: begin
          if (tx_tail == 8'd0) begin
            tx_st <= T_IDLE;
            st_sticky[0] <= 1'b1;
          end else begin
            tx_tail <= tx_tail - 8'd1;
          end
        end
        default: tx_st <= T_IDLE;
      endcase

      // TX buffer pushes (after the engine's take in the same cycle)
      if (w_tx) begin : txpush
        reg [1:0] c;
        c = tx_cnt;
        // the engine took a word this cycle if tx_cnt was decremented above: use the updated count
        if ((tx_st == T_ARM && tx_go && !tx_pulse && tx_cnt != 2'd0) ||
            ((tx_st == T_DATA) && sym_end && !((m_code == C_MAN || m_code == C_CLK) && tx_half) &&
             !need_stuff && tx_nb == 5'd1 && !tx_lastw && tx_cnt != 2'd0))
          c = tx_cnt - 2'd1;
        if (c == 2'd0) begin
          if (tx_st == T_ARM && tx_go && !tx_pulse && tx_cnt != 2'd0) begin
            tx_buf0 <= p_wdata; tx_lst0 <= (p_addr == 4'd14);
          end else begin
            tx_buf0 <= p_wdata; tx_lst0 <= (p_addr == 4'd14);
          end
          tx_cnt <= c + 2'd1;
        end else if (c == 2'd1) begin
          tx_buf1 <= p_wdata; tx_lst1 <= (p_addr == 4'd14);
          tx_cnt <= c + 2'd1;
        end
      end

      // Jabber (FULL)
      if (FULL) begin
        if (now[8:0] == 9'd0) begin
          if (tx_st != T_IDLE && tx_st != T_ARM) begin
            if (!jabber) jab_cnt <= (jab_cnt[13]) ? jab_cnt : jab_cnt + 17'd1;
          end else if (jabber) begin
            jab_cnt <= jab_cnt + 17'd1;
          end else begin
            jab_cnt <= 17'd0;
          end
        end
        if (!jabber && jab_cnt[13]) begin            // 8,192 ticks of 512 cycles = 2^22 cycles
          jabber  <= 1'b1;
          jab_cnt <= 17'd0;
        end else if (jabber && jab_cnt[16]) begin    // 65,536 ticks = 2^25 cycles idle
          jabber  <= 1'b0;
          jab_cnt <= 17'd0;
        end
        if (jabber && (tx_st != T_IDLE && tx_st != T_ARM)) jab_cnt <= 17'd0;
      end

      // ---------------- outputs (registered) ----------------
      tx_line_q <= tx_line;
      case (tx_st)
        T_DATA, T_CRC, T_JAM: begin
          if (m_usb) begin
            tx_v[0] <= ~tx_line;            // VPO: K
            tx_v[1] <= tx_line;             // VMO: J
          end else if (m_code == C_CLK) begin
            tx_v[0] <= tx_bit;              // data
            tx_v[1] <= tx_sck;              // clock
          end else begin
            tx_v[0] <= tx_line;             // TD+ / TXD
            tx_v[1] <= ~tx_line;            // TD-
          end
          tx_oe <= 1'b1;
        end
        T_TAIL1: begin
          if (m_usb) begin
            if (sym_end && !tx_half) begin  // SE0 from the end of the last bit
              tx_v[1:0] <= 2'b00;
            end
          end else if (m_code == C_MAN) begin
            if (!tx_half) tx_v[1:0] <= 2'b01; // TP_IDL: TD+ high for the hold time
          end else if (m_code == C_CLK) begin
            tx_v[1] <= tx_sck;
          end else begin
            tx_v[0] <= tx_line;
          end
          tx_oe <= 1'b1;
        end
        T_TAIL2: begin
          tx_v[1:0] <= 2'b10;               // USB J: VPO 0, VMO 1
          tx_oe <= 1'b1;
        end
        T_PULSE: begin
          tx_v[1:0] <= 2'b01;               // link test pulse: TD+ high
          tx_oe <= 1'b1;
        end
        default: begin
          if (m_usb) tx_v[1:0] <= 2'b10;    // J (released when OE is off)
          else if (m_code == C_MAN) tx_v[1:0] <= 2'b00;
          else if (m_code == C_CLK) tx_v[1:0] <= {c_ltc[8], 1'b0};
          else tx_v[1:0] <= 2'b11;          // NRZ idle: marking (1)
          tx_oe <= 1'b0;
        end
      endcase
      tx_v[3:2] <= tx_v[1:0];               // tap B: one cycle later (10BASE-Te second pad pair)
    end
  end

  // Unused in some builds.
  wire _unused = &{1'b0, tx_pop, lstate_q, ck_dly, ck_pend, rx_bit_v, m_oe_low};

endmodule
