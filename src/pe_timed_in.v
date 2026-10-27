/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_timed_in: input part of the timed I/O unit (A1 block D3).
//
// What it does:
//   For each of the 13 thread input pins (docs/isa.md 5.1: uio[7:0] =
//   inputs 0 to 7, ui_in[4:0] = inputs 8 to 12), a 2-stage synchronizer
//   (pe_sync2) gives s(c), the pin level of cycle c - 2 (docs/isa.md
//   6.1). A shift register keeps the last 5 values s(c-1) to s(c-5).
//   s_hist[13*i +: 13] is s(c-1-i).
//
// Why:
//   A thread executes only in its slot, every 4 cycles. SAMPLE needs
//   s(t) of an exact cycle t, and WAIT needs each edge with its cycle. In
//   its next slot after t, the thread finds s(t) in this history (t is 1
//   to 4 cycles back), and the edges of the last 4 cycles (an edge at
//   cycle x needs s(x-1) and s(x), so 5 values). Thus, one shared history
//   of 65 flip-flops replaces a sample time register, a comparator and an
//   edge-capture register for each thread (A1 block D3: 4 x (16-bit
//   capture, 16-bit sample time)). These flip-flops shift in each cycle,
//   so they need no load-enable multiplexer.
//
// Pin level of a uio pin:
//   A uio pin with OE = 1 reads its own output value V (docs/isa.md 5.1).
//   The model calculates the level in the same way.

module pe_timed_in (
    input  wire        clk,
    input  wire        rst_n,   // active low, asynchronous assert
    input  wire [4:0]  ui_in,   // ui_in[4:0]: inputs 8 to 12
    input  wire [7:0]  uio_in,  // uio pad values: inputs 0 to 7
    input  wire [7:0]  pin_v,   // V of output pins 0 to 7 (uio)
    input  wire [7:0]  pin_oe,  // OE of output pins 0 to 7 (uio)
    output wire [12:0] s_now,   // s(c)
    output wire [64:0] s_hist   // s(c-1-i) at [13*i +: 13], i = 0 to 4
);

  wire [12:0] level = {ui_in, (uio_in & ~pin_oe) | (pin_v & pin_oe)};

  genvar i;
  generate
    for (i = 0; i < 13; i = i + 1) begin : g_sync
      pe_sync2 u_sync (
          .clk  (clk),
          .rst_n(rst_n),
          .d    (level[i]),
          .q    (s_now[i])
      );
    end
  endgenerate

  reg [64:0] hist;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      hist <= 65'd0;  // docs/isa.md 2.4: input synchronizer 0 after reset
    end else begin
      hist <= {hist[51:0], s_now};
    end
  end

  assign s_hist = hist;

endmodule
