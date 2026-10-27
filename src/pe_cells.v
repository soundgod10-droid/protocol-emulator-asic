/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_cg: clock gate for one word of a latch memory.
//
// What it does:
//   gclk follows clk in the clock cycles that start when "en" was 1 at
//   the rising clock edge. In the other cycles gclk stays 0. The cell
//   keeps "en" in a latch while clk is low. Thus, a change of "en" while
//   clk is high cannot make a short pulse (glitch) on gclk.
//
// Why:
//   The latch memories (decision D2, and the host FIFOs of A1 block D6)
//   write one word in the first half of the cycle after the write
//   request. gclk opens the latches of that word. A plain AND gate of clk
//   and a decoded address can make a glitch when the address changes
//   near the clock edge, and the glitch can write a wrong word. The
//   integrated clock gate cell of the library (sg13cmos5l_lgcp_1) cannot.
//
// Behavioral model or library cell (PE_CELLS_BEHAVIORAL below):
//   Synthesis (Yosys defines SYNTHESIS) and the linter (VERILATOR) use the
//   library cell. The formal checks (FORMAL) and the RTL simulation (no
//   macro) use the behavioral model. The gate-level simulation does not
//   read this file. The selection is in the file, because the local test
//   runner does not use the COMPILE_ARGS of test/Makefile.

`ifdef FORMAL
`define PE_CELLS_BEHAVIORAL
`elsif SYNTHESIS
`elsif VERILATOR
`else
`define PE_CELLS_BEHAVIORAL
`endif

module pe_cg (
    input  wire clk,
    input  wire en,    // 1 = give the next clock pulse to gclk
    output wire gclk
);

`ifdef PE_CELLS_BEHAVIORAL
  reg en_l;
  // Same function as the library cell: a latch that is open while clk is
  // low, and an AND gate.
  always @* if (!clk) en_l = en;
  assign gclk = clk & en_l;
`else
  sg13cmos5l_lgcp_1 u_cell (
      .GATE(en),
      .CLK (clk),
      .GCLK(gclk)
  );
`endif

endmodule

// pe_lat: one latch bit of a latch memory.
//
// What it does:
//   While g is 1, q follows d. When g goes to 0, q keeps the last value.
//
// Why:
//   Decision D2 uses latches (sg13cmos5l_dlhq_1, 30.84 um2) and not
//   flip-flops for the 128 x 16 memory. A latch is smaller than a
//   flip-flop (48.99 um2), and a loadable flip-flop also needs a mux2
//   (R1_uart_tx.md 9.4 item 3). The cell is instantiated, so that the
//   linter does not report an inferred latch and synthesis does not change
//   it.

module pe_lat (
    input  wire g,  // gate: 1 = open
    input  wire d,
    output wire q
);

`ifdef PE_BEHAVIORAL_CELLS
  reg q_l;
  always @* if (g) q_l = d;
  assign q = q_l;
`else
  sg13cmos5l_dlhq_1 u_cell (
      .D   (d),
      .GATE(g),
      .Q   (q)
  );
`endif

endmodule
