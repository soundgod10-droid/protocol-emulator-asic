/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_cycle_counter: free-running cycle counter of the timed I/O unit.
//
// What it does:
//   The counter adds 1 at each rising clock edge. Reset sets it to 0.
//   It does not stop and it does not load a value. At the maximum value
//   it goes back to 0.
//
// Why:
//   Each pin event has a cycle time. A timed output channel changes its
//   pin when the counter is equal to that time. Thus, all pin edges come
//   from one time base, and the pin timing is accurate to one clock
//   cycle. The chip uses only bits [15:0] (the event time is 16 bits,
//   docs/isa.md 2.2); synthesis removes the bits that have no load.

//
// clear:
//   1 in a cycle sets the counter to 0 at the end of that cycle. The host
//   starts the threads with it (pe_host_port START), so that the first
//   cycle of the threads is cycle 0, thread 0 has the first slot and the
//   cycle numbers agree with the reference model (docs/isa.md 3.1).

module pe_cycle_counter #(
    parameter integer WIDTH = 32  // A1 section 7: 32-bit cycle counter
) (
    input  wire             clk,
    input  wire             rst_n,  // active low, asynchronous assert
    input  wire             clear,  // synchronous: 1 = count is 0 in the next cycle
    output reg  [WIDTH-1:0] count
);

  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      count <= {WIDTH{1'b0}};
    end else if (clear) begin
      count <= {WIDTH{1'b0}};
    end else begin
      // Wrap at the maximum value is correct: the timed output channels
      // compare for equality, so only the low bits and their order matter.
      count <= count + {{(WIDTH - 1) {1'b0}}, 1'b1};
    end
  end

endmodule
