/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// Free models for formal/proc.sby. Each module has the ports of the RTL module of the same name; each output
// is free in each cycle ($anyseq). They replace the SRAM macros (pe_sram: every program and data word), the host
// FIFOs (pe_host_fifo), the line helpers (pe_slu: every port word, helper pin level and event) and the input
// synchronizers (pe_timed_in: every input level and history). The proof then holds for every program and every
// input; the real modules have their own proofs or tests (docs/verification.md).

module pe_sram (
    input  wire        clk,
    input  wire        rst_n,
    input  wire        req,
    input  wire        we,
    input  wire [10:0] addr,
    input  wire [15:0] wdata,
    output wire [15:0] rdata
);
  assign rdata = $anyseq;
endmodule

module pe_host_fifo (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [1:0]  t_tid,
    input  wire        t_pull,
    input  wire        t_push,
    input  wire [15:0] t_wdata,
    output wire        t_in_valid,
    output wire [15:0] t_in_data,
    output wire        t_out_full,
    input  wire [1:0]  h_tid,
    input  wire        h_push,
    input  wire [15:0] h_wdata,
    output wire        h_in_full,
    input  wire        h_pop,
    output wire        h_out_valid,
    output wire [15:0] h_out_data,
    output wire [11:0] cnt_in,
    output wire [11:0] cnt_out
);
  assign t_in_valid  = $anyseq;
  assign t_in_data   = $anyseq;
  assign t_out_full  = $anyseq;
  assign h_in_full   = $anyseq;
  assign h_out_valid = $anyseq;
  assign h_out_data  = $anyseq;
  assign cnt_in      = $anyseq;
  assign cnt_out     = $anyseq;
endmodule

module pe_slu #(
    parameter FULL = 0,
    parameter SPEC = 0
) (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] now,
    input  wire        p_we,
    input  wire        p_re,
    input  wire [3:0]  p_addr,
    input  wire [15:0] p_wdata,
    output wire [15:0] p_rdata,
    input  wire [12:0] pin_raw,
    input  wire [12:0] pin_sync,
    output wire [3:0]  tx_v,
    output wire        tx_oe,
    output wire        ev
);
  assign p_rdata = $anyseq;
  assign tx_v    = $anyseq;
  assign tx_oe   = $anyseq;
  assign ev      = $anyseq;
endmodule

module pe_timed_in (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [4:0]  ui_in,
    input  wire [7:0]  uio_in,
    input  wire [7:0]  pin_v,
    input  wire [7:0]  pin_oe,
    input  wire [1:0]  ev_in,
    output wire [12:0] level,
    output wire [14:0] s_now,
    output wire [74:0] s_hist
);
  assign level  = $anyseq;
  assign s_now  = $anyseq;
  assign s_hist = $anyseq;
endmodule
