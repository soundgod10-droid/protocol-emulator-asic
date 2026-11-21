// SPDX-License-Identifier: Apache-2.0
// The single-bit gate and flip-flop cells that read_liberty makes from the cell functions of the netlist, as
// word-level cells of width 1 (techmap -autoproc -map), for the Yosys sim pass of equiv/dprove.ys: that pass
// evaluates the word-level cells only ("Unsupported cell type: $_OR_", equiv run 44881773212). Logic unchanged.

(* techmap_celltype = "$_BUF_" *)
module _cv_buf (input A, output Y); assign Y = A; endmodule
(* techmap_celltype = "$_NOT_" *)
module _cv_not (input A, output Y); assign Y = ~A; endmodule
(* techmap_celltype = "$_AND_" *)
module _cv_and (input A, input B, output Y); assign Y = A & B; endmodule
(* techmap_celltype = "$_NAND_" *)
module _cv_nand (input A, input B, output Y); assign Y = ~(A & B); endmodule
(* techmap_celltype = "$_OR_" *)
module _cv_or (input A, input B, output Y); assign Y = A | B; endmodule
(* techmap_celltype = "$_NOR_" *)
module _cv_nor (input A, input B, output Y); assign Y = ~(A | B); endmodule
(* techmap_celltype = "$_XOR_" *)
module _cv_xor (input A, input B, output Y); assign Y = A ^ B; endmodule
(* techmap_celltype = "$_XNOR_" *)
module _cv_xnor (input A, input B, output Y); assign Y = ~(A ^ B); endmodule
(* techmap_celltype = "$_ANDNOT_" *)
module _cv_andnot (input A, input B, output Y); assign Y = A & ~B; endmodule
(* techmap_celltype = "$_ORNOT_" *)
module _cv_ornot (input A, input B, output Y); assign Y = A | ~B; endmodule
(* techmap_celltype = "$_MUX_" *)
module _cv_mux (input A, input B, input S, output Y); assign Y = S ? B : A; endmodule

(* techmap_celltype = "$_DFF_P_" *)
module _cv_dff_p (input C, input D, output reg Q); always @(posedge C) Q <= D; endmodule
(* techmap_celltype = "$_DFF_N_" *)
module _cv_dff_n (input C, input D, output reg Q); always @(negedge C) Q <= D; endmodule
(* techmap_celltype = "$_DFF_PP0_" *)
module _cv_dff_pp0 (input C, input D, input R, output reg Q);
  always @(posedge C, posedge R) if (R) Q <= 1'b0; else Q <= D;
endmodule
(* techmap_celltype = "$_DFF_PP1_" *)
module _cv_dff_pp1 (input C, input D, input R, output reg Q);
  always @(posedge C, posedge R) if (R) Q <= 1'b1; else Q <= D;
endmodule
(* techmap_celltype = "$_DFF_PN0_" *)
module _cv_dff_pn0 (input C, input D, input R, output reg Q);
  always @(posedge C, negedge R) if (!R) Q <= 1'b0; else Q <= D;
endmodule
(* techmap_celltype = "$_DFF_PN1_" *)
module _cv_dff_pn1 (input C, input D, input R, output reg Q);
  always @(posedge C, negedge R) if (!R) Q <= 1'b1; else Q <= D;
endmodule
(* techmap_celltype = "$_DFF_NP0_" *)
module _cv_dff_np0 (input C, input D, input R, output reg Q);
  always @(negedge C, posedge R) if (R) Q <= 1'b0; else Q <= D;
endmodule
(* techmap_celltype = "$_DFF_NN0_" *)
module _cv_dff_nn0 (input C, input D, input R, output reg Q);
  always @(negedge C, negedge R) if (!R) Q <= 1'b0; else Q <= D;
endmodule
