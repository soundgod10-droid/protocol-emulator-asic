/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_reset_sync: reset synchronizer.
//
// What it does:
//   When rst_n_in goes low, rst_n_out goes low at once (no clock is
//   necessary). When rst_n_in goes high, rst_n_out goes high at the
//   second rising clock edge.
//
// Why:
//   The reset comes from a pad, without a relation to the clock. If the
//   flip-flops leave reset near a clock edge, some can leave it one cycle
//   later than others (recovery time). This block releases the reset on a
//   clock edge, so that all flip-flops leave reset in the same cycle. The
//   assert is asynchronous, so that the UART pin goes to idle (high) at
//   once, also without a clock.

module pe_reset_sync (
    input  wire clk,
    input  wire rst_n_in,   // reset from the pad, active low
    output wire rst_n_out   // reset for the design, active low
);

  reg [1:0] stage;

  always @(posedge clk or negedge rst_n_in) begin
    if (!rst_n_in) begin
      stage <= 2'b00;
    end else begin
      // Two stages: the first stage can go metastable, the second gives
      // it one clock cycle to become stable.
      stage <= {stage[0], 1'b1};
    end
  end

  assign rst_n_out = stage[1];

endmodule

// pe_sync2: two flip-flop synchronizer for one input bit.
//
// What it does:
//   The output is the input, 2 clock cycles later.
//
// Why:
//   An input pin can change at any time relative to the clock. The first
//   flip-flop can go metastable. The second flip-flop gives it one clock
//   cycle to become stable. A1 section 2.6 gives this 2-stage synchronizer
//   for each input sample.

module pe_sync2 (
    input  wire clk,
    input  wire rst_n,  // active low, asynchronous assert
    input  wire d,      // asynchronous input
    output wire q       // synchronized output
);

  reg [1:0] stage;

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      stage <= 2'b00;
    end else begin
      stage <= {stage[0], d};
    end
  end

  assign q = stage[1];

endmodule
