/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// slc_fv: formal wrapper of the chip's USB helper copy (src/pe_slu.v with SPEC 1, FULL 0) in its clocked and
// NRZ modes (docs/helpers.md section 6), for formal/slc_rx.sby, slc_tx.sby and their teeth.
//
// What it does:
//   The configuration words that the thread writes for these modes (MODE, TXPER, LTC, SEL, and the bit count
//   TXCTRL[4:0]) are held at constant values that the solver chooses (anyconst); the .sby connects the helper's
//   registers to them after flattening, so the RTL stays unchanged. The thread rule behind this: the
//   configuration is written while the helper is idle (docs/helpers.md section 6). Every other input is free in
//   every cycle: the pins, the falling-edge DIN sample (din_f1_free, as formal/slu_fv.v), the port reads, reset
//   after step 0, and the port writes that each job allows. The monitors below are written from
//   docs/helpers.md section 6, not from the RTL:
//
//   SLC_RX (edge-started receive, LTC[10]; UART receive):
//     R1  A frame starts exactly at a falling edge of DIN (as seen two cycles behind the pad, the rising-edge
//         synchronizer copy x1) while no sample of the previous frame is outstanding; such an edge always starts
//         one. So frames follow back to back: the next edge is taken from the cycle in which the last bit of the
//         previous frame is delivered.
//     R2  Sample k of the frame (k = 0 .. N-1, N = TXCTRL[4:0], 0 = 16) is the selected DIN copy (LTC[12]: the
//         falling-edge copy x0, half a cycle earlier; else x1) in cycle e + TXPER + D + 2 * TXPER * k, where e is
//         the start cycle and D = SEL[15:12]; it is delivered as a data bit in the next cycle, and no data bit is
//         delivered at any other time.
//     R3  The last sample pushes one word with the frame in its top N bits (first sample in bit 16 - N), and
//         only then is a word pushed.
//     R4  RXEOP is set after the last sample (unless the thread clears it in the same cycle).

