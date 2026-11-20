/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none
`timescale 1ns / 1ps

// The two SRAM macro models of src/sram_sim.v with the read delay of the layout, for the SDF job only
// (test/Makefile uses this file in place of src/sram_sim.v when SDF_FILE is set).
//
// The layout SDF gives the macro read time as one IOPATH A_CLK A_DOUT[k] entry for each output bit. The SDF
// parser of vvp does not accept bus-bit names, and a bus-level path (A_CLK *> A_DOUT) with an SDF entry stops
// vvp (Icarus 12: "Assertion failed: tmp == use_delay, file vvp/delay.cc, line 667"). So test/sdf_names.py takes
// the largest value of these entries (16 bits, rise and fall, min:typ:max) from the SDF of the run and corner and
// writes it to sram_delay.vh; this model puts A_DOUT that long after the clock edge. The largest value is the later
// edge for every bit: conservative for setup at the reading flip-flops, optimistic for hold. The function is that of
// src/sram_sim.v (datasheet operation table, see there).

`include "sram_delay.vh"

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
        if (A_REN) A_DOUT <= #(`SRAM_512_DLY) (mem[A_ADDR] & ~A_BM) | (A_DIN & A_BM);
      end else if (A_REN) begin
        A_DOUT <= #(`SRAM_512_DLY) mem[A_ADDR];
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
        if (A_REN) A_DOUT <= #(`SRAM_1024_DLY) (mem[A_ADDR] & ~A_BM) | (A_DIN & A_BM);
      end else if (A_REN) begin
        A_DOUT <= #(`SRAM_1024_DLY) mem[A_ADDR];
      end
    end
  end
  wire _unused = &{A_DLY, A_BIST_CLK, A_BIST_EN, A_BIST_MEN, A_BIST_WEN, A_BIST_REN, A_BIST_ADDR,
                   A_BIST_DIN, A_BIST_BM, 1'b0};
endmodule
