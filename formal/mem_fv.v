/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// mem_fv: formal harness of the 128 x 16 latch memory (pe_latch_ram,
// decision D2) with the behavioral latch and clock gate models of
// pe_cells.v.
//
// What it does:
//   It makes the clock from the solver steps: clk changes at each step,
//   so one clock cycle is 2 steps (high half, then low half). The write
//   request (we, waddr, wdata) and the read address are free values that
//   change only at the rising clock edge, because in the chip they come
//   from flip-flops (pe_core, pe_test_port). The reset is applied in the
//   first cycle only. The properties are in pe_latch_ram (`ifdef FORMAL:
//   M1 to M4).
//
// Why:
//   The latches and the clock gates are level-sensitive. With a real
//   clock signal the check sees when each latch is open, not only the
//   values at the clock edges. The memory has the size of the chip.
//
// Not in this check:
//   A reset in the first half of the cycle after a store sets the write
//   data flip-flop to 0 while the word is open, so that word gets 0. The
//   harness has no such reset (M2b_core.md, open items).

module mem_fv (
    input wire        we_in,
    input wire [6:0]  waddr_in,
    input wire [15:0] wdata_in,
    input wire [6:0]  raddr_in
);

  (* gclk *) wire f_step;  // global solver clock: one step = half a clock period

  reg clk = 1'b0;
  always @(posedge f_step) clk <= !clk;

  reg rst_n = 1'b0;  // reset in the first cycle only
  always @(posedge clk) rst_n <= 1'b1;

  reg        we = 1'b0;
  reg [6:0]  waddr;
  reg [15:0] wdata;
  reg [6:0]  raddr;
  always @(posedge clk) begin
    we    <= we_in;
    waddr <= waddr_in;
    wdata <= wdata_in;
    raddr <= raddr_in;
  end

  wire [15:0] rdata;

  pe_latch_ram #(
      .WORDS(128),
      .AW   (7),
      .WIDTH(16)
  ) u_mem (
      .clk  (clk),
      .rst_n(rst_n),
      .we   (we),
      .waddr(waddr),
      .wdata(wdata),
      .raddr(raddr),
      .rdata(rdata)
  );

endmodule
