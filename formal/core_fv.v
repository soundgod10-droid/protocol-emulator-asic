/*
 * Copyright (c) 2026 Steven Anthony
 * SPDX-License-Identifier: Apache-2.0
 */

`default_nettype none

// core_fv: formal harness of the barrel core (pe_core, A1 block D1).
//
// What it does:
//   It connects the core to the cycle counter (pe_cycle_counter, clear =
//   START, as in project.v) and gives all other inputs of the core freely
//   in each cycle: START at any cycle, each configuration, each memory
//   word (instruction or data), each state of the event slots, the input
//   history and the FIFOs. The properties are in pe_core (`ifdef FORMAL:
//   B1 to B4 barrel order, E1 to E8 pin events, W1 to W3 memory write).
//
// Why:
//   With free inputs, a proof holds for each program and each input, not
//   only for the programs of the lockstep test (test/test_core.py).
//   The low 16 bits of the counter are the same for WIDTH = 16 and for
//   the 32 bits of the chip, so the check uses 16 bits.

module core_fv (
    input wire        clk,
    input wire        rst_n,
    input wire        start,
    input wire [27:0] start_pc,
    input wire [3:0]  run_mask,
    input wire [29:0] pin_owner,
    input wire [14:0] pin_en,
    input wire [14:0] pin_od,
    input wire [15:0] mem_rdata,
    input wire [14:0] ev_ready,
    input wire [12:0] s_now,
    input wire [64:0] s_hist,
    input wire        fifo_in_valid,
    input wire [15:0] fifo_in_data,
    input wire        fifo_out_full
);

  // The check starts in reset. After it, reset can occur at any cycle.
  initial assume (!rst_n);

  wire [15:0] now;

  pe_cycle_counter #(
      .WIDTH(16)
  ) u_cnt (
      .clk  (clk),
      .rst_n(rst_n),
      .clear(start),
      .count(now)
  );

  wire [6:0]  mem_raddr;
  wire        mem_we;
  wire [6:0]  mem_waddr;
  wire [15:0] mem_wdata;
  wire [14:0] ev_load;
  wire [15:0] ev_time;
  wire        ev_value;
  wire        ev_oe;
  wire        fifo_pull;
  wire        fifo_push;
  wire [15:0] fifo_wdata;

  pe_core u_core (
      .clk          (clk),
      .rst_n        (rst_n),
      .now          (now),
      .start        (start),
      .start_pc     (start_pc),
      .run_mask     (run_mask),
      .pin_owner    (pin_owner),
      .pin_en       (pin_en),
      .pin_od       (pin_od),
      .mem_raddr    (mem_raddr),
      .mem_rdata    (mem_rdata),
      .mem_we       (mem_we),
      .mem_waddr    (mem_waddr),
      .mem_wdata    (mem_wdata),
      .ev_ready     (ev_ready),
      .ev_load      (ev_load),
      .ev_time      (ev_time),
      .ev_value     (ev_value),
      .ev_oe        (ev_oe),
      .s_now        (s_now),
      .s_hist       (s_hist),
      .fifo_pull    (fifo_pull),
      .fifo_push    (fifo_push),
      .fifo_wdata   (fifo_wdata),
      .fifo_in_valid(fifo_in_valid),
      .fifo_in_data (fifo_in_data),
      .fifo_out_full(fifo_out_full)
  );

  // The properties are not empty: a store, an event write and a START
  // during the run can occur.
  always @* begin
    cover (rst_n && mem_we);
    cover (rst_n && (ev_load != 15'd0));
    cover (rst_n && start && (now[1:0] == 2'd2));
  end

endmodule
