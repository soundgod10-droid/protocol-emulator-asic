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

// Size: the default is the chip size 128 x 16. Z3 did not finish a proof
// or a 4-cycle bounded check of the 2,048 latches in 25 to 30 minutes.
// Thus two jobs check the two dimensions: mem_small.sby 8 words x 16
// bits (full word width) and mem.sby 128 words x 2 bits (full address
// decoder and 128:1 read port). Each word and each bit has the same
// generated logic, and the word address of the properties is free.

// FREE_RESET = 1 (mem_reset.sby): the reset can also come at each later
// clock level change. This shows the effect of a reset during a store.

module mem_fv #(
    parameter integer WORDS      = 128,
    parameter integer AW         = 7,
    parameter integer WIDTH      = 16,  // mem.sby: 2 (all 128 words, each bit has the same logic)
    parameter integer FREE_RESET = 0
) (
    input wire             rst_in,   // used only with FREE_RESET = 1
    input wire             we_in,
    input wire [AW-1:0]    waddr_in,
    input wire [WIDTH-1:0] wdata_in,
    input wire [AW-1:0]    raddr_in
);

  (* gclk *) wire f_step;  // global solver clock: one step = half a clock period

  reg clk = 1'b0;
  always @(posedge f_step) clk <= !clk;

  reg rst_q = 1'b0;  // reset in the first cycle only
  always @(posedge clk) rst_q <= 1'b1;
  wire rst_n = (FREE_RESET != 0) ? (rst_q && rst_in) : rst_q;

  reg             we = 1'b0;
  reg [AW-1:0]    waddr;
  reg [WIDTH-1:0] wdata;
  reg [AW-1:0]    raddr;
  always @(posedge clk) begin
    we    <= we_in;
    waddr <= waddr_in;
    wdata <= wdata_in;
    raddr <= raddr_in;
  end

  wire [WIDTH-1:0] rdata;

  pe_latch_ram #(
      .WORDS(WORDS),
      .AW   (AW),
      .WIDTH(WIDTH)
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
