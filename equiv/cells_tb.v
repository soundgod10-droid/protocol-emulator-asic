/*
 * SPDX-License-Identifier: Apache-2.0
 */

`timescale 1ns / 1ps
`default_nettype none

// cells_tb: the behavioural models of src/pe_cells.v (pe_cg, pe_lat), which every RTL simulation, the coverage run and
// the formal jobs use, against the library cell models of the PDK (sg13cmos5l_lgcp_1, sg13cmos5l_dlhq_1), which the
// gate-level simulations use and which the netlist equivalence keeps as matched black boxes. From each of the 4
// input states, every sequence of DEPTH single-input changes (each step changes one of the two inputs of a cell;
// two inputs changing at the same instant would be a set-up or hold violation, not a function) is applied to both;
// after each step, 1 ns later, the outputs must be equal. Icarus Verilog, functional cell models (FUNCTIONAL).
// Exit: "cells_tb: PASS" or $fatal after the differences.

module cells_tb;
  localparam integer DEPTH = 12;

  reg clk, en, g, d;
  wire gclk_beh, gclk_cell, q_beh, q_cell;

  pe_cg  u_cg_beh (.clk(clk), .en(en), .gclk(gclk_beh));
  sg13cmos5l_lgcp_1 u_cg_cell (.GATE(en), .CLK(clk), .GCLK(gclk_cell));
  pe_lat u_lat_beh (.g(g), .d(d), .q(q_beh));
  sg13cmos5l_dlhq_1 u_lat_cell (.D(d), .GATE(g), .Q(q_cell));

  integer start, seq, k, checks, errors;

  initial begin
    checks = 0;
    errors = 0;
    for (start = 0; start < 4; start = start + 1)
    for (seq = 0; seq < (1 << DEPTH); seq = seq + 1) begin
      // a known state first: both latches open (clk 0, g 1), then the start values
      clk = 0; en = start[0]; g = 1; d = start[0];
      #2;
      clk = start[1]; g = !start[1];
      #2;
      for (k = 0; k < DEPTH; k = k + 1) begin
        if ((seq >> k) & 1) begin
          en = !en;
          d = !d;
        end else begin
          clk = !clk;
          g = !g;
        end
        #1;
        checks = checks + 1;
        if (gclk_beh !== gclk_cell) begin
          errors = errors + 1;
          if (errors < 5) $display("cells_tb: pe_cg differs: sequence %0d step %0d, clk %b en %b: model %b cell %b",
                                   seq, k, clk, en, gclk_beh, gclk_cell);
        end
        if (q_beh !== q_cell) begin
          errors = errors + 1;
          if (errors < 5) $display("cells_tb: pe_lat differs: sequence %0d step %0d, g %b d %b: model %b cell %b",
                                   seq, k, g, d, q_beh, q_cell);
        end
        #1;
      end
    end
    $display("cells_tb: 4 start states x %0d sequences of %0d single-input changes, %0d comparisons per cell, %0d differences",
             1 << DEPTH, DEPTH, checks, errors);
    if (errors != 0) $fatal(1, "cells_tb: FAIL");
    $display("cells_tb: PASS");
    $finish;
  end
endmodule
