/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Receive phase loop of the serial line unit (SLU): bit clock recovery and bit decision.
//
// A numerically controlled phase accumulator theta counts the position in the bit in units of
// 2^-12 sample. One bit is N samples, so theta wraps at N * 2^12. Each input edge gives a phase
// error eps against the expected edge position: the bit boundary (NRZ, NRZI: USB) or the middle
// of the bit (Manchester: 10BASE-T). An edge inside the window corrects theta by eps / 2^kp
// (kp = 0: hard resync on every edge) and, in the last gear, updates a frequency integrator I
// that is added to theta once per bit (second-order loop). The gain shifts after a count of
// accepted edges (gears). The bit is the line level at the decision point: the sample nearest
// the bit centre (NRZ) or 3/4 bit (Manchester).
//
// Both clock edges (ddr = 1): two samples per clock cycle, x0 first in time, then x1 (the
// falling-edge sample is x0, the rising-edge sample x1). The two sub-steps run in one cycle
// through two copies of pe_slu_loop_step, so the register values at the end of each cycle
// equal those of one step per sample. At most one bit is decided per cycle (a bit is at least
// 6 samples long).
//
// Every operation and its order follow the bit-exact loop model in test/slu_loop_model.py; the
// test test_slu.test_slu_loop_equiv compares the decided bits, theta, I and the edge count with
// it in every cycle. Configurations used: 10BASE-T N = 12 (ddr), gears kp 2, 4, 6 after 8 and
// 24 edges, integrator shift 14 in the last gear, window 3 samples (5 in the first 24 edges);
// USB low speed N = 40, kp 0 (hard resync), window 20 samples.

