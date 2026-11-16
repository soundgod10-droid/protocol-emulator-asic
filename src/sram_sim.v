/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Functional simulation models of the two IHP SG13 SRAM macros (RTL and gate-level simulation
// only; synthesis reads src/sram_bb.v). Written from the macro datasheet operation table
// (IHP-Open-PDK libs.ref/sg13g2_sram/doc/RM_IHPSG13_1P_<words>x16_c2_bm_bist.txt): all inputs are
// sampled at the rising A_CLK edge; A_MEN = 0: no operation; A_WEN = 1: write the bits with A_BM = 1;
// A_REN = 1 and A_WEN = 0: read, A_DOUT shows the word after the edge and keeps it until the next
// read; A_WEN = A_REN = 1: write-through (A_DOUT shows the written bits). A_DLY must be 1. The
// memory content after power-up is not defined: the model starts with X, so a read of a word that
// was never written gives X in simulation.

module RM_IHPSG13_1P_512x16_c2_bm_bist (
    input  wire        A_CLK,
    input  wire        A_MEN,
    input  wire        A_WEN,
    input  wire        A_REN,
    input  wire [8:0]  A_ADDR,
    input  wire [15:0] A_DIN,
    input  wire        A_DLY,
    output reg  [15:0] A_DOUT,
    input  wire [15:0] A_BM,
    input  wire        A_BIST_CLK,
    input  wire        A_BIST_EN,
    input  wire        A_BIST_MEN,
    input  wire        A_BIST_WEN,
    input  wire        A_BIST_REN,
    input  wire [8:0]  A_BIST_ADDR,
    input  wire [15:0] A_BIST_DIN,
    input  wire [15:0] A_BIST_BM
);
  reg [15:0] mem [0:511];
  always @(posedge A_CLK) begin
    if (A_MEN) begin
      if (A_WEN) begin
        mem[A_ADDR] <= (mem[A_ADDR] & ~A_BM) | (A_DIN & A_BM);
        if (A_REN) A_DOUT <= (mem[A_ADDR] & ~A_BM) | (A_DIN & A_BM);
      end else if (A_REN) begin
        A_DOUT <= mem[A_ADDR];
      end
    end
  end
  wire _unused = &{A_DLY, A_BIST_CLK, A_BIST_EN, A_BIST_MEN, A_BIST_WEN, A_BIST_REN, A_BIST_ADDR,
                   A_BIST_DIN, A_BIST_BM, 1'b0};
endmodule

module RM_IHPSG13_1P_1024x16_c2_bm_bist (
    input  wire        A_CLK,
    input  wire        A_MEN,
    input  wire        A_WEN,
    input  wire        A_REN,
    input  wire [9:0]  A_ADDR,
    input  wire [15:0] A_DIN,
    input  wire        A_DLY,
    output reg  [15:0] A_DOUT,
    input  wire [15:0] A_BM,
    input  wire        A_BIST_CLK,
    input  wire        A_BIST_EN,
    input  wire        A_BIST_MEN,
    input  wire        A_BIST_WEN,
    input  wire        A_BIST_REN,
    input  wire [9:0]  A_BIST_ADDR,
    input  wire [15:0] A_BIST_DIN,
    input  wire [15:0] A_BIST_BM
);
  reg [15:0] mem [0:1023];
  always @(posedge A_CLK) begin
    if (A_MEN) begin
      if (A_WEN) begin
        mem[A_ADDR] <= (mem[A_ADDR] & ~A_BM) | (A_DIN & A_BM);
        if (A_REN) A_DOUT <= (mem[A_ADDR] & ~A_BM) | (A_DIN & A_BM);
      end else if (A_REN) begin
        A_DOUT <= mem[A_ADDR];
      end
    end
  end
  wire _unused = &{A_DLY, A_BIST_CLK, A_BIST_EN, A_BIST_MEN, A_BIST_WEN, A_BIST_REN, A_BIST_ADDR,
                   A_BIST_DIN, A_BIST_BM, 1'b0};
endmodule
