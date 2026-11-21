/*
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none
`timescale 1ns / 1ps

// wiring_check: bitstream for the first bench step (C4 F0), with the same pins as fpga_top.v.
//
// What it does:
//   Every FPGA output pin (uo[0..7], uio[0..7], all driven as outputs here) sends the same kind of frame, again and
//   again: four 8N1 bytes, LSB first, at DIV clock cycles a bit (1 Mbit/s from the ULX3S's 25 MHz oscillator), then
//   two idle bits. Byte 0 is the pin's own index (0xA0 + 0..15: uo[k] is k, uio[k] is 8 + k), byte 1 the value of
//   ui[7:0] as the FPGA sees it, byte 2 the rst_n pin in bit 0, byte 3 the XOR of bytes 0-2 and 0x5A. The inputs are
//   sampled through two flip-flops and held for a frame. All pins send their frames at the same time.
//   The host (fpga/wiring_judge.py) decodes each of its inputs: a pin that decodes another index is a swapped or
//   moved wire, a pin with no valid frame an open or shorted one. It drives one of its outputs high at a time: the ui
//   bits that follow it name where that wire lands (none: open; two: shorted).
//
// Why:
//   A wrong wire on the bench makes every later row fail for a reason that is not the chip's. This check names each
//   wrong wire before the chip bitstream is loaded. No reset is needed: the registers start from their initial values.

module wiring_check #(
    parameter integer DIV = 25  // clock cycles per bit
) (
    input  wire       clk,
    input  wire       rst_n,
    input  wire [7:0] ui,
    output wire [7:0] uo,
    inout  wire [7:0] uio
);
  reg [7:0]  div = 8'd0;
  reg [5:0]  bitn = 6'd0;  // bit of the frame: 0 to 39 the four bytes, 40 and 41 idle
  reg [7:0]  ui_s1 = 8'd0, ui_s2 = 8'd0, ui_f = 8'd0;
  reg        rst_s1 = 1'b0, rst_s2 = 1'b0, rst_f = 1'b0;
  reg [15:0] line_q = 16'hFFFF;

  always @(posedge clk) begin
    ui_s1  <= ui;
    ui_s2  <= ui_s1;
    rst_s1 <= rst_n;
    rst_s2 <= rst_s1;
    if (div == DIV - 1) begin
      div <= 8'd0;
      if (bitn == 6'd41) begin
        bitn  <= 6'd0;
        ui_f  <= ui_s2;
        rst_f <= rst_s2;
      end else begin
        bitn <= bitn + 6'd1;
      end
    end else begin
      div <= div + 8'd1;
    end
  end

  wire [15:0] line;
  genvar i;
  generate
    for (i = 0; i < 16; i = i + 1) begin : g_pin
      wire [7:0]  id = 8'hA0 + i;
      wire [7:0]  b2 = {7'd0, rst_f};
      wire [7:0]  ck = id ^ ui_f ^ b2 ^ 8'h5A;
      // bit 0 is the start bit of byte 0
      wire [39:0] fr = {1'b1, ck, 1'b0, 1'b1, b2, 1'b0, 1'b1, ui_f, 1'b0, 1'b1, id, 1'b0};
      assign line[i] = (bitn < 6'd40) ? fr[bitn] : 1'b1;
    end
  endgenerate

  // registered, so the pins change only at clock edges
  always @(posedge clk) line_q <= line;

  assign uo  = line_q[7:0];
  assign uio = line_q[15:8];
endmodule
