/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_timed_in: input part of the timed I/O unit.
//
// What it does:
//   For each of the 13 thread input pins (docs/isa.md 5.1: uio[7:0] =
//   inputs 0 to 7, ui_in[4:0] = inputs 8 to 12), a 2-stage synchronizer
//   (pe_sync2) gives s(c), the pin level of cycle c - 2 (docs/isa.md
//   6.1). Inputs 6 and 7 (I2C SCL and SDA) also go through a spike filter
//   (pe_spike_filter, K = 4): their s(c) changes only after 4 samples in
//   a row of the new level, 4 cycles later than the other inputs.
//   A shift register keeps the last 5 values s(c-1) to s(c-5).
//   s_hist[13*i +: 13] is s(c-1-i).
//
// Why:
//   A thread executes only in its slot, every 4 cycles. SAMPLE needs
//   s(t) of an exact cycle t, and WAIT needs each edge with its cycle. In
//   its next slot after t, the thread finds s(t) in this history (t is 1
//   to 4 cycles back), and the edges of the last 4 cycles (an edge at
//   cycle x needs s(x-1) and s(x), so 5 values). Thus, one shared history
//   of 65 flip-flops replaces a sample time register, a comparator and an
//   edge-capture register for each thread (4 x (16-bit capture, 16-bit
//   sample time)). These flip-flops shift in each cycle,
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
    input  wire [1:0]  ev_in,   // events of the line helpers (virtual inputs 13, 14)
    output wire [12:0] level,   // pin levels before the synchronizers (helper DIN capture)
    output wire [14:0] s_now,   // s(c); inputs 13, 14: the helper events of cycle c - 2
    output wire [74:0] s_hist   // s(c-1-i) at [15*i +: 15], i = 0 to 4
);

  assign level = {ui_in, (uio_in & ~pin_oe) | (pin_v & pin_oe)};
  wire [12:0] y;  // synchronizer outputs

  // Virtual inputs 13 and 14: the event outputs of the USB and 10BASE-Te helpers. They are
  // synchronous, so two plain flip-flop stages give them the same delay as a pin (s(c) = the
  // event of cycle c - 2), and SAMPLE, WAIT and port PINS treat them as pins (docs/isa.md 5.1).
  reg [1:0] ev_d1, ev_d2;
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      ev_d1 <= 2'b00;
      ev_d2 <= 2'b00;
    end else begin
      ev_d1 <= ev_in;
      ev_d2 <= ev_d1;
    end
  end
  assign s_now[14:13] = ev_d2;

  genvar i;
  generate
    for (i = 0; i < 13; i = i + 1) begin : g_sync
      pe_sync2 u_sync (
          .clk  (clk),
          .rst_n(rst_n),
          .d    (level[i]),
          .q    (y[i])
      );
      // Inputs 6 and 7 (uio[6], uio[7]: I2C SCL and SDA, docs/isa.md 5.1)
      // have a spike filter after the synchronizer (docs/isa.md 6.1).
      if ((i == 6) || (i == 7)) begin : g_filter
        pe_spike_filter #(
            .K(5)
        ) u_filter (
            .clk  (clk),
            .rst_n(rst_n),
            .d    (y[i]),
            .q    (s_now[i])
        );
      end else begin : g_direct
        assign s_now[i] = y[i];
      end
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
