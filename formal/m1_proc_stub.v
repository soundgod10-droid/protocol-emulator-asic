/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_proc (formal stub for the M1 UART proof, formal/uart_tx.sby only).
//
// What it does:
//   It has the ports of src/pe_proc.v. Each output has a free value in
//   each cycle (anyseq). The inputs have no load.
//
// Why:
//   In M1 mode (CORE_SEL = 0, assumed in uart_tx_fv.v) the processor
//   drives no M1 pin: project.v selects the M1 pins, and only uo_out in
//   processor mode uses pin_v, pin_oe and the readout. Free outputs thus
//   include each value that the real processor can give. The real
//   processor has 2,560 latches, which make the M1 proof too large. The
//   smtbmc flow does not accept a black box (read_verilog -lib gives
//   "is a blackbox/whitebox module"), so this stub has free outputs.

module pe_proc (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] now,
    input  wire        start,
    input  wire [27:0] start_pc,
    input  wire [3:0]  run_mask,
    input  wire [29:0] pin_owner,
    input  wire [14:0] pin_en,
    input  wire [14:0] pin_od,
    input  wire        h_mem_we,
    input  wire [6:0]  h_mem_addr,
    input  wire [15:0] h_mem_data,
    input  wire [1:0]  h_tid,
    input  wire        h_push,
    input  wire [15:0] h_wdata,
    input  wire        h_pop,
    output wire        h_out_valid,
    output wire [15:0] h_out_data,
    input  wire [4:0]  ui_in,
    input  wire [7:0]  uio_in,
    output wire [14:0] pin_v,
    output wire [14:0] pin_oe
);

  (* anyseq *) reg        f_out_valid;
  (* anyseq *) reg [15:0] f_out_data;
  (* anyseq *) reg [14:0] f_pin_v;
  (* anyseq *) reg [14:0] f_pin_oe;

  assign h_out_valid = f_out_valid;
  assign h_out_data  = f_out_data;
  assign pin_v       = f_pin_v;
  assign pin_oe      = f_pin_oe;

endmodule
