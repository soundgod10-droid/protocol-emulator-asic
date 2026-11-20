/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// fpga_latch_ram_fv: miter of the chip's latch memory (src/pe_latch_ram.v with the behavioural latch and clock-gate
// models of src/pe_cells.v, renamed pe_latch_ram_lat by the .sby) and the FPGA stand-in (fpga/pe_latch_ram_fpga.v),
// 16 words x 16 bits, for formal/fpga_latch_ram.sby (multiclock: the clock changes level at every solver step, so the
// latches and clock gates are checked at both clock levels). Both get the same inputs, free in every step. For a
// word chosen by the solver: once it has been written, the two read ports give the same value whenever it is read,
// at every step (both clock levels). FPGA_LATE (the tooth) gives the stand-in its write one cycle late: must fail.

module fpga_latch_ram_fv (
    input wire        we,
    input wire [3:0]  waddr,
    input wire [15:0] wdata,
    input wire [3:0]  raddr
);
  reg clk = 1'b0;
  always @($global_clock) clk <= !clk;

  // inputs change only at the falling edge (as from flip-flops of the same clock in the chip)
  reg        we_q;
  reg [3:0]  waddr_q, raddr_q;
  reg [15:0] wdata_q;
  always @(negedge clk) begin
    we_q <= we; waddr_q <= waddr; wdata_q <= wdata; raddr_q <= raddr;
  end

  wire [15:0] r_lat, r_fpga;
  pe_latch_ram_lat #(.WORDS(16), .AW(4), .WIDTH(16)) u_lat (
      .clk(clk), .we(we_q), .waddr(waddr_q), .wdata(wdata_q), .raddr(raddr_q), .rdata(r_lat));

`ifdef FPGA_LATE
  reg        we_l;
  reg [3:0]  waddr_l;
  reg [15:0] wdata_l;
  always @(posedge clk) begin
    we_l <= we_q; waddr_l <= waddr_q; wdata_l <= wdata_q;
  end
  pe_latch_ram #(.WORDS(16), .AW(4), .WIDTH(16)) u_fpga (
      .clk(clk), .we(we_l), .waddr(waddr_l), .wdata(wdata_l), .raddr(raddr_q), .rdata(r_fpga));
`else
  pe_latch_ram #(.WORDS(16), .AW(4), .WIDTH(16)) u_fpga (
      .clk(clk), .we(we_q), .waddr(waddr_q), .wdata(wdata_q), .raddr(raddr_q), .rdata(r_fpga));
`endif

  (* anyconst *) reg [3:0] f_a;
  reg f_w = 1'b0;                     // word f_a has been written (request seen at a rising edge)
  always @(posedge clk) if (we_q && (waddr_q == f_a)) f_w <= 1'b1;

  always @* begin
    if (f_w && (raddr_q == f_a)) assert (r_lat == r_fpga);
  end

  always @* cover (f_w && (raddr_q == f_a) && clk);
endmodule
