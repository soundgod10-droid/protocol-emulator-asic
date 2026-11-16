/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// sram_fv: formal wrapper of the processor memory (pe_sram) with the functional models of the two
// SRAM macros (src/sram_sim.v, written from the macro datasheet operation table).
//
// What it does:
//   The request (req, we, addr, wdata) is free in each cycle; in the chip it comes from flip-flops
//   of the core or of the host port. The reset is applied in the first cycle. One tracked address A
//   (any value 0 to 2,047, fixed for the trace) and the word last written to the location of A are
//   kept by the wrapper; the location comes from the address map of docs/isa.md 2.2, written here
//   as arithmetic: 0 to 511 program word a, 512 to 2,047 data word (a - 512) mod 1,024.
//
// Properties:
//   S1: A read of A that follows a write to the location of A gives that word in the next cycle.
//   S2: The word stays on rdata until the next read (writes in between do not change it).
//   S3: rdata after a read of A is never a word of the other macro: a write to an address with an
//       other location (also the same 9 low bits in the other macro) does not change the word of A.
//
// Why bounded (mode bmc): the memory content is a state of the macro models, which the wrapper
//   cannot name, so k-induction would start from an arbitrary content. Every write-read pair with
//   up to 8 other requests between them is in the bound.

module sram_fv (
    input wire        clk,
    input wire        req,
    input wire        we,
    input wire [10:0] addr,
    input wire [15:0] wdata
);

  reg rst_n = 1'b0;  // reset in the first cycle only
  always @(posedge clk) rst_n <= 1'b1;

  wire [15:0] rdata;

  pe_sram u_mem (
      .clk  (clk),
      .rst_n(rst_n),
      .req  (req && rst_n),
      .we   (we),
      .addr (addr),
      .wdata(wdata),
      .rdata(rdata)
  );

  (* anyconst *) reg [10:0] f_a;

  // Location of an address: macro (0 program, 1 data) and word in the macro.
  function [10:0] f_loc(input [10:0] x);
    reg [10:0] d;
    begin
      d = x - 11'd512;
      f_loc = (x < 11'd512) ? {2'b00, x[8:0]} : {1'b1, d[9:0]};
    end
  endfunction

  wire f_hit = req && rst_n && (f_loc(addr) == f_loc(f_a));

  reg        f_known = 1'b0;  // a word was written to the location of A
  reg [15:0] f_word;
  reg        f_chk = 1'b0;    // rdata must be f_exp (a read of A, no read since)
  reg [15:0] f_exp;

  always @(posedge clk) begin
    if (f_hit && we) begin
      f_known <= 1'b1;
      f_word  <= wdata;
    end
    if (req && rst_n && !we) begin
      f_chk <= f_hit && f_known;
      f_exp <= f_word;
    end
  end

  always @* begin
    // S1 to S3
    if (f_chk) assert (rdata == f_exp);
  end

  always @* begin
    cover (f_chk && (f_loc(f_a) >= 11'd1024));  // a data-memory word read back
    cover (f_chk && (f_a >= 11'd1536));         // through the alias
  end

endmodule