module slc_fv #(
    parameter PMAX = 4     // largest TXPER of this parameter-reduced proof (the RTL takes 1 to 255)
) (
    input wire        clk,
    input wire        rst_n,
    input wire        p_we,
    input wire        p_re,
    input wire [3:0]  p_addr,
    input wire [15:0] p_wdata,
    input wire [12:0] pin_raw,
    input wire [12:0] pin_sync,
    input wire        din_f1_free
);
  initial assume (!rst_n);

  reg [15:0] now;
  always @(posedge clk) now <= now + 16'd1;

  wire [15:0] p_rdata;
  wire [3:0]  tx_v;
  wire        tx_oe, ev;
  pe_slu #(
      .FULL(0),
      .SPEC(1)
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

  // Configuration chosen by the solver, constant in a run.
  (* anyconst *) reg [7:0] f_per;    // TXPER
  (* anyconst *) reg [3:0] f_dly;    // SEL[15:12], sample delay
  (* anyconst *) reg [3:0] f_din;    // SEL[3:0], DIN pin
  (* anyconst *) reg       f_fall;   // LTC[12]
  (* anyconst *) reg [1:0] f_ck;     // LTC[9:8], clock levels
  (* anyconst *) reg [4:0] f_len;    // TXCTRL[4:0], samples a frame (0 = 16)
  always @* assume ((f_per >= 8'd2) && (f_per <= PMAX) && (f_din < 4'd13) && (f_len <= 5'd16));

  // The register values (connected to u_slu.c_mode, c_txper, c_ltc, c_sel, tx_lastbits by the .sby): the
  // writable bits of SPEC 1 set as the thread writes them, every other bit at its USB value (F_MODE 03B9,
  // F_LTC 0044).
  (* keep *) wire [15:0] k_mode  = 16'h01AB;                          // code 3 (clocked), RX on, no stuffing
  (* keep *) wire [15:0] k_txper = {8'd0, f_per};
  (* keep *) wire [15:0] k_ltc   = 16'h0444 | {3'd0, f_fall, 2'd0, f_ck, 8'd0};   // LTC[10] edge start
  (* keep *) wire [15:0] k_sel   = {f_dly, 8'hFF, f_din};
  (* keep *) wire [4:0]  k_len   = f_len;

  // Receive only: the thread clears flags (STATUS) and sets the event mask; it writes nothing that starts or
  // stops a transmission and no configuration word while receiving.
  always @* assume (!p_we || (p_addr == 4'd2) || (p_addr == 4'd3));

  // Internal nets of the helper, connected by the .sby after flattening.
  (* keep *) wire        mon_x0;      // x0
  (* keep *) wire        mon_x1;      // x1
  (* keep *) wire        mon_sop;     // rx_sop
  (* keep *) wire        mon_rdv;     // r_data_v
  (* keep *) wire        mon_rd;      // r_data
  (* keep *) wire        mon_push;    // rx_push
  (* keep *) wire [15:0] mon_pushw;   // rx_push_w
  (* keep *) wire [15:0] mon_sticky;  // sticky

  // ---------------------------------------------------------------------------------------------------
  // Monitor of the edge-started frames
  // ---------------------------------------------------------------------------------------------------
  wire [4:0]  len  = (f_len == 5'd0) ? 5'd16 : f_len;
  wire [15:0] top  = ~(16'hFFFF >> len);                  // the top len bits of a word
  wire        smp  = f_fall ? mon_x0 : mon_x1;            // the selected DIN copy
  reg         x1q;                                        // x1 in the previous cycle
  reg         m_fr;                                       // a frame has a sample not yet delivered
  reg  [4:0]  m_k;                                        // samples taken of the frame
  reg  [9:0]  m_cd;                                       // cycles to the next sample time
  reg         m_due, m_sv, m_last;                        // a bit to deliver now, its value, it is the last
  reg  [15:0] m_w;                                        // delivered bits, newest in bit 15
  reg         m_eop;                                      // the last bit was delivered in the previous cycle

  wire idle  = !m_fr || (m_due && m_last);
  wire start = idle && x1q && !mon_x1;
  wire take  = m_fr && (m_k != len) && (m_cd == 10'd0);
  wire [15:0] w_new = {m_sv, m_w[15:1]};

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      x1q <= 1'b0; m_fr <= 1'b0; m_k <= 5'd0; m_cd <= 10'd0; m_due <= 1'b0; m_last <= 1'b0; m_eop <= 1'b0;
    end else begin
      x1q   <= mon_x1;
      m_due <= 1'b0;
      m_eop <= m_due && m_last && !(p_we && (p_addr == 4'd2) && p_wdata[5]);
      if (m_fr && (m_cd != 10'd0)) m_cd <= m_cd - 10'd1;
      if (take) begin
        m_sv   <= smp;
        m_due  <= 1'b1;
        m_last <= (m_k + 5'd1 == len);
        m_k    <= m_k + 5'd1;
        m_cd   <= {1'b0, f_per, 1'b0} - 10'd1;
      end
      if (m_due) begin
        m_w <= w_new;
        if (m_last) m_fr <= 1'b0;
      end
      if (start) begin
        m_fr <= 1'b1;
        m_k  <= 5'd0;
        m_cd <= {2'd0, f_per} + {6'd0, f_dly} - 10'd1;
      end
    end
  end

  always @* begin
    if (rst_n) begin
      // R1
      assert (mon_sop == start);
      // R2
      assert (mon_rdv == m_due);
      if (m_due) assert (mon_rd == m_sv);
      // R3
      assert (mon_push == (m_due && m_last));
      if (m_due && m_last) assert (((mon_pushw ^ w_new) & top) == 16'd0);
      // R4
      if (m_eop) assert (mon_sticky[5]);
    end
  end

`ifdef SLC_COVER
  // Two frames back to back, the second started in the cycle that delivers the last bit of the first.
  always @* cover (rst_n && m_due && m_last && start);
`endif

endmodule
