/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_latch_ram for an FPGA build (fpga/files.f reads this file in place of src/pe_latch_ram.v and src/pe_cells.v).
//
// What it does:
//   The same module name, parameters and ports as src/pe_latch_ram.v. A write request (we, waddr, wdata) in cycle
//   k writes the word at the rising clock edge that ends cycle k; the read port is combinational.
//
// Why it gives the same values:
//   In the chip the write data goes into a flip-flop at that same edge and the word's latches are open in the
//   high half of cycle k+1, so the word holds the new data from that edge on (formal property M3 of
//   src/pe_latch_ram.v). This model holds it from the same edge, so every value sampled at a rising edge is the
//   same. formal/fpga_latch_ram.sby proves it against the behavioural latch memory. An FPGA has no use for
//   latches and gated clocks; they would give LUT latches and a derived clock.

module pe_latch_ram #(
    parameter integer WORDS = 128,
    parameter integer AW    = 7,
    parameter integer WIDTH = 16
) (
    input  wire             clk,
    input  wire             we,
    input  wire [AW-1:0]    waddr,
    input  wire [WIDTH-1:0] wdata,
    input  wire [AW-1:0]    raddr,
    output wire [WIDTH-1:0] rdata
);
  reg [WIDTH-1:0] mem [0:WORDS-1];
  always @(posedge clk) begin
    if (we) mem[waddr] <= wdata;
  end
  assign rdata = mem[raddr];
endmodule
