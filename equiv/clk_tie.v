/*
 * SPDX-License-Identifier: Apache-2.0
 */

// Techmap for equiv/chip.eqy: each clock-gate cell becomes eqy_lgcp_box (equiv/cells_bb.v), the same cell without its
// clock pin, on both sides. In the RTL that pin is the chip's clk port with no logic in front of it, and a black-box
// input driven straight by a chip input is a piece with no logic, which eqy's partition step refuses ("Wire ...clk
// is used but has no driver", equiv runs 44891831007 and 44919684023). The SRAM macros' A_CLK pins are tied off in
// chip.eqy for the same reason. The clock pins are checked by equiv/teeth.py clocks (every clock gate and macro
// clocked from clk through buffers only, as in the synthesis netlist).

(* techmap_celltype = "sg13cmos5l_lgcp_1" *)
module eqy_lgcp_tie (
    input  wire GATE,
    input  wire CLK,
    output wire GCLK
);
  eqy_lgcp_box _TECHMAP_REPLACE_ (
      .GATE(GATE),
      .GCLK(GCLK)
  );
endmodule