module pe_slu_loop_step (
    input  wire               x,         // sample
    input  wire               xprev,     // previous sample
    input  wire signed [19:0] th,        // phase, 2^-12 sample
    input  wire signed [11:0] ii,        // frequency integrator, 2^-16 sample per bit
    input  wire        [5:0]  nacc,      // accepted edges (saturating)
    input  wire        [1:0]  nedge,     // edges before the initial sync
    input  wire               init,      // initial sync done
    input  wire               decided,   // bit of this period decided
    input  wire        [5:0]  n,         // samples per bit
    input  wire               manch,
    input  wire        [2:0]  kp0,
    input  wire        [2:0]  kp1,
    input  wire        [2:0]  kp2,
    input  wire        [5:0]  gn0,
    input  wire        [5:0]  gn1,
    input  wire               ki_en,
    input  wire        [3:0]  ki_sh,     // integrator shift - 4 (SI)
    input  wire        [5:0]  win,
    input  wire        [5:0]  wina,
    input  wire        [5:0]  acq_n,
    input  wire        [1:0]  init_edge,
    output reg  signed [19:0] th_o,
    output reg  signed [11:0] ii_o,
    output reg         [5:0]  nacc_o,
    output reg         [1:0]  nedge_o,
    output reg                init_o,
    output reg                decided_o,
    output reg                bit_v,
    output reg                bit_d
);

  wire signed [20:0] none_s = $signed({3'b000, n, 12'd0});        // N * 2^12
  wire signed [20:0] half_s = $signed({4'b0000, n, 11'd0});       // N * 2^11
  wire signed [20:0] mid_s  = manch ? half_s : 21'sd0;
  // Decision point: (3/4 N - 1/2) * 2^12 (Manchester) or (1/2 N - 1/2) * 2^12.
  wire signed [20:0] tdec_s = manch ? ($signed({1'b0, n, 10'd0}) + $signed({2'b00, n, 11'd0}) - 21'sd2048)
                                    : (half_s - 21'sd2048);

  reg signed [20:0] t;        // theta during the step
  reg signed [20:0] a;        // edge position against the expected edge, before mod N
  reg signed [20:0] eps;
  reg signed [20:0] w;
  reg signed [20:0] corr;
  reg signed [20:0] d;
  reg signed [20:0] inew;
  reg        [2:0]  kp;
  reg               hk;
  reg               xe, st, ok;   // xe: edge between xprev and x
  reg        [1:0]  ne;

  always @(*) begin
    t         = $signed({th[19], th}) + 21'sd4096;
    ii_o      = ii;
    nacc_o    = nacc;
    nedge_o   = nedge;
    init_o    = init;
    decided_o = decided;
    bit_v     = 1'b0;
    bit_d     = x;
    xe        = (x != xprev);
    st        = 1'b0;
    ok        = 1'b0;
    a         = 21'sd0;
    eps       = 21'sd0;
    w         = 21'sd0;
    corr      = 21'sd0;
    d         = 21'sd0;
    inew      = 21'sd0;
    kp        = 3'd0;
    hk        = 1'b0;
    ne        = nedge;

    if (xe && !init) begin
      ne      = (nedge == 2'd3) ? 2'd3 : nedge + 2'd1;
      nedge_o = ne;
      if (ne >= init_edge) begin
        st        = 1'b1;
        t         = mid_s + 21'sd2048;
        init_o    = 1'b1;
        decided_o = 1'b0;
        nacc_o    = 6'd0;
      end
    end

    if (xe && init) begin
      // eps = ((theta - 1/2 sample - MID + HALF) mod NONE) - HALF
      a = t - 21'sd2048 - mid_s + half_s;
      if (a < 0) a = a + none_s;
      else if (a >= none_s) a = a - none_s;
      eps = a - half_s;
      w   = ((acq_n != 6'd0) && (nacc < acq_n)) ? $signed({3'b000, wina, 12'd0})
                                                : $signed({3'b000, win, 12'd0});
      ok  = ((eps < 0) ? -eps : eps) <= w;
      if (ok) begin
        if (nacc < gn0) begin
          kp = kp0;
        end else if (nacc < gn1) begin
          kp = kp1;
        end else begin
          kp = kp2;
          hk = ki_en;
        end
        // corr = (eps + rnd) >> kp, rnd = 2^(kp-1) for kp > 0 (round half up), arithmetic shift
        corr = (kp == 3'd0) ? eps : ((eps + (21'sd1 <<< (kp - 3'd1))) >>> kp);
        t    = t - corr;
        if (hk) begin
          d    = (ki_sh == 4'd0) ? eps : ((eps + (21'sd1 <<< (ki_sh - 4'd1))) >>> ki_sh);
          inew = $signed({{9{ii[11]}}, ii}) - d;
          if (inew > 21'sd2047) ii_o = 12'sd2047;
          else if (inew < -21'sd2047) ii_o = -12'sd2047;
          else ii_o = inew[11:0];
        end
        nacc_o = (nacc == 6'd63) ? 6'd63 : nacc + 6'd1;
      end
    end

    // Bit boundary: wrap, add the frequency term (I >> 4).
    if (t >= none_s) begin
      t         = t - none_s + $signed({{13{ii_o[11]}}, ii_o[11:4]});
      decided_o = 1'b0;
    end

    // Decision.
    if (init_o && !decided_o && (t >= tdec_s)) begin
      bit_v     = 1'b1;
      decided_o = 1'b1;
    end

    th_o = t[19:0];
  end

endmodule

module pe_slu_loop (
    input  wire              clk,
    input  wire              rst_n,
    input  wire              en,         // 0: loop state at reset values (hunt)
    input  wire              ddr,
    input  wire              x0,         // first sample of the cycle (used only if ddr)
    input  wire              x1,         // last sample of the cycle
    input  wire       [5:0]  n,
    input  wire              manch,
    input  wire       [2:0]  kp0,
    input  wire       [2:0]  kp1,
    input  wire       [2:0]  kp2,
    input  wire       [5:0]  gn0,
    input  wire       [5:0]  gn1,
    input  wire              ki_en,
    input  wire       [3:0]  ki_sh,
    input  wire       [5:0]  win,
    input  wire       [5:0]  wina,
    input  wire       [5:0]  acq_n,
    input  wire       [1:0]  init_edge,
    output reg               bit_v,      // a bit was decided in this cycle
    output reg               bit_d,
    output wire       [5:0]  nacc,
    output wire              locked_init
);

  reg signed [19:0] th;
  reg signed [11:0] ii;
  reg        [5:0]  nacc_q;
  reg        [1:0]  nedge;
  reg               init, decided, xprev;

  wire signed [19:0] th_a, th_b;
  wire signed [11:0] ii_a, ii_b;
  wire        [5:0]  nacc_a, nacc_b;
  wire        [1:0]  nedge_a, nedge_b;
  wire               init_a, init_b, dec_a, dec_b, bv_a, bv_b, bd_a, bd_b;

  // Sub-step A: sample x0 (ddr only).
  pe_slu_loop_step u_a (
      .x(x0), .xprev(xprev), .th(th), .ii(ii), .nacc(nacc_q), .nedge(nedge), .init(init),
      .decided(decided), .n(n), .manch(manch), .kp0(kp0), .kp1(kp1), .kp2(kp2), .gn0(gn0),
      .gn1(gn1), .ki_en(ki_en), .ki_sh(ki_sh), .win(win), .wina(wina), .acq_n(acq_n),
      .init_edge(init_edge), .th_o(th_a), .ii_o(ii_a), .nacc_o(nacc_a), .nedge_o(nedge_a),
      .init_o(init_a), .decided_o(dec_a), .bit_v(bv_a), .bit_d(bd_a)
  );

  // Sub-step B: sample x1, from the state after A (ddr) or from the registers (one sample).
  pe_slu_loop_step u_b (
      .x(x1), .xprev(ddr ? x0 : xprev), .th(ddr ? th_a : th), .ii(ddr ? ii_a : ii),
      .nacc(ddr ? nacc_a : nacc_q), .nedge(ddr ? nedge_a : nedge), .init(ddr ? init_a : init),
      .decided(ddr ? dec_a : decided), .n(n), .manch(manch), .kp0(kp0), .kp1(kp1), .kp2(kp2),
      .gn0(gn0), .gn1(gn1), .ki_en(ki_en), .ki_sh(ki_sh), .win(win), .wina(wina),
      .acq_n(acq_n), .init_edge(init_edge), .th_o(th_b), .ii_o(ii_b), .nacc_o(nacc_b),
      .nedge_o(nedge_b), .init_o(init_b), .decided_o(dec_b), .bit_v(bv_b), .bit_d(bd_b)
  );

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      th      <= 20'sd0;
      ii      <= 12'sd0;
      nacc_q  <= 6'd0;
      nedge   <= 2'd0;
      init    <= 1'b0;
      decided <= 1'b0;
      xprev   <= 1'b0;
      bit_v   <= 1'b0;
      bit_d   <= 1'b0;
    end else if (!en) begin
      th      <= 20'sd0;
      ii      <= 12'sd0;
      nacc_q  <= 6'd0;
      nedge   <= 2'd0;
      init    <= 1'b0;
      decided <= 1'b0;
      xprev   <= x1;
      bit_v   <= 1'b0;
    end else begin
      th      <= th_b;
      ii      <= ii_b;
      nacc_q  <= nacc_b;
      nedge   <= nedge_b;
      init    <= init_b;
      decided <= dec_b;
      xprev   <= x1;
      bit_v   <= (ddr & bv_a) | bv_b;
      bit_d   <= (ddr & bv_a) ? bd_a : bd_b;
    end
  end

  assign nacc        = nacc_q;
  assign locked_init = init;

endmodule
