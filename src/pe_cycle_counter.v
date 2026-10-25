/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_cycle_counter: free-running cycle counter of the timed I/O unit
// (block D2 of architecture D, A1_architecture.md section 4.4 and 7).
//
// What it does:
//   The counter adds 1 at each rising clock edge. Reset sets it to 0.
//   It does not stop and it does not load a value. At the maximum value
//   it goes back to 0.
//
// Why:
//   In architecture D, each pin event has a cycle time. A timed output
//   channel changes its pin when the counter is equal to that time. Thus,
//   all pin edges come from one time base, and the pin timing is accurate
//   to one clock cycle. A1 section 7 gives a 32-bit counter. M1 uses only
//   bits [15:0] (the event time is 16 bits). Synthesis removes the bits
//   that have no load. M2 will read the full value (time capture).

module pe_cycle_counter #(
    parameter integer WIDTH = 32  // A1 section 7: 32-bit cycle counter
) (
    input  wire             clk,
    input  wire             rst_n,  // active low, asynchronous assert
    output reg  [WIDTH-1:0] count
);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      count <= {WIDTH{1'b0}};
    end else begin
      // Wrap at the maximum value is correct: the timed output channels
      // compare for equality, so only the low bits and their order matter.
      count <= count + {{(WIDTH - 1) {1'b0}}, 1'b1};
    end
  end

endmodule
