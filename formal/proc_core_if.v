/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// pe_core interface model for formal/proc.sby: the ports of src/pe_core.v; every output is free in every cycle,
// and the event writes obey the guarantees that formal/core.sby proves for every program and every input
// (pe_core E1 to E4), written here as assumptions on the outputs:
//   - an event is written only into an empty slot, into at most one slot a cycle (E1, E2);
//   - its time is 4 to 32,767 cycles after the write (E3);
//   - only the slot thread tid = now[1:0] writes, and only into the slot of an enabled pin that it owns and that
//     is not given to a helper (E4: f_out && f_owned, ev_load = 1 << pin).
// Which time (T + offset or now + 4) and the LATE flag are pe_core's own properties (E4, E6) and the LATE link
// of formal/core_fv.v; they do not depend on the slots.

module pe_core (
    input  wire        clk,
    input  wire        rst_n,
    input  wire [15:0] now,
    input  wire        start,
    input  wire [1:0]  prestart,
    input  wire [35:0] start_pc,
    input  wire [3:0]  run_mask,
    input  wire [29:0] pin_owner,
    input  wire [14:0] pin_en,
    input  wire [14:0] pin_od,
    input  wire [14:0] pin_hdrv,
    output wire [10:0] mem_addr,
    output wire        mem_we,
    output wire [15:0] mem_wdata,
    input  wire [15:0] mem_rdata,
    output wire        io_we,
    output wire        io_re,
    output wire [5:0]  io_addr,
    output wire [15:0] io_wdata,
    input  wire [15:0] io_rdata,
    input  wire [14:0] ev_ready,
    output wire [14:0] ev_load,
    output wire [15:0] ev_time,
    output wire        ev_value,
    output wire        ev_oe,
    input  wire [14:0] s_now,
    input  wire [74:0] s_hist,
    output wire        fifo_pull,
    output wire        fifo_push,
    output wire [15:0] fifo_wdata,
    input  wire        fifo_in_valid,
    input  wire [15:0] fifo_in_data,
    input  wire        fifo_out_full,
    output wire [3:0]  th_run,
    output wire [3:0]  th_late,
    output wire [3:0]  th_err
);
  assign mem_addr   = $anyseq;
  assign mem_we     = $anyseq;
  assign mem_wdata  = $anyseq;
  assign io_we      = $anyseq;
  assign io_re      = $anyseq;
  assign io_addr    = $anyseq;
  assign io_wdata   = $anyseq;
  assign ev_load    = $anyseq;
  assign ev_time    = $anyseq;
  assign ev_value   = $anyseq;
  assign ev_oe      = $anyseq;
  assign fifo_pull  = $anyseq;
  assign fifo_push  = $anyseq;
  assign fifo_wdata = $anyseq;
  assign th_run     = $anyseq;
  assign th_late    = $anyseq;
  assign th_err     = $anyseq;

  wire [15:0] lead = ev_time - now;
  always @* begin
    if (!rst_n) assume (ev_load == 15'd0);
    assume ((ev_load & ~ev_ready) == 15'd0);
    assume ((ev_load & (ev_load - 15'd1)) == 15'd0);
    if (ev_load != 15'd0) assume ((lead >= 16'd4) && (lead <= 16'd32767));
  end
  genvar p;
  generate
    for (p = 0; p < 15; p = p + 1) begin : g_own
      always @* if (ev_load[p]) assume (pin_en[p] && !pin_hdrv[p] && (pin_owner[2*p+:2] == now[1:0]));
    end
  endgenerate
endmodule
