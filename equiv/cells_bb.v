/*
 * SPDX-License-Identifier: Apache-2.0
 */

// Black boxes for the netlist-to-RTL equivalence (equiv/chip.eqy): the latch and integrated clock-gate cells that
// src/pe_cells.v instantiates under SYNTHESIS. The same black boxes stand on both sides, so the check matches each
// instance by its path and compares the signals at its inputs; the cell function itself is the library's
// (sg13cmos5l_dlhq_1: latch open while GATE is 1; sg13cmos5l_lgcp_1: GCLK = CLK and GATE latched while CLK is 0).

(* blackbox *)
module sg13cmos5l_dlhq_1 (
    input  wire D,
    input  wire GATE,
    output wire Q
);
endmodule

(* blackbox *)
module sg13cmos5l_lgcp_1 (
    input  wire GATE,
    input  wire CLK,
    output wire GCLK
);
endmodule
