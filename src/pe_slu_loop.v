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
// through copies of pe_slu_loop_step, so the register values at the end of each cycle equal
// those of one step per sample, with one rule: at most one edge is used in a cycle (an edge at x1
// right after an edge at x0 is a pulse of half a cycle and is not used). At most one bit is
// decided per cycle (a bit is at least 6 samples long).
//
// Every operation and its order follow the bit-exact loop model in test/slu_loop_model.py; the
// test test_slu.test_slu_loop_equiv compares the decided bits, theta, I and the edge count with
// it in every cycle. Configurations used: 10BASE-T N = 12 (ddr), gears kp 2, 4, 6 after 8 and
// 24 edges, integrator shift 14 in the last gear, window 3 samples (5 in the first 24 edges);
// USB low speed N = 40, kp 0 (hard resync), window 20 samples.

module pe_slu_loop_step (
    input  wire               x,         // sample
    input  wire               xprev,     // previous sample
    input  wire               allow,     // 0: an edge at this sample is not used
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
  wire signed [20:0] tdec_s = manch ? ($signed({5'd0, n, 10'd0}) + $signed({4'd0, n, 11'd0}) - 21'sd2048)
                                    : (half_s - 21'sd2048);
  // Configuration-only terms (the same in both step copies; synthesis shares them).
  wire signed [20:0] kb_s   = -21'sd2048 - mid_s;
  wire signed [20:0] win_s  = $signed({3'b000, win, 12'd0});
  wire signed [20:0] wina_s = $signed({3'b000, wina, 12'd0});
  wire signed [20:0] win_n  = -win_s;
  wire signed [20:0] wina_n = -wina_s;

  // (v + rnd) >>> s with rnd = 2^(s-1) for s > 0 (round half up), as mux stages and a decoder:
  // a variable shift cell can stall the SHARE pass of the layout flow's synthesis for hours.
  function signed [20:0] f_rsra(input signed [20:0] v, input [3:0] s);
    reg signed [20:0] r;
    reg        [20:0] rnd;
    integer           j;
    begin
      rnd = 21'd0;
      for (j = 1; j < 16; j = j + 1)
        if (s == j[3:0]) rnd[j-1] = 1'b1;
      r = v + $signed(rnd);
      r = s[0] ? {r[20], r[20:1]} : r;
      r = s[1] ? {{2{r[20]}}, r[20:2]} : r;
      r = s[2] ? {{4{r[20]}}, r[20:4]} : r;
      r = s[3] ? {{8{r[20]}}, r[20:8]} : r;
      f_rsra = r;
    end
  endfunction

  reg signed [20:0] t;        // theta during the step
  reg signed [20:0] a;        // edge position against the expected edge, before mod N
  reg signed [20:0] eps;
  reg signed [20:0] corr;
  reg signed [20:0] d;
  reg signed [20:0] inew;
  reg        [2:0]  kp;
  reg               hk;
  reg               xe, ok;       // xe: edge between xprev and x
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
    xe        = allow && (x != xprev);
    ok        = 1'b0;
    a         = 21'sd0;
    eps       = 21'sd0;
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
      // eps = ((theta - 1/2 sample - MID + HALF) mod NONE) - HALF, computed as b = theta - 1/2 sample
      // - MID wrapped once into [-HALF, HALF) (theta stays in [-HALF, 2 NONE), so one wrap suffices)
      a = t + kb_s;
      if (a < -half_s) eps = a + none_s;
      else if (a >= half_s) eps = a - none_s;
      else eps = a;
      if ((acq_n != 6'd0) && (nacc < acq_n)) begin
        w  = wina_s;
        ok = (eps <= wina_s) && (eps >= wina_n);
      end else begin
        w  = win_s;
        ok = (eps <= win_s) && (eps >= win_n);
      end
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
        corr = f_rsra(eps, {1'b0, kp});
        t    = t - corr;
        if (hk) begin
          d    = f_rsra(eps, ki_sh);
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

  // Both clock edges: at most one edge is used in a cycle. If x0 has an edge (sub-step A uses it),
  // an edge at x1 (a pulse of half a cycle, 8.3 ns at 60 MHz, shorter than any 10BASE-T pulse) is
  // not used. Then sub-step B never needs the result of A's edge logic for its own edge logic:
  // B with edge logic starts from A without an edge (u_a0, a few adders), and B after an edge in A
  // (u_b1) has no edge logic. The longest path is one full step and one step without an edge,
  // not two full steps (the slow-corner path of the 10BASE-Te loop at 60 MHz).
  wire               e0 = (x0 != xprev);
  wire signed [19:0] th_a, th_a0, th_b0, th_b1, th_b;
  wire signed [11:0] ii_a, ii_a0, ii_b0, ii_b1, ii_b;
  wire        [5:0]  nacc_a, nacc_a0, nacc_b0, nacc_b1, nacc_b;
  wire        [1:0]  nedge_a, nedge_a0, nedge_b0, nedge_b1, nedge_b;
  wire               init_a, init_a0, init_b0, init_b1, init_b;
  wire               dec_a, dec_a0, dec_b0, dec_b1, dec_b;
  wire               bv_a, bv_a0, bv_b0, bv_b1, bv_b, bd_a, bd_a0, bd_b0, bd_b1, bd_b;

  // Sub-step A: sample x0 (ddr only), with its edge.
  pe_slu_loop_step u_a (
      .x(x0), .xprev(xprev), .allow(1'b1), .th(th), .ii(ii), .nacc(nacc_q), .nedge(nedge),
      .init(init), .decided(decided), .n(n), .manch(manch), .kp0(kp0), .kp1(kp1), .kp2(kp2),
      .gn0(gn0), .gn1(gn1), .ki_en(ki_en), .ki_sh(ki_sh), .win(win), .wina(wina), .acq_n(acq_n),
      .init_edge(init_edge), .th_o(th_a), .ii_o(ii_a), .nacc_o(nacc_a), .nedge_o(nedge_a),
      .init_o(init_a), .decided_o(dec_a), .bit_v(bv_a), .bit_d(bd_a)
  );

  // Sub-step A without an edge (the state that B starts from when x0 has no edge).
  pe_slu_loop_step u_a0 (
      .x(x0), .xprev(xprev), .allow(1'b0), .th(th), .ii(ii), .nacc(nacc_q), .nedge(nedge),
      .init(init), .decided(decided), .n(n), .manch(manch), .kp0(kp0), .kp1(kp1), .kp2(kp2),
      .gn0(gn0), .gn1(gn1), .ki_en(ki_en), .ki_sh(ki_sh), .win(win), .wina(wina), .acq_n(acq_n),
      .init_edge(init_edge), .th_o(th_a0), .ii_o(ii_a0), .nacc_o(nacc_a0), .nedge_o(nedge_a0),
      .init_o(init_a0), .decided_o(dec_a0), .bit_v(bv_a0), .bit_d(bd_a0)
  );

  // Sub-step B with its edge: from A without an edge (ddr) or from the registers (one sample).
  pe_slu_loop_step u_b0 (
      .x(x1), .xprev(ddr ? x0 : xprev), .allow(1'b1), .th(ddr ? th_a0 : th),
      .ii(ddr ? ii_a0 : ii), .nacc(ddr ? nacc_a0 : nacc_q), .nedge(ddr ? nedge_a0 : nedge),
      .init(ddr ? init_a0 : init), .decided(ddr ? dec_a0 : decided), .n(n), .manch(manch),
      .kp0(kp0), .kp1(kp1), .kp2(kp2), .gn0(gn0), .gn1(gn1), .ki_en(ki_en), .ki_sh(ki_sh),
      .win(win), .wina(wina), .acq_n(acq_n), .init_edge(init_edge), .th_o(th_b0), .ii_o(ii_b0),
      .nacc_o(nacc_b0), .nedge_o(nedge_b0), .init_o(init_b0), .decided_o(dec_b0), .bit_v(bv_b0),
      .bit_d(bd_b0)
  );

  // Sub-step B after an edge in A: no edge logic.
  pe_slu_loop_step u_b1 (
      .x(x1), .xprev(x0), .allow(1'b0), .th(th_a), .ii(ii_a), .nacc(nacc_a), .nedge(nedge_a),
      .init(init_a), .decided(dec_a), .n(n), .manch(manch), .kp0(kp0), .kp1(kp1), .kp2(kp2),
      .gn0(gn0), .gn1(gn1), .ki_en(ki_en), .ki_sh(ki_sh), .win(win), .wina(wina), .acq_n(acq_n),
      .init_edge(init_edge), .th_o(th_b1), .ii_o(ii_b1), .nacc_o(nacc_b1), .nedge_o(nedge_b1),
      .init_o(init_b1), .decided_o(dec_b1), .bit_v(bv_b1), .bit_d(bd_b1)
  );

  wire sel1 = ddr && e0;
  assign th_b    = sel1 ? th_b1    : th_b0;
  assign ii_b    = sel1 ? ii_b1    : ii_b0;
  assign nacc_b  = sel1 ? nacc_b1  : nacc_b0;
  assign nedge_b = sel1 ? nedge_b1 : nedge_b0;
  assign init_b  = sel1 ? init_b1  : init_b0;
  assign dec_b   = sel1 ? dec_b1   : dec_b0;
  assign bv_b    = sel1 ? bv_b1    : bv_b0;
  assign bd_b    = sel1 ? bd_b1    : bd_b0;
  wire _unused_a0 = &{1'b0, bv_a0, bd_a0};

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
